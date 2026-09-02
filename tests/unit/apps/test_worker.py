import asyncio
import signal
import threading
from unittest.mock import Mock

import pytest

from celery import platforms, signals
from celery.apps.worker import Worker, install_worker_int_handler, install_worker_term_handler, on_cold_shutdown
from celery.platforms import EX_FAILURE, EX_OK, current_process
from celery.worker import components, state
from celery.worker.components import stop_pool


class WorkerCase:
    def create_worker(self, **kwargs):
        return Worker(app=self.app, hostname="worker@example.com", quiet=True, **kwargs)


class test_Worker_on_start(WorkerCase):
    async def test_purges_through_the_async_control_api(self):
        # Blueprint.start calls on_start on the worker's own event loop, and
        # the blocking Control.purge() cannot run there: it asks the loop it
        # is running on to run the purge for it.
        worker = self.create_worker(purge=True)
        purged = []

        async def apurge(connection=None):
            purged.append(connection)
            return 2

        worker.app.control.apurge = apurge
        worker.app.control.purge = Mock(name="purge")

        await worker.purge_messages()

        assert purged == [None]
        worker.app.control.purge.assert_not_called()

    async def test_purges_before_touching_the_process(self):
        worker = self.create_worker(purge=True)
        calls = []

        async def purge_messages():
            calls.append("purge")

        worker.purge_messages = purge_messages
        worker.set_process_status = calls.append
        worker.install_platform_tweaks = lambda w: calls.append("tweaks")

        await worker.on_start()

        assert calls == ["purge", "-active-", "tweaks"]

    async def test_leaves_the_queue_alone_without_purge(self):
        worker = self.create_worker(purge=False)
        worker.app.control.apurge = Mock(name="apurge")
        worker.set_process_status = Mock(name="set_process_status")
        worker.install_platform_tweaks = Mock(name="install_platform_tweaks")

        await worker.on_start()

        worker.app.control.apurge.assert_not_called()


class test_Worker_blueprint(WorkerCase):
    async def test_awaits_a_coroutine_on_start(self):
        # An unawaited on_start() left the worker running with none of the
        # work it does, and Python only logged a warning about it.
        worker = self.create_worker()
        started = []

        async def on_start():
            started.append(True)

        worker.blueprint.on_start = on_start

        await worker.blueprint.start(Mock(steps=[]))

        assert started == [True]


class FakePool:
    def __init__(self, block=None):
        self.block = block
        self.stopped_on = None

    def stop(self):
        self.stopped_on = threading.current_thread()
        if self.block is not None:
            self.block.wait(5)

    def terminate(self):
        pass


class test_stop_pool:
    async def test_stops_the_pool_off_the_calling_thread(self):
        pool = FakePool()

        assert await stop_pool(pool) is True
        assert pool.stopped_on is not threading.current_thread()

    async def test_the_loop_keeps_running_while_the_pool_comes_down(self):
        # The pool joins each of its threads for up to ten seconds. Doing that
        # on the loop thread stalls the rest of the shutdown for as long.
        release = threading.Event()
        ticks = []

        async def tick():
            for _ in range(3):
                await asyncio.sleep(0.01)
                ticks.append(1)
            release.set()

        stopped, _ = await asyncio.gather(stop_pool(FakePool(release), timeout=5), tick())

        assert stopped is True
        assert ticks == [1, 1, 1]

    async def test_gives_up_on_a_pool_that_will_not_come_down(self):
        release = threading.Event()
        try:
            assert await stop_pool(FakePool(release), timeout=0.1) is False
        finally:
            release.set()


class test_Pool_bootstep:
    def make_step(self):
        step = components.Pool(Mock(name="worker"))
        return step

    async def test_stop_does_not_join_on_the_loop_thread(self):
        w = Mock(name="worker", pool=FakePool())

        await self.make_step().stop(w)

        assert w.pool.stopped_on is not threading.current_thread()

    async def test_terminate_stops_the_pool(self):
        w = Mock(name="worker", pool=FakePool())

        await self.make_step().terminate(w)

        assert w.pool.stopped_on is not threading.current_thread()


class test_shutdown_signals(WorkerCase):
    @pytest.fixture(autouse=True)
    def _restore(self):
        names = ("SIGTERM", "SIGINT", "SIGQUIT")
        saved = {name: platforms.signals[name] for name in names}
        # Independent of whichever name the process happens to carry.
        process_name = current_process()._name
        current_process()._name = "MainProcess"
        yield
        current_process()._name = process_name
        for name, handler in saved.items():
            platforms.signals[name] = handler
        state.should_stop = None
        state.should_terminate = None

    def raise_signal(self, name):
        platforms.signals[name](getattr(signal, name), None)

    def test_sigterm_announces_a_warm_shutdown(self):
        install_worker_term_handler(self.create_worker())
        announced = []

        def on_shutting_down(**kwargs):
            announced.append(kwargs)

        signals.worker_shutting_down.connect(on_shutting_down)
        try:
            self.raise_signal("SIGTERM")
        finally:
            signals.worker_shutting_down.disconnect(on_shutting_down)

        assert [(a["sig"], a["how"], a["exitcode"]) for a in announced] == [("SIGTERM", "Warm", EX_OK)]
        assert state.should_stop == EX_OK
        assert state.should_terminate is None

    def test_the_second_sigint_shuts_down_cold(self):
        worker = self.create_worker()
        install_worker_int_handler(worker)
        warm = platforms.signals["SIGINT"]

        self.raise_signal("SIGINT")

        cold = platforms.signals["SIGINT"]
        assert cold is not warm
        assert state.should_stop == EX_FAILURE
        assert state.should_terminate is None

        self.raise_signal("SIGINT")

        assert state.should_terminate == EX_FAILURE

    async def test_the_cold_shutdown_stops_the_pool_off_the_loop(self):
        worker = self.create_worker()
        worker.consumer = Mock(name="consumer")
        worker.consumer.pool = FakePool()

        on_cold_shutdown(worker)

        # Scheduled, not joined here: this runs on the worker's event loop.
        assert worker.consumer.pool.stopped_on is None
        for _ in range(200):
            await asyncio.sleep(0.01)
            if worker.consumer.pool.stopped_on is not None:
                break
        assert worker.consumer.pool.stopped_on is not threading.current_thread()
