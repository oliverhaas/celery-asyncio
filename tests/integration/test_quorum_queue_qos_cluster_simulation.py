import gc
import logging
import multiprocessing
import os
import pprint
import uuid

import pytest
from kombu import Queue

from celery import Celery, _state
from celery.contrib.testing.worker import start_worker
from celery.utils.eventloop import default_loop_runner

QOS_GLOBAL_ERROR = "qos.global not allowed"

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def create_app(queue_name: str) -> Celery:
    rabbitmq_user = os.environ.get("RABBITMQ_DEFAULT_USER", "guest")
    rabbitmq_pass = os.environ.get("RABBITMQ_DEFAULT_PASS", "guest")
    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = os.environ.get("REDIS_PORT", "6379")

    broker_url = os.environ.get("TEST_BROKER", f"amqp://{rabbitmq_user}:{rabbitmq_pass}@localhost:5672//")
    redis_db = os.environ.get("REDIS_DB", "10")
    backend_url = os.environ.get("TEST_BACKEND", f"redis://{redis_host}:{redis_port}/{redis_db}")

    app = Celery("quorum_qos_race", broker=broker_url, backend=backend_url)

    app.conf.task_queues = [
        Queue(
            name=queue_name,
            queue_arguments={"x-queue-type": "quorum"},
        )
    ]
    app.conf.task_default_queue = queue_name
    app.conf.worker_prefetch_multiplier = 1
    app.conf.task_acks_late = True
    app.conf.task_reject_on_worker_lost = True
    app.conf.broker_transport_options = {"confirm_publish": True}

    return app


def dummy_task_factory(app: Celery, simulate_qos_issue: bool):
    @app.task(name="dummy_task")
    def dummy_task():
        if simulate_qos_issue:
            raise Exception("qos.global not allowed on quorum queues (simulated)")
        return "ok"

    return dummy_task


def run_worker(queue_name: str, simulate_qos_issue: bool, result_queue: multiprocessing.Queue):
    app = create_app(queue_name)
    logger.info("[Celery config snapshot]:\n%s", pprint.pformat(dict(app.conf)))
    task = dummy_task_factory(app, simulate_qos_issue)

    try:
        with start_worker(
            app,
            queues=[queue_name],
            loglevel="INFO",
            perform_ping_check=False,
            shutdown_timeout=15,
        ):
            res = task.delay()
            try:
                result = res.get(timeout=10)
                result_queue.put({"status": "ok", "result": result})
            except Exception as e:
                result_queue.put({"status": "error", "reason": str(e)})
    except Exception as e:
        logger.exception("[worker %s] external failure", simulate_qos_issue)
        result_queue.put({"status": "external_failure", "reason": str(e)})
    finally:
        if result_queue.empty():
            result_queue.put({"status": "crash", "reason": "Worker crashed without reporting"})


async def delete_queues(queue_names: list[str]) -> None:
    async with create_app(queue_names[0]).connection_for_write() as connection:
        channel = await connection.channel()
        for queue_name in queue_names:
            await channel.queue_delete(queue_name)


@pytest.mark.amqp
@pytest.mark.timeout(90)
def test_rabbitmq_quorum_qos_visibility_race():
    try:
        multiprocessing.set_start_method("spawn", force=True)
    except RuntimeError:
        pass

    results = []
    processes = []
    queues = []
    queue_names = []

    for i in range(3):
        simulate = i == 0
        q = multiprocessing.Queue()
        queues.append(q)
        queue_name = f"race_quorum_queue_{uuid.uuid4().hex}"
        queue_names.append(queue_name)

        p = multiprocessing.Process(target=run_worker, args=(queue_name, simulate, q))
        p.daemon = True
        processes.append(p)
        p.start()

    try:
        for i, (p, q) in enumerate(zip(processes, queues, strict=False)):
            try:
                p.join(timeout=30)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=10)
                    results.append({"status": "timeout", "reason": f"[worker {i}] timeout"})
                else:
                    try:
                        results.append(q.get(timeout=5))
                    except Exception as e:
                        results.append({"status": "error", "reason": f"Result error: {e!s}"})
            except Exception:
                try:
                    results.append(q.get(timeout=5))
                except Exception:
                    results.append({"status": "crash", "reason": f"Worker {i} crashed and gave no result"})

        if any(QOS_GLOBAL_ERROR in r.get("reason", "").lower() for r in results):
            pytest.xfail("Detected global QoS usage on quorum queue (simulated failure)")
    finally:
        for i, p in enumerate(processes):
            if p.is_alive():
                p.terminate()
                p.join(timeout=10)

        # Quorum queues are durable, so they outlive the run unless deleted.
        default_loop_runner().run(delete_queues(queue_names))

        # Reset Celery app/task global state
        _state._set_current_app(None)
        _state._task_stack.__init__()  # reinitialize stack to avoid stale state

        # Force garbage collection
        gc.collect()

        # Reset multiprocessing to default (may help restore test_multiprocess_producer expectations)
        if multiprocessing.get_start_method(allow_none=True) == "spawn":
            try:
                multiprocessing.set_start_method("fork", force=True)
            except RuntimeError:
                pass
