import pytest
from kombu import Exchange, Queue

from celery import Celery, chord
from celery.contrib.testing.worker import start_worker
from celery.result import allow_join_result
from celery.utils.eventloop import default_loop_runner
from tests.integration.conftest import TEST_BACKEND, TEST_BROKER

HEADER_QUEUE = "header_queue"
CALLBACK_QUEUE = "chord_callback_queue"


async def delete_queues(app: Celery) -> None:
    async with app.connection_for_write() as connection:
        channel = await connection.channel()
        for queue in (HEADER_QUEUE, CALLBACK_QUEUE):
            await channel.queue_delete(queue)


@pytest.fixture
def app():
    """Quorum queues behind topic exchanges, chord_unlock routed to one of them."""
    app = Celery("test_app", broker=TEST_BROKER, backend=TEST_BACKEND)

    app.conf.task_default_exchange_type = "topic"
    app.conf.task_default_exchange = "default_exchange"
    app.conf.task_default_queue = "default_queue"
    app.conf.task_default_routing_key = "default"

    app.conf.task_queues = [
        Queue(
            HEADER_QUEUE,
            Exchange("header_exchange", type="topic"),
            routing_key="header_rk",
            queue_arguments={"x-queue-type": "quorum"},
        ),
        Queue(
            CALLBACK_QUEUE,
            Exchange("chord_callback_exchange", type="topic"),
            routing_key=CALLBACK_QUEUE,
            queue_arguments={"x-queue-type": "quorum"},
        ),
    ]

    app.conf.task_routes = {
        "celery.chord_unlock": {
            "queue": CALLBACK_QUEUE,
            "exchange": "chord_callback_exchange",
            "routing_key": CALLBACK_QUEUE,
            "exchange_type": "topic",
        },
    }

    yield app

    # Quorum queues are durable, so they outlive the run unless deleted.
    default_loop_runner().run(delete_queues(app))


@pytest.fixture
def add(app):
    @app.task(bind=True, max_retries=3, default_retry_delay=1)
    def add(self, x, y):
        return x + y

    return add


@pytest.fixture
def summarize(app):
    @app.task(bind=True, max_retries=3, default_retry_delay=1)
    def summarize(self, results):
        return sum(results)

    return summarize


@pytest.mark.amqp
@pytest.mark.timeout(120)
def test_chords_complete_over_quorum_queues_behind_topic_exchanges(app, add, summarize):
    """Celery discussion #9742: chords routed this way got stuck on RabbitMQ."""
    chord_count = 50
    header_fanout = 3

    with (
        start_worker(app, queues=[HEADER_QUEUE, CALLBACK_QUEUE], loglevel="info", perform_ping_check=False),
        allow_join_result(),
    ):
        results = []
        for i in range(chord_count):
            header = [
                add.s(i, j).set(queue=HEADER_QUEUE, exchange="header_exchange", routing_key="header_rk")
                for j in range(header_fanout)
            ]
            callback = summarize.s().set(
                queue=CALLBACK_QUEUE, exchange="chord_callback_exchange", routing_key=CALLBACK_QUEUE
            )
            results.append((i, chord(header)(callback)))

        for i, result in results:
            assert result.get(timeout=30) == sum(i + j for j in range(header_fanout))
