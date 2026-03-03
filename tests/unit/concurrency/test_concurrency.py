import asyncio
import os
import threading
import time
from itertools import count
from unittest.mock import Mock, patch

import pytest

from celery import concurrency
from celery.concurrency.aio import ApplyResult, LoopWorker, TaskPool
from celery.concurrency.base import BasePool, apply_target
from celery.exceptions import WorkerShutdown, WorkerTerminate


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
        expected_pool_names = (
            "asyncio",
            "solo",
            "threads",
        )
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
            # Give time for cleanup
            time.sleep(0.1)
            assert w._active_count == 0
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

        # All equal — picks first (stable min)
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
