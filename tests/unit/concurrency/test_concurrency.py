import asyncio
import logging
import os
import threading
import time
from contextlib import contextmanager
from itertools import count
from unittest.mock import Mock, call, patch

import pytest

from celery import concurrency, signals, states
from celery.app.trace import trace_task_ret
from celery.concurrency.aio import (
    ApplyResult,
    AsyncApplyResult,
    LoopWorker,
    SyncJob,
    TaskPool,
    _TaskTerminated,
    _TaskTimedOut,
)
from celery.concurrency.base import BasePool, apply_target
from celery.exceptions import (
    SoftTimeLimitExceeded,
    Terminated,
    WorkerLostError,
    WorkerShutdown,
    WorkerTerminate,
)
from celery.result import AsyncResult
from celery.utils import uuid
from celery.worker import state as worker_state
from celery.worker.request import Request


def report_when_done(handler, done):
    """Wrap a request handler so a test can wait for it to have run."""

    def wrapper(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        finally:
            done.set()

    return wrapper


def wait_until(predicate, timeout=10.0):
    """Poll until the predicate holds, returning whether it did."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


@contextmanager
def collect_signal(signal):
    """Collect what a signal is sent with while the block runs."""
    received = []

    def receiver(**kwargs):
        received.append(kwargs)

    signal.connect(receiver, weak=False)
    try:
        yield received
    finally:
        signal.disconnect(receiver)


class Recorder:
    """Collects what the pool reports back for one task."""

    def __init__(self):
        self.done = threading.Event()
        self.results = []
        self.failures = []
        self.timeouts = []
        self.accepted = []

    def on_success(self, result):
        self.results.append(result)
        self.done.set()

    def on_failure(self, exc_info, **kwargs):
        self.failures.append(exc_info)
        self.done.set()

    def on_accepted(self, pid, time_accepted):
        self.accepted.append((pid, time_accepted))

    def on_timeout(self, soft, timeout):
        self.timeouts.append((soft, timeout))
        self.done.set()

    @property
    def result(self):
        assert len(self.results) == 1, self.results
        return self.results[0]

    @property
    def failure(self):
        assert len(self.failures) == 1, self.failures
        return self.failures[0].exception


class AioPoolCase:
    """Runs tasks through a started pool the way the worker does."""

    def setup_method(self):
        self._pools = []

    def teardown_method(self):
        for pool in self._pools:
            pool.stop()

    def start_pool(self, **kwargs):
        pool = TaskPool(10, app=self.app, **kwargs)
        pool.start()
        self._pools.append(pool)
        return pool

    def apply(self, pool, name, recorder, args=(), kwargs=None, task_id=None, **options):
        task_id = task_id or uuid()
        request = {"id": task_id, "task": name, "hostname": "testhost", "delivery_info": {}}
        pool.apply_async(
            trace_task_ret,
            args=(name, task_id, request, (args, kwargs or {}, {}), None, None),
            accept_callback=recorder.on_accepted,
            timeout_callback=recorder.on_timeout,
            callback=recorder.on_success,
            error_callback=recorder.on_failure,
            **options,
        )
        return task_id

    def meta(self, task_id):
        return self.app.backend.get_task_meta(task_id)


class test_BasePool:
    def test_apply_target(self):

        scratch = {}
        counter = count(0)

        def gen_callback(name, retval=None):

            def callback(*args):
                scratch[name] = (next(counter), args)
                return retval

            return callback

        apply_target(
            gen_callback("target", 42),
            args=(8, 16),
            callback=gen_callback("callback"),
            accept_callback=gen_callback("accept_callback"),
        )

        assert scratch["target"] == (1, (8, 16))
        assert scratch["callback"] == (2, (42,))
        pa1 = scratch["accept_callback"]
        assert pa1[0] == 0
        assert pa1[1][0] == os.getpid()
        assert pa1[1][1]

        # No accept callback
        scratch.clear()
        apply_target(gen_callback("target", 42), args=(8, 16), callback=gen_callback("callback"), accept_callback=None)
        assert scratch == {
            "target": (3, (8, 16)),
            "callback": (4, (42,)),
        }

    def test_apply_target__propagate(self):
        target = Mock(name="target")
        target.side_effect = KeyError()
        with pytest.raises(KeyError):
            apply_target(target, propagate=(KeyError,))

    def test_apply_target__raises(self):
        target = Mock(name="target")
        target.side_effect = KeyError()
        with pytest.raises(KeyError):
            apply_target(target)

    def test_apply_target__raises_WorkerShutdown(self):
        target = Mock(name="target")
        target.side_effect = WorkerShutdown()
        with pytest.raises(WorkerShutdown):
            apply_target(target)

    def test_apply_target__raises_WorkerTerminate(self):
        target = Mock(name="target")
        target.side_effect = WorkerTerminate()
        with pytest.raises(WorkerTerminate):
            apply_target(target)

    def test_apply_target__raises_BaseException(self):
        target = Mock(name="target")
        callback = Mock(name="callback")
        target.side_effect = BaseException()
        apply_target(target, callback=callback)
        callback.assert_called()

    @patch("celery.concurrency.base.reraise")
    def test_apply_target__raises_BaseException_raises_else(self, reraise):
        target = Mock(name="target")
        callback = Mock(name="callback")
        reraise.side_effect = KeyError()
        target.side_effect = BaseException()
        with pytest.raises(KeyError):
            apply_target(target, callback=callback)
        callback.assert_not_called()

    def test_does_not_debug(self):
        x = BasePool(10)
        x._does_debug = False
        x.apply_async(object)

    def test_num_processes(self):
        assert BasePool(7).num_processes == 7

    def test_interface_on_start(self):
        BasePool(10).on_start()

    def test_interface_on_stop(self):
        BasePool(10).on_stop()

    def test_interface_on_apply(self):
        BasePool(10).on_apply()

    def test_interface_info(self):
        assert BasePool(10).info == {
            "implementation": "celery.concurrency.base:BasePool",
            "max-concurrency": 10,
        }

    def test_interface_flush(self):
        assert BasePool(10).flush() is None

    def test_active(self):
        p = BasePool(10)
        assert not p.active
        p._state = p.RUN
        assert p.active

    def test_restart(self):
        p = BasePool(10)
        with pytest.raises(NotImplementedError):
            p.restart()

    def test_interface_on_terminate(self):
        p = BasePool(10)
        p.on_terminate()

    def test_interface_terminate_job(self):
        with pytest.raises(NotImplementedError):
            BasePool(10).terminate_job(101)

    def test_interface_did_start_ok(self):
        assert BasePool(10).did_start_ok()

    def test_interface_register_with_event_loop(self):
        assert BasePool(10).register_with_event_loop(Mock()) is None

    def test_interface_on_soft_timeout(self):
        assert BasePool(10).on_soft_timeout(Mock()) is None

    def test_interface_on_hard_timeout(self):
        assert BasePool(10).on_hard_timeout(Mock()) is None

    def test_interface_close(self):
        p = BasePool(10)
        p.on_close = Mock()
        p.close()
        assert p._state == p.CLOSE
        p.on_close.assert_called_with()

    def test_interface_no_close(self):
        assert BasePool(10).on_close() is None


class test_get_available_pool_names:
    def test_returns_asyncio_pool_names(self):
        expected_pool_names = ("asyncio",)
        assert concurrency.get_available_pool_names() == expected_pool_names


class test_LoopWorker:
    def test_start_and_stop(self):
        app = Mock()
        w = LoopWorker(concurrency=5, app=app, index=0)
        w.start()
        try:
            assert w._loop is not None
            assert w._loop.is_running()
            assert w._semaphore is not None
            assert w._thread.is_alive()
            assert w._thread.name == "celery-loop-worker-0"
            app.set_current.assert_called_once()
        finally:
            w.stop()
        assert not w._thread.is_alive()

    def test_stop_closes_the_loop(self):
        w = LoopWorker(concurrency=5, app=Mock(), index=0)
        w.start()
        w.stop()
        assert w._loop.is_closed()

    def test_stop_is_idempotent(self):
        w = LoopWorker(concurrency=5, app=Mock(), index=0)
        w.start()
        w.stop()
        w.stop()

    def test_submit_runs_coroutine(self):
        app = Mock()
        w = LoopWorker(concurrency=5, app=app, index=0)
        w.start()
        try:
            result = []
            done = threading.Event()

            async def coro():
                result.append(42)
                done.set()

            w.submit(coro)
            assert done.wait(timeout=5)
            assert result == [42]
        finally:
            w.stop()

    def test_semaphore_limits_concurrency(self):
        app = Mock()
        concurrency_limit = 2
        w = LoopWorker(concurrency=concurrency_limit, app=app, index=0)
        w.start()
        try:
            threading.Event()
            max_concurrent = []
            current_count = [0]
            lock = threading.Lock()
            all_done = threading.Event()
            total = 4

            async def coro():
                with lock:
                    current_count[0] += 1
                    max_concurrent.append(current_count[0])
                await asyncio.sleep(0.1)
                with lock:
                    current_count[0] -= 1
                    if len(max_concurrent) >= total * 2 - 1:
                        # Approximation: we've recorded enough
                        pass

            done_count = [0]
            done_lock = threading.Lock()

            async def coro_with_done():
                with lock:
                    current_count[0] += 1
                    max_concurrent.append(current_count[0])
                await asyncio.sleep(0.1)
                with lock:
                    current_count[0] -= 1
                with done_lock:
                    done_count[0] += 1
                    if done_count[0] == total:
                        all_done.set()

            for _ in range(total):
                w.submit(coro_with_done)

            assert all_done.wait(timeout=5)
            # The max concurrent should never exceed the semaphore limit
            assert max(max_concurrent) <= concurrency_limit
        finally:
            w.stop()

    def test_active_count_tracking(self):
        app = Mock()
        w = LoopWorker(concurrency=10, app=app, index=0)
        w.start()
        try:
            started = threading.Event()
            release = threading.Event()

            async def coro():
                started.set()
                # Wait for release signal - poll since threading.Event
                # can't be awaited
                while not release.is_set():
                    await asyncio.sleep(0.01)

            w.submit(coro)
            assert started.wait(timeout=5)
            # While task is running, active_count should be >= 1
            assert w._active_count >= 1
            release.set()
            assert wait_until(lambda: w._active_count == 0)
        finally:
            w.stop()

    def test_a_job_cancelled_before_its_first_step_frees_its_slot(self):
        w = LoopWorker(concurrency=10, app=Mock(), index=0)
        w.start()
        try:
            ran = []
            on_done = Mock(name="on_done")
            job = AsyncApplyResult(w, "job-id", on_done)
            job.terminate()

            async def coro():
                ran.append(True)

            w.submit(coro, job=job)

            assert wait_until(lambda: on_done.called)
            assert wait_until(lambda: w._active_count == 0)
            assert ran == []
        finally:
            w.stop()


class test_TaskPool:
    def test_init_defaults(self):
        pool = TaskPool(10, app=Mock())
        assert pool._loop_worker_count == 1
        assert pool._loop_concurrency == 10
        assert pool._sync_worker_count == 1

    def test_init_custom(self):
        pool = TaskPool(10, app=Mock(), loop_workers=3, loop_concurrency=20, sync_workers=4)
        assert pool._loop_worker_count == 3
        assert pool._loop_concurrency == 20
        assert pool._sync_worker_count == 4

    def test_start_stop(self):
        app = Mock()
        pool = TaskPool(10, app=app, loop_workers=2, loop_concurrency=5, sync_workers=2)
        pool.on_start()
        try:
            assert len(pool._loop_workers) == 2
            assert pool._executor is not None
            for w in pool._loop_workers:
                assert w._loop.is_running()
                assert w._thread.is_alive()
        finally:
            pool.on_stop()
        assert len(pool._loop_workers) == 0
        assert pool._executor is None

    def test_least_loaded_dispatch(self):
        pool = TaskPool(10, app=Mock(), loop_workers=3)
        w0 = Mock(_active_count=5)
        w1 = Mock(_active_count=2)
        w2 = Mock(_active_count=8)
        pool._loop_workers = [w0, w1, w2]

        # Should pick w1 (lowest active count)
        assert pool._pick_loop_worker() is w1

        # After w1 gets more load, should pick w0
        w1._active_count = 7
        assert pool._pick_loop_worker() is w0

        w0._active_count = 3
        w1._active_count = 3
        w2._active_count = 3
        assert pool._pick_loop_worker() is w0

    def test_is_async_task(self):
        app = Mock()
        pool = TaskPool(10, app=app)

        # Async task
        async def async_run():
            pass

        task = Mock()
        task.run = async_run
        app.tasks.__getitem__ = Mock(return_value=task)
        assert pool._is_async_task(("my.task",))

        # Sync task
        def sync_run():
            pass

        task.run = sync_run
        assert not pool._is_async_task(("my.task",))

        # No args
        assert not pool._is_async_task(())

        # Unknown task
        app.tasks.__getitem__ = Mock(side_effect=KeyError)
        assert not pool._is_async_task(("unknown.task",))

    def test_sync_task_dispatch(self):
        app = Mock()
        pool = TaskPool(10, app=app, sync_workers=1)
        pool.on_start()
        try:
            result_holder = []
            done = threading.Event()

            def target(*args, **kwargs):
                result_holder.append("executed")
                done.set()
                return (0, "ok", 0.1)

            result = pool._apply_sync_task(
                target,
                ("my.task", "uuid", {}, b"body", None, None),
                {},
                callback=None,
                accept_callback=None,
            )
            assert isinstance(result, ApplyResult)
        finally:
            pool.on_stop()

    def test_get_info(self):
        app = Mock()
        pool = TaskPool(10, app=app, loop_workers=2, loop_concurrency=5, sync_workers=3)
        pool._loop_workers = [Mock(_active_count=1), Mock(_active_count=2)]
        info = pool._get_info()
        assert info["implementation"] == "asyncio+threads"
        assert info["loop-workers"] == 2
        assert info["loop-concurrency"] == 5
        assert info["sync-workers"] == 3
        assert info["loop-active"] == [1, 2]


class test_async_task_failures(AioPoolCase):
    def test_soft_time_limit_fails_the_task(self, caplog):
        @self.app.task(name="aio.soft_limit", shared=False)
        async def sleeper():
            await asyncio.sleep(30)

        pool = self.start_pool()
        rec = Recorder()
        with caplog.at_level(logging.ERROR):
            with collect_signal(signals.task_failure) as failures:
                task_id = self.apply(pool, "aio.soft_limit", rec, soft_timeout=0.2)
                assert rec.done.wait(10)

        failed, retval, _ = rec.result
        assert failed
        assert isinstance(retval.exception, SoftTimeLimitExceeded)
        assert self.meta(task_id)["status"] == states.FAILURE
        assert isinstance(self.meta(task_id)["result"], SoftTimeLimitExceeded)
        assert [f["task_id"] for f in failures] == [task_id]
        assert isinstance(failures[0]["exception"], SoftTimeLimitExceeded)
        assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        with pytest.raises(SoftTimeLimitExceeded):
            AsyncResult(task_id, app=self.app).get(timeout=5)

    def test_an_error_outside_the_tracer_goes_to_the_error_callback(self):
        @self.app.task(name="aio.escaping", shared=False)
        async def escaping():
            return 1

        async def broken_tracer(*args, **kwargs):
            raise RuntimeError("the result backend is gone")

        escaping.__async_trace__ = broken_tracer

        pool = self.start_pool()
        rec = Recorder()
        self.apply(pool, "aio.escaping", rec)
        assert rec.done.wait(10)
        assert rec.results == []
        assert isinstance(rec.failure, RuntimeError)

    def test_an_error_outside_the_tracer_fails_the_request(self, caplog):
        @self.app.task(name="aio.escaping_request", shared=False)
        async def escaping():
            return 1

        async def broken_tracer(*args, **kwargs):
            raise RuntimeError("the result backend is gone")

        escaping.__async_trace__ = broken_tracer

        pool = self.start_pool()
        message = self.TaskMessage("aio.escaping_request", args=(), kwargs={})
        req = Request(message, app=self.app, on_ack=Mock(), on_reject=Mock())
        reported = threading.Event()
        req.on_failure = report_when_done(req.on_failure, reported)
        with caplog.at_level(logging.ERROR):
            with collect_signal(signals.task_failure) as failures:
                req.execute_using_pool(pool)
                assert reported.wait(10)

        assert self.meta(req.id)["status"] == states.FAILURE
        assert isinstance(self.meta(req.id)["result"], RuntimeError)
        assert [f["task_id"] for f in failures] == [req.id]
        assert any("the result backend is gone" in r.getMessage() for r in caplog.records)


class test_async_task_termination(AioPoolCase):
    def _long_task(self, name, **options):
        """Register an async task that reports how it ended."""
        marks = {"started": threading.Event(), "cancelled": threading.Event(), "completed": threading.Event()}

        @self.app.task(name=name, shared=False, **options)
        async def long_one():
            marks["started"].set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                marks["cancelled"].set()
                raise
            marks["completed"].set()
            return "finished"

        return marks

    def test_terminate_job_cancels_the_running_coroutine(self):
        marks = self._long_task("aio.terminate_me")
        pool = self.start_pool()
        rec = Recorder()
        task_id = self.apply(pool, "aio.terminate_me", rec)
        assert marks["started"].wait(10)

        assert pool.terminate_job(task_id) is True

        assert rec.done.wait(10)
        assert marks["cancelled"].is_set()
        assert not marks["completed"].is_set()
        assert rec.results == []
        assert isinstance(rec.failure, Terminated)

    def test_revoked_task_is_not_overwritten_by_its_own_result(self):
        marks = self._long_task("aio.revoke_me")
        pool = self.start_pool()
        message = self.TaskMessage("aio.revoke_me", args=(), kwargs={})
        req = Request(message, app=self.app, on_ack=Mock(), on_reject=Mock())
        req.execute_using_pool(pool)
        assert marks["started"].wait(10)

        req.terminate(pool)

        assert marks["cancelled"].wait(10)
        assert wait_until(lambda: not pool._async_jobs)
        assert not marks["completed"].is_set()
        assert self.meta(req.id)["status"] == states.REVOKED

    def test_cancelling_on_connection_loss_stops_the_coroutine(self):
        marks = self._long_task("aio.cancel_me")
        pool = self.start_pool()
        message = self.TaskMessage("aio.cancel_me", args=(), kwargs={})
        req = Request(message, app=self.app, on_ack=Mock(), on_reject=Mock())
        req.execute_using_pool(pool)
        assert marks["started"].wait(10)

        req.cancel(pool)

        assert marks["cancelled"].wait(10)
        assert wait_until(lambda: not pool._async_jobs)
        assert not marks["completed"].is_set()
        assert self.meta(req.id)["status"] == states.RETRY

    def test_cancelling_is_a_retry_not_a_revoke_when_the_pool_reports_first(self):
        # The pool reports the cancelled coroutine from its loop thread, here
        # while cancel() is still in the task's on_retry handler.
        reported = threading.Event()
        marks = self._long_task("aio.cancel_me_slowly", on_retry=lambda *args: reported.wait(10))
        pool = self.start_pool()
        message = self.TaskMessage("aio.cancel_me_slowly", args=(), kwargs={})
        req = Request(message, app=self.app, on_ack=Mock(), on_reject=Mock())
        req.on_failure = report_when_done(req.on_failure, reported)
        req.execute_using_pool(pool)
        assert marks["started"].wait(10)

        with collect_signal(signals.task_revoked) as revoked:
            req.cancel(pool)

        assert revoked == []
        assert self.meta(req.id)["status"] == states.RETRY

    def test_terminating_announces_the_revoke_once_when_the_pool_reports_first(self):
        # As above, with terminate() still publishing its task-revoked event.
        reported = threading.Event()
        revokes = []

        def send(type, **fields):
            if type == "task-revoked":
                revokes.append(fields)
                if len(revokes) == 1:
                    reported.wait(10)

        marks = self._long_task("aio.terminate_me_slowly")
        pool = self.start_pool()
        message = self.TaskMessage("aio.terminate_me_slowly", args=(), kwargs={})
        eventer = Mock(enabled=True, send=Mock(side_effect=send))
        req = Request(message, app=self.app, on_ack=Mock(), on_reject=Mock(), eventer=eventer)
        req.on_failure = report_when_done(req.on_failure, reported)
        req.execute_using_pool(pool)
        assert marks["started"].wait(10)

        req.terminate(pool)

        assert len(revokes) == 1

    def test_terminating_a_job_storing_its_result_leaves_it_to_finish(self):
        storing = threading.Event()

        @self.app.task(name="aio.finishes_while_revoked", shared=False)
        async def finishes_while_revoked():
            return "finished"

        backend_cls = type(self.app.backend)
        store = backend_cls.amark_as_done

        async def slow_store(backend, *args, **kwargs):
            storing.set()
            await asyncio.sleep(0.1)
            return await store(backend, *args, **kwargs)

        pool = self.start_pool()
        message = self.TaskMessage("aio.finishes_while_revoked", args=(), kwargs={})
        req = Request(message, app=self.app, on_ack=Mock(), on_reject=Mock())
        done = threading.Event()
        req.on_success = report_when_done(req.on_success, done)
        with patch.object(backend_cls, "amark_as_done", slow_store), collect_signal(signals.task_revoked) as revoked:
            req.execute_using_pool(pool)
            assert storing.wait(10)
            req.terminate(pool)
            assert done.wait(10)

        assert revoked == []
        assert self.meta(req.id)["status"] == states.SUCCESS


class test_sync_task_termination(AioPoolCase):
    def setup_method(self):
        super().setup_method()
        self.release = threading.Event()

    def teardown_method(self):
        self.release.set()
        super().teardown_method()

    def _busy_task(self, name, **options):
        marks = {"started": threading.Event(), "completed": threading.Event()}

        @self.app.task(name=name, shared=False, **options)
        def busy():
            marks["started"].set()
            while not self.release.is_set():
                time.sleep(0.01)
            marks["completed"].set()
            return "finished"

        return marks

    def _request(self, name, on_ack=None, on_reject=None):
        message = self.TaskMessage(name, args=(), kwargs={})
        req = Request(message, app=self.app, on_ack=on_ack or Mock(), on_reject=on_reject or Mock())
        reported = threading.Event()
        req.on_failure = report_when_done(req.on_failure, reported)
        return req, reported

    def test_terminating_a_running_task_revokes_it(self):
        marks = self._busy_task("sync.terminate_me")
        pool = self.start_pool(sync_workers=1)
        req, reported = self._request("sync.terminate_me")
        req.execute_using_pool(pool)
        assert marks["started"].wait(10)

        with collect_signal(signals.task_failure) as failures, collect_signal(signals.task_revoked) as revoked:
            req.terminate(pool)
            assert reported.wait(10)

        assert not marks["completed"].is_set()
        assert failures == []
        assert len(revoked) == 1
        assert self.meta(req.id)["status"] == states.REVOKED

    def test_cancelling_a_running_task_is_a_retry(self):
        marks = self._busy_task("sync.cancel_me")
        pool = self.start_pool(sync_workers=1)
        req, reported = self._request("sync.cancel_me")
        req.execute_using_pool(pool)
        assert marks["started"].wait(10)

        with collect_signal(signals.task_revoked) as revoked:
            req.cancel(pool)
            assert reported.wait(10)

        assert not marks["completed"].is_set()
        assert revoked == []
        assert self.meta(req.id)["status"] == states.RETRY

    def test_cancelling_an_acks_late_task_leaves_its_message_for_redelivery(self):
        marks = self._busy_task("sync.redeliver_me", acks_late=True)
        pool = self.start_pool(sync_workers=1)
        on_ack, on_reject = Mock(name="on_ack"), Mock(name="on_reject")
        req, reported = self._request("sync.redeliver_me", on_ack=on_ack, on_reject=on_reject)
        req.execute_using_pool(pool)
        assert marks["started"].wait(10)

        req.cancel(pool, emit_retry=False)

        assert reported.wait(10)
        assert not marks["completed"].is_set()
        on_ack.assert_not_called()
        on_reject.assert_not_called()
        assert self.meta(req.id)["status"] == states.PENDING

    def test_a_cancel_that_spares_threads_lets_the_task_finish(self):
        marks = self._busy_task("sync.outlives_the_cancel")
        pool = self.start_pool(sync_workers=1)
        req, _ = self._request("sync.outlives_the_cancel")
        done = threading.Event()
        req.on_success = report_when_done(req.on_success, done)
        req.execute_using_pool(pool)
        assert marks["started"].wait(10)

        req.cancel(pool, interrupt_thread=False)
        self.release.set()

        assert done.wait(10)
        assert marks["completed"].is_set()
        assert self.meta(req.id)["status"] == states.SUCCESS

    def test_a_task_terminated_before_it_starts_never_runs(self):
        marks = self._busy_task("sync.terminated_early")
        pool = self.start_pool(sync_workers=1)
        req, reported = self._request("sync.terminated_early")

        req.terminate(pool)
        req.execute_using_pool(pool)

        assert reported.wait(10)
        assert not marks["started"].is_set()
        assert self.meta(req.id)["status"] == states.REVOKED

    def test_a_task_that_swallows_the_termination_is_still_stopped(self):
        started = threading.Event()

        @self.app.task(name="sync.swallows_it", shared=False)
        def swallows_it():
            started.set()
            try:
                while not self.release.is_set():
                    time.sleep(0.01)
            except BaseException:
                pass
            return "swallowed"

        pool = self.start_pool(sync_workers=1)
        rec = Recorder()
        task_id = self.apply(pool, "sync.swallows_it", rec)
        assert started.wait(10)

        assert pool.terminate_job(task_id) is True

        assert rec.done.wait(10)
        assert rec.results == []
        assert isinstance(rec.failure, Terminated)

    def test_terminating_a_task_storing_its_result_leaves_it_to_finish(self):
        storing = threading.Event()

        @self.app.task(name="sync.finishes_while_revoked", shared=False)
        def finishes_while_revoked():
            return "finished"

        backend_cls = type(self.app.backend)
        store = backend_cls.mark_as_done

        def slow_store(backend, *args, **kwargs):
            storing.set()
            for _ in range(10):
                time.sleep(0.01)
            return store(backend, *args, **kwargs)

        pool = self.start_pool(sync_workers=1)
        req, _ = self._request("sync.finishes_while_revoked")
        done = threading.Event()
        req.on_success = report_when_done(req.on_success, done)
        with patch.object(backend_cls, "mark_as_done", slow_store), collect_signal(signals.task_revoked) as revoked:
            req.execute_using_pool(pool)
            assert storing.wait(10)
            req.terminate(pool)
            assert done.wait(10)

        assert revoked == []
        assert self.meta(req.id)["status"] == states.SUCCESS

    def test_the_thread_runs_the_next_task_after_a_termination(self):
        marks = self._busy_task("sync.terminated_first")

        @self.app.task(name="sync.runs_next", shared=False)
        def runs_next():
            return "ok"

        pool = self.start_pool(sync_workers=1)
        first = Recorder()
        task_id = self.apply(pool, "sync.terminated_first", first)
        assert marks["started"].wait(10)

        assert pool.terminate_job(task_id) is True
        assert first.done.wait(10)
        assert isinstance(first.failure, Terminated)
        assert wait_until(lambda: not pool._active_futures)
        assert pool.terminate_job(task_id) is False

        second = Recorder()
        next_id = self.apply(pool, "sync.runs_next", second)
        assert second.done.wait(10)
        assert second.result[0] == 0
        assert self.meta(next_id)["result"] == "ok"


class test_async_task_time_limits(AioPoolCase):
    def test_a_hard_limit_still_fires_after_a_soft_one(self):
        soft_seen = threading.Event()

        @self.app.task(name="aio.stubborn", shared=False)
        async def stubborn():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                soft_seen.set()
            await asyncio.sleep(30)

        pool = self.start_pool()
        rec = Recorder()
        self.apply(pool, "aio.stubborn", rec, soft_timeout=0.2, timeout=0.6)

        assert rec.done.wait(10)
        assert soft_seen.is_set()
        assert rec.timeouts == [(False, 0.6)]
        assert rec.results == []
        assert rec.failures == []


class test_async_task_shutdown(AioPoolCase):
    def test_a_job_storing_its_result_is_left_to_finish(self):
        storing = threading.Event()

        @self.app.task(name="aio.finishes_at_shutdown", shared=False)
        async def finishes_at_shutdown():
            return "finished"

        backend_cls = type(self.app.backend)
        store = backend_cls.amark_as_done

        async def slow_store(backend, *args, **kwargs):
            storing.set()
            await asyncio.sleep(0.1)
            return await store(backend, *args, **kwargs)

        pool = self.start_pool()
        rec = Recorder()
        with patch.object(backend_cls, "amark_as_done", slow_store):
            task_id = self.apply(pool, "aio.finishes_at_shutdown", rec)
            assert storing.wait(10)

            pool.stop()

        assert rec.failures == []
        assert len(rec.results) == 1
        assert self.meta(task_id)["status"] == states.SUCCESS

    def test_a_job_still_running_its_body_is_cancelled(self):
        started, cancelled = threading.Event(), threading.Event()

        @self.app.task(name="aio.runs_into_shutdown", shared=False)
        async def runs_into_shutdown():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        pool = self.start_pool()
        rec = Recorder()
        self.apply(pool, "aio.runs_into_shutdown", rec)
        assert started.wait(10)

        pool.stop()

        assert cancelled.is_set()
        assert isinstance(rec.failure, Terminated)


class test_flush(AioPoolCase):
    def test_async_jobs_that_have_not_started_are_dropped(self):
        release = threading.Event()
        started = []

        @self.app.task(name="aio.holds_the_slot", shared=False)
        async def holds_the_slot():
            started.append("first")
            while not release.is_set():
                await asyncio.sleep(0.01)
            return "first"

        @self.app.task(name="aio.waits_for_the_slot", shared=False)
        async def waits_for_the_slot():
            started.append("second")
            return "second"

        pool = self.start_pool(loop_workers=1, loop_concurrency=1)
        first, second = Recorder(), Recorder()
        self.apply(pool, "aio.holds_the_slot", first)
        assert wait_until(lambda: started == ["first"])
        self.apply(pool, "aio.waits_for_the_slot", second)

        pool.flush()
        release.set()

        assert first.done.wait(10)
        assert wait_until(lambda: not pool._async_jobs)
        assert wait_until(lambda: pool._loop_workers[0]._active_count == 0)
        assert started == ["first"]
        assert len(first.results) == 1
        assert (second.accepted, second.results, second.failures) == ([], [], [])

    def test_sync_jobs_that_have_not_started_are_dropped(self):
        release = threading.Event()
        started = []

        @self.app.task(name="aio.holds_the_thread", shared=False)
        def holds_the_thread():
            started.append("first")
            release.wait(10)
            return "first"

        @self.app.task(name="aio.waits_for_the_thread", shared=False)
        def waits_for_the_thread():
            started.append("second")
            return "second"

        pool = self.start_pool(sync_workers=1)
        first, second = Recorder(), Recorder()
        self.apply(pool, "aio.holds_the_thread", first)
        assert wait_until(lambda: started == ["first"])
        self.apply(pool, "aio.waits_for_the_thread", second)

        pool.flush()
        release.set()

        assert first.done.wait(10)
        assert wait_until(lambda: not pool._active_futures)
        assert started == ["first"]
        assert len(first.results) == 1
        assert (second.accepted, second.results, second.failures) == ([], [], [])


class test_async_task_exits(AioPoolCase):
    @pytest.mark.parametrize("exc_type", [SystemExit, KeyboardInterrupt])
    def test_a_task_exiting_leaves_its_loop_worker_running(self, exc_type):
        @self.app.task(name="aio.exiting", shared=False)
        async def exiting():
            raise exc_type("from the task")

        @self.app.task(name="aio.after_the_exit", shared=False)
        async def after_the_exit():
            return "ran"

        pool = self.start_pool()
        rec = Recorder()
        self.apply(pool, "aio.exiting", rec)
        assert rec.done.wait(10)
        assert isinstance(rec.failure, WorkerLostError)
        assert pool._loop_workers[0]._thread.is_alive()

        after = Recorder()
        task_id = self.apply(pool, "aio.after_the_exit", after)
        assert after.done.wait(10)
        assert self.meta(task_id)["status"] == states.SUCCESS
        assert self.meta(task_id)["result"] == "ran"

    @pytest.mark.parametrize(
        ("exc", "flag", "expected"),
        [
            (WorkerShutdown(3), "should_stop", 3),
            (WorkerTerminate(2), "should_terminate", 2),
        ],
    )
    def test_a_shutdown_request_reaches_the_worker(self, exc, flag, expected):
        @self.app.task(name="aio.asks_for_shutdown", shared=False)
        async def asks_for_shutdown():
            raise exc

        pool = self.start_pool()
        rec = Recorder()
        try:
            self.apply(pool, "aio.asks_for_shutdown", rec)
            assert rec.done.wait(10)
            assert getattr(worker_state, flag) == expected
            assert isinstance(rec.failure, WorkerLostError)
            assert pool._loop_workers[0]._thread.is_alive()
        finally:
            worker_state.should_stop = None
            worker_state.should_terminate = None


class test_sync_task_time_limits(AioPoolCase):
    def test_a_soft_limit_is_raised_inside_the_task(self):
        @self.app.task(name="sync.soft_limit", shared=False)
        def sleeper():
            for _ in range(3000):
                time.sleep(0.01)

        pool = self.start_pool(sync_workers=1)
        rec = Recorder()
        task_id = self.apply(pool, "sync.soft_limit", rec, soft_timeout=0.2)

        assert rec.done.wait(10)
        failed, retval, _ = rec.result
        assert failed
        assert isinstance(retval.exception, SoftTimeLimitExceeded)
        assert self.meta(task_id)["status"] == states.FAILURE

    def test_a_hard_limit_is_reported_for_a_stuck_task(self):
        release = threading.Event()

        @self.app.task(name="sync.hard_limit", shared=False)
        def stuck():
            release.wait(10)

        pool = self.start_pool(sync_workers=1)
        pool.stuck_thread_grace = 0.1
        rec = Recorder()
        self.apply(pool, "sync.hard_limit", rec, timeout=0.3)
        try:
            assert rec.done.wait(10)
            assert rec.timeouts == [(False, 0.3)]
            assert wait_until(lambda: pool._stuck_thread_count == 1)
        finally:
            release.set()
        assert wait_until(lambda: not pool._active_futures)
        assert rec.results == []
        assert rec.failures == []

    def test_a_hard_limit_stops_a_task_running_python_code(self):
        completed = threading.Event()

        @self.app.task(name="sync.runs_past_its_limit", shared=False)
        def runs_past_its_limit():
            for _ in range(3000):
                time.sleep(0.01)
            completed.set()

        pool = self.start_pool(sync_workers=1)
        rec = Recorder()
        self.apply(pool, "sync.runs_past_its_limit", rec, timeout=0.3)

        assert rec.done.wait(10)
        assert rec.timeouts == [(False, 0.3)]
        assert wait_until(lambda: not pool._active_futures)
        assert not completed.is_set()
        assert pool._stuck_thread_count == 0
        assert rec.results == []
        assert rec.failures == []

    def test_a_soft_limit_does_not_land_while_the_result_is_stored(self):
        @self.app.task(name="sync.stores_slowly", shared=False)
        def stores_slowly():
            return "ok"

        backend_cls = type(self.app.backend)
        store = backend_cls.mark_as_done

        def slow_store(backend, *args, **kwargs):
            for _ in range(50):
                time.sleep(0.01)
            return store(backend, *args, **kwargs)

        pool = self.start_pool(sync_workers=1)
        rec = Recorder()
        with patch.object(backend_cls, "mark_as_done", slow_store):
            task_id = self.apply(pool, "sync.stores_slowly", rec, soft_timeout=0.2)
            assert rec.done.wait(10)

        assert rec.result[0] == 0
        assert self.meta(task_id)["status"] == states.SUCCESS

    def test_a_soft_limit_does_not_land_in_the_next_task(self):
        @self.app.task(name="sync.slow_one", shared=False)
        def slow_one():
            for _ in range(3000):
                time.sleep(0.01)

        @self.app.task(name="sync.quick_one", shared=False)
        def quick_one():
            return "ok"

        pool = self.start_pool(sync_workers=1)
        first = Recorder()
        self.apply(pool, "sync.slow_one", first, soft_timeout=0.2)
        assert first.done.wait(10)

        second = Recorder()
        task_id = self.apply(pool, "sync.quick_one", second)
        assert second.done.wait(10)
        assert second.result[0] == 0
        assert self.meta(task_id)["status"] == states.SUCCESS
        assert self.meta(task_id)["result"] == "ok"

    @pytest.mark.parametrize("limit", ["soft_timeout", "timeout"])
    def test_a_queued_task_is_timed_from_when_it_starts(self, limit):
        release = threading.Event()

        @self.app.task(name="sync.holds_the_thread", shared=False)
        def holds_the_thread():
            release.wait(10)

        @self.app.task(name="sync.queued_behind_it", shared=False)
        def queued_behind_it():
            for _ in range(5):
                time.sleep(0.01)
            return "ok"

        pool = self.start_pool(sync_workers=1)
        second = Recorder()
        try:
            self.apply(pool, "sync.holds_the_thread", Recorder())
            task_id = self.apply(pool, "sync.queued_behind_it", second, **{limit: 0.3})
            assert not second.done.wait(0.6)
        finally:
            release.set()

        assert second.done.wait(10)
        assert second.timeouts == []
        assert second.result[0] == 0
        assert self.meta(task_id)["result"] == "ok"
        assert pool._stuck_thread_count == 0


class test_sync_threads_all_stuck(AioPoolCase):
    @pytest.mark.parametrize(("sync_workers", "all_stuck"), [(1, True), (2, False)])
    def test_tells_whether_a_queued_sync_task_can_start(self, sync_workers, all_stuck):
        release = threading.Event()

        @self.app.task(name="sync.stuck_on_a_thread", shared=False)
        def stuck_on_a_thread():
            release.wait(10)

        pool = self.start_pool(sync_workers=sync_workers)
        pool.stuck_thread_grace = 0.1
        rec = Recorder()
        assert not pool.sync_threads_all_stuck
        self.apply(pool, "sync.stuck_on_a_thread", rec, timeout=0.2)
        try:
            assert rec.done.wait(10)
            assert wait_until(lambda: pool.stuck_threads == 1)
            assert pool.sync_threads_all_stuck is all_stuck
        finally:
            release.set()
        assert wait_until(lambda: pool.stuck_threads == 0)
        assert not pool.sync_threads_all_stuck


class test_sync_task_stop(AioPoolCase):
    def test_the_stop_waits_for_a_running_task_to_report(self):
        started = threading.Event()

        @self.app.task(name="sync.running_at_the_stop", shared=False)
        def running_at_the_stop():
            started.set()
            time.sleep(0.3)
            return "done"

        pool = self.start_pool(sync_workers=1)
        rec = Recorder()
        task_id = self.apply(pool, "sync.running_at_the_stop", rec)
        assert started.wait(10)

        pool.stop()

        assert rec.result[0] == 0
        assert self.meta(task_id)["result"] == "done"

    def test_the_stop_leaves_a_stuck_task_behind(self):
        release = threading.Event()

        @self.app.task(name="sync.stuck_at_the_stop", shared=False)
        def stuck_at_the_stop():
            release.wait(10)

        pool = self.start_pool(sync_workers=1)
        pool.stuck_thread_grace = 0.1
        rec = Recorder()
        self.apply(pool, "sync.stuck_at_the_stop", rec, timeout=0.2)
        try:
            assert rec.done.wait(10)
            stopping = threading.Thread(target=pool.stop)
            stopping.start()
            stopping.join(5)

            assert not stopping.is_alive()
            assert pool.stuck_threads == 1
        finally:
            release.set()
        assert wait_until(lambda: pool.stuck_threads == 0)


def from_another_thread(fn, *args):
    result = []
    thread = threading.Thread(target=lambda: result.append(fn(*args)))
    thread.start()
    thread.join(10)
    return result[0]


class test_SyncJob:
    @pytest.fixture(autouse=True)
    def inject(self):
        with patch("celery.concurrency.aio._raise_in_thread", return_value=1) as inject:
            yield inject

    def started_job(self):
        job = SyncJob("job-1", soft_timeout=None, timeout=None, timeout_callback=None)
        job.start(Mock(name="target"), Mock(name="on_hard_limit"))
        return job

    def test_the_limit_timers_stop_when_the_task_finishes(self):
        timers = []

        class _Timer(threading.Timer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                timers.append(self)

        job = SyncJob("job-1", soft_timeout=30.0, timeout=30.0, timeout_callback=None)
        with patch("threading.Timer", _Timer):
            guarded = job.start(Mock(return_value="ok"), Mock(name="on_hard_limit"))
        assert len(timers) == 2
        assert all(timer.is_alive() for timer in timers)

        assert guarded() == "ok"

        for timer in timers:
            timer.join(timeout=5)
        assert not any(timer.is_alive() for timer in timers)
        assert job.finished.is_set()

    def test_the_hard_limit_leaves_a_finished_task_alone(self):
        job = self.started_job()
        job.finish()

        assert from_another_thread(job.expire) is False
        assert not job.timed_out

    def test_an_interrupt_before_the_body_is_raised_as_the_body_starts(self, inject):
        job = self.started_job()

        assert from_another_thread(job.interrupt, _TaskTerminated) is True
        with pytest.raises(_TaskTerminated):
            job.open()
        job.close()

        inject.assert_not_called()

    @pytest.mark.parametrize("end", ["close", "finish"])
    def test_an_interrupt_that_has_not_landed_is_dropped(self, inject, end):
        job = self.started_job()
        job.open()

        assert from_another_thread(job.interrupt, SoftTimeLimitExceeded) is True
        getattr(job, end)()

        thread_id = threading.get_ident()
        assert inject.call_args_list == [call(thread_id, SoftTimeLimitExceeded), call(thread_id, None)]
        assert from_another_thread(job.interrupt, _TaskTerminated) is False

    def test_an_interrupt_from_the_task_thread_waits_for_the_body_to_end(self, inject):
        job = self.started_job()
        job.open()

        assert job.interrupt(_TaskTerminated) is True
        with pytest.raises(_TaskTerminated):
            job.close()

        inject.assert_not_called()

    def test_a_termination_replaces_a_soft_limit(self, inject):
        job = self.started_job()
        job.open()

        assert from_another_thread(job.interrupt, SoftTimeLimitExceeded) is True
        assert from_another_thread(job.interrupt, _TaskTerminated) is True

        thread_id = threading.get_ident()
        assert inject.call_args_list == [call(thread_id, SoftTimeLimitExceeded), call(thread_id, _TaskTerminated)]

    def test_a_terminated_task_is_terminated_once_and_does_not_time_out(self, inject):
        job = self.started_job()
        job.open()

        assert from_another_thread(job.interrupt, _TaskTerminated) is True
        assert from_another_thread(job.interrupt, _TaskTerminated) is True
        assert from_another_thread(job.interrupt, SoftTimeLimitExceeded) is False
        assert from_another_thread(job.expire) is True

        assert not job.timed_out
        assert inject.call_args_list == [call(threading.get_ident(), _TaskTerminated)]

    def test_a_task_past_its_hard_limit_can_no_longer_be_terminated(self, inject):
        job = self.started_job()
        job.open()

        assert from_another_thread(job.expire) is True
        assert from_another_thread(job.interrupt, _TaskTerminated) is False

        assert job.timed_out
        assert inject.call_args_list == [call(threading.get_ident(), _TaskTimedOut)]


class test_AsyncApplyResult:
    def test_terminate_cancels_on_the_owning_loop(self):
        worker = Mock(name="worker")
        job = AsyncApplyResult(worker, "job-id", Mock(name="on_done"))
        task = Mock(name="task")
        job.attach(task)

        job.terminate()

        worker.cancel_task.assert_called_once_with(task)
        task.cancel.assert_not_called()

    def test_a_job_terminated_before_it_starts_is_cancelled_on_arrival(self):
        worker = Mock(name="worker")
        job = AsyncApplyResult(worker, "job-id", Mock(name="on_done"))
        job.terminate()

        task = Mock(name="task")
        job.attach(task)

        task.cancel.assert_called_once_with()

    def test_terminating_twice_cancels_once(self):
        worker = Mock(name="worker")
        job = AsyncApplyResult(worker, "job-id", Mock(name="on_done"))
        task = Mock(name="task")
        job.attach(task)

        job.terminate()
        job.cancel()

        assert worker.cancel_task.call_count == 1

    def test_the_finished_task_is_handed_back(self):
        on_done = Mock(name="on_done")
        job = AsyncApplyResult(Mock(name="worker"), "job-id", on_done)
        task = Mock(name="task")
        job.attach(task)

        task.add_done_callback.call_args[0][0](task)

        on_done.assert_called_once_with(job)
        assert job._task is None

    def test_a_job_discarded_before_it_is_scheduled_is_cancelled_on_arrival(self):
        job = AsyncApplyResult(Mock(name="worker"), "job-id", Mock(name="on_done"))
        job.discard()

        task = Mock(name="task")
        job.attach(task)

        task.cancel.assert_called_once_with()

    def test_terminate_reports_the_job_cancelled(self):
        job = AsyncApplyResult(Mock(name="worker"), "job-id", Mock(name="on_done"))
        job.attach(Mock(name="task"))

        assert job.terminate() is True
        assert job.terminate() is True


class test_process_signals:
    def test_a_loop_worker_signals_its_own_lifetime(self):
        with collect_signal(signals.worker_process_init) as started:
            with collect_signal(signals.worker_process_shutdown) as stopped:
                worker = LoopWorker(concurrency=1, app=Mock(), index=0)
                worker.start()
                try:
                    assert len(started) == 1
                    assert stopped == []
                finally:
                    worker.stop()
                assert len(stopped) == 1

        assert stopped[0]["sender"] is None
        assert stopped[0]["pid"] == os.getpid()
        assert stopped[0]["exitcode"] == 0

    def test_every_loop_worker_of_a_pool_is_signalled(self):
        pool = TaskPool(10, app=Mock(), loop_workers=2)
        with collect_signal(signals.worker_process_init) as started:
            with collect_signal(signals.worker_process_shutdown) as stopped:
                pool.start()
                try:
                    assert len(started) == 2
                    assert stopped == []
                finally:
                    pool.stop()
                assert len(stopped) == 2
