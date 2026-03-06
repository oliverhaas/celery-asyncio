import sys

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from celery.apps.worker import safe_say
from celery.bootsteps import CLOSE, RUN, TERMINATE, StartStopStep
from celery.exceptions import ImproperlyConfigured, WorkerShutdown, WorkerTerminate
from celery.platforms import EX_FAILURE
from celery.utils.nodenames import worker_direct
from celery.utils.scheduling import Timer
from celery.worker import components, state
from celery.worker import worker as worker_module
from celery.worker.request import Request


def MockStep(step=None):
    if step is None:
        step = Mock(name="step")
    else:
        step.blueprint = Mock(name="step.blueprint")
    step.blueprint.name = "MockNS"
    step.name = f"MockStep({id(step)})"
    return step


class ConsumerCase:
    def create_task_message(self, channel, *args, **kwargs):
        m = self.TaskMessage(*args, **kwargs)
        m.channel = channel
        m.delivery_info = {"consumer_tag": "mock"}
        return m


class test_WorkController(ConsumerCase):
    def setup_method(self):
        self.worker = self.create_worker()
        self._logger = worker_module.logger
        self._comp_logger = components.logger
        self.logger = worker_module.logger = Mock()
        self.comp_logger = components.logger = Mock()

        @self.app.task(shared=False)
        def foo_task(x, y, z):
            return x * y * z

        self.foo_task = foo_task

    def teardown_method(self):
        worker_module.logger = self._logger
        components.logger = self._comp_logger

    def create_worker(self, **kw):
        worker = self.app.WorkController(concurrency=1, loglevel=0, **kw)
        worker.blueprint.shutdown_complete.set()
        return worker

    def test_on_consumer_ready(self):
        self.worker.on_consumer_ready(Mock())

    def test_setup_queues_worker_direct(self):
        self.app.conf.worker_direct = True
        self.app.amqp.__dict__["queues"] = Mock()
        self.worker.setup_queues({})
        self.app.amqp.queues.select_add.assert_called_with(
            worker_direct(self.worker.hostname),
        )

    def test_setup_queues__missing_queue(self):
        self.app.amqp.queues.select = Mock(name="select")
        self.app.amqp.queues.deselect = Mock(name="deselect")
        self.app.amqp.queues.select.side_effect = KeyError()
        self.app.amqp.queues.deselect.side_effect = KeyError()
        with pytest.raises(ImproperlyConfigured):
            self.worker.setup_queues("x,y", exclude="foo,bar")
        self.app.amqp.queues.select = Mock(name="select")
        with pytest.raises(ImproperlyConfigured):
            self.worker.setup_queues("x,y", exclude="foo,bar")

    def test_send_worker_shutdown(self):
        with patch("celery.signals.worker_shutdown") as ws:
            self.worker._send_worker_shutdown()
            ws.send.assert_called_with(sender=self.worker)

    async def test_shutdown_no_blueprint(self):
        self.worker.blueprint = None
        await self.worker._shutdown()

    @patch("celery.worker.worker.create_pidlock")
    async def test_use_pidfile(self, create_pidlock):
        create_pidlock.return_value = Mock()
        worker = self.create_worker(pidfile="pidfilelockfilepid")
        worker.steps = []
        await worker.start()
        create_pidlock.assert_called()
        worker.consumer = AsyncMock()
        await worker.stop()
        worker.pidlock.release.assert_called()

    def test_attrs(self):
        worker = self.worker
        assert worker.timer is not None
        assert isinstance(worker.timer, Timer)
        assert worker.pool is not None
        assert worker.consumer is not None
        assert worker.steps

    def test_with_embedded_beat(self):
        worker = self.app.WorkController(concurrency=1, loglevel=0, beat=True)
        assert worker.beat
        assert worker.beat in [w.obj for w in worker.steps]

    async def test_dont_stop_or_terminate(self):
        worker = self.app.WorkController(concurrency=1, loglevel=0)
        # stop/terminate on non-RUN worker should not crash
        await worker.stop()
        assert worker.blueprint.state != CLOSE
        await worker.terminate()
        assert worker.blueprint.state != CLOSE

    def test_process_task(self):
        worker = self.worker
        worker.pool = Mock()
        channel = Mock()
        m = self.create_task_message(
            channel,
            self.foo_task.name,
            args=[4, 8, 10],
            kwargs={},
        )
        task = Request(m, app=self.app)
        worker._process_task(task)
        assert worker.pool.apply_async.call_count == 1
        worker.pool.stop()

    def test_process_task_raise_base(self):
        worker = self.worker
        worker.pool = Mock()
        worker.pool.apply_async.side_effect = KeyboardInterrupt("Ctrl+C")
        channel = Mock()
        m = self.create_task_message(
            channel,
            self.foo_task.name,
            args=[4, 8, 10],
            kwargs={},
        )
        task = Request(m, app=self.app)
        worker.steps = []
        worker.blueprint.state = RUN
        with pytest.raises(KeyboardInterrupt):
            worker._process_task(task)

    def test_process_task_raise_WorkerTerminate(self):
        worker = self.worker
        worker.pool = Mock()
        worker.pool.apply_async.side_effect = WorkerTerminate()
        channel = Mock()
        m = self.create_task_message(
            channel,
            self.foo_task.name,
            args=[4, 8, 10],
            kwargs={},
        )
        task = Request(m, app=self.app)
        worker.steps = []
        worker.blueprint.state = RUN
        with pytest.raises(SystemExit):
            worker._process_task(task)

    def test_process_task_raise_regular(self):
        worker = self.worker
        worker.pool = Mock()
        worker.pool.apply_async.side_effect = KeyError("some exception")
        channel = Mock()
        m = self.create_task_message(
            channel,
            self.foo_task.name,
            args=[4, 8, 10],
            kwargs={},
        )
        task = Request(m, app=self.app)
        with pytest.raises(KeyError):
            worker._process_task(task)
        worker.pool.stop()

    async def test_start_catches_base_exceptions(self):
        worker1 = self.create_worker()
        worker1.blueprint.state = RUN
        stc = MockStep()
        stc.start = AsyncMock(side_effect=WorkerTerminate())
        stc.terminate = AsyncMock()
        stc.stop = AsyncMock()
        stc.close = Mock()
        worker1.steps = [stc]
        await worker1.start()
        stc.start.assert_called_with(worker1)
        assert stc.terminate.call_count

        worker2 = self.create_worker()
        worker2.blueprint.state = RUN
        sec = MockStep()
        sec.start = AsyncMock(side_effect=WorkerShutdown())
        sec.terminate = None
        sec.stop = AsyncMock()
        sec.close = Mock()
        worker2.steps = [sec]
        await worker2.start()
        assert sec.stop.call_count

    def test_statedb(self):
        from celery.worker import state

        Persistent = state.Persistent

        state.Persistent = Mock()
        try:
            worker = self.create_worker(statedb="statefilename")
            assert worker._persistence
        finally:
            state.Persistent = Persistent

    async def test_signal_consumer_close(self):
        worker = self.worker
        worker.consumer = AsyncMock()

        await worker.signal_consumer_close()
        worker.consumer.close.assert_called_with()

        worker.consumer = AsyncMock()
        worker.consumer.close.side_effect = AttributeError()
        await worker.signal_consumer_close()

    def test_rusage__no_resource(self):
        from celery.worker import worker

        prev, worker.resource = worker.resource, None
        try:
            self.worker.pool = Mock(name="pool")
            with pytest.raises(NotImplementedError):
                self.worker.rusage()
            self.worker.stats()
        finally:
            worker.resource = prev

    def test_repr(self):
        assert repr(self.worker)

    def test_str(self):
        assert str(self.worker) == self.worker.hostname

    async def test_start__stop(self):
        worker = self.worker
        worker.blueprint.shutdown_complete.set()
        worker.steps = [MockStep(StartStopStep(self)) for _ in range(4)]
        worker.blueprint.state = RUN
        worker.blueprint.started = 4
        for w in worker.steps:
            w.start = AsyncMock()
            w.close = Mock()
            w.stop = AsyncMock()

        await worker.start()
        for w in worker.steps:
            w.start.assert_called()
        worker.consumer = AsyncMock()
        await worker.stop(exitcode=3)
        for stopstep in worker.steps:
            stopstep.close.assert_called()
            stopstep.stop.assert_called()

        # Doesn't close pool if no pool.
        await worker.start()
        worker.pool = None
        worker.consumer = AsyncMock()
        await worker.stop()

        # test that stop of None is not attempted
        worker.steps[-1] = None
        await worker.start()
        worker.consumer = AsyncMock()
        await worker.stop()

    async def test_start__KeyboardInterrupt(self):
        worker = self.worker
        worker.blueprint = Mock(name="blueprint")
        worker.blueprint.start = AsyncMock(side_effect=KeyboardInterrupt())
        worker.stop = AsyncMock(name="stop")
        await worker.start()
        worker.stop.assert_called_with(exitcode=EX_FAILURE)

    async def test_step_raises(self):
        worker = self.worker
        step = Mock()
        step.start = AsyncMock(side_effect=TypeError())
        worker.steps = [step]
        worker.stop = AsyncMock()
        await worker.start()
        worker.stop.assert_called_with(exitcode=EX_FAILURE)

    def test_state(self):
        assert self.worker.state

    async def test_start__terminate(self):
        worker = self.worker
        worker.blueprint.shutdown_complete.set()
        worker.blueprint.started = 5
        worker.blueprint.state = RUN
        worker.steps = [MockStep() for _ in range(5)]
        for s in worker.steps:
            s.start = AsyncMock()
            s.stop = AsyncMock()
            s.terminate = AsyncMock()
            s.close = Mock()
        await worker.start()
        for w in worker.steps[:3]:
            w.start.assert_called()
        assert worker.blueprint.started == len(worker.steps)
        assert worker.blueprint.state == RUN
        worker.consumer = AsyncMock()
        await worker.terminate()
        for step in worker.steps:
            step.terminate.assert_called()
        worker.blueprint.state = TERMINATE
        await worker.terminate()

    def test_Pool_pool_no_sem(self):
        w = Mock()
        w.pool_cls.uses_semaphore = False
        components.Pool(w).create(w)
        assert w.process_task is w._process_task

    async def test_wait_for_soft_shutdown(self):
        worker = self.worker
        worker.app.conf.worker_soft_shutdown_timeout = 10
        request = Mock(name="task", id="1234213")
        state.task_accepted(request)
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            async def clear_active(*args):
                state.active_requests.discard(request)
            mock_sleep.side_effect = clear_active
            await worker.wait_for_soft_shutdown()
            mock_sleep.assert_called_with(0.5)

    async def test_wait_for_soft_shutdown_no_tasks(self):
        worker = self.worker
        worker.app.conf.worker_soft_shutdown_timeout = 10
        worker.app.conf.worker_enable_soft_shutdown_on_idle = True
        state.active_requests.clear()
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await worker.wait_for_soft_shutdown()
            # enters the if block (active=True) but while loop exits
            # immediately since active_requests is empty
            mock_sleep.assert_not_called()

    async def test_wait_for_soft_shutdown_no_wait(self):
        worker = self.worker
        request = Mock(name="task", id="1234213")
        state.task_accepted(request)
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await worker.wait_for_soft_shutdown()
            mock_sleep.assert_not_called()

    async def test_wait_for_soft_shutdown_no_wait_no_tasks(self):
        worker = self.worker
        worker.app.conf.worker_enable_soft_shutdown_on_idle = True
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await worker.wait_for_soft_shutdown()
            mock_sleep.assert_not_called()


class test_WorkerApp:
    def test_safe_say_defaults_to_stderr(self, capfd):
        safe_say("hello")
        captured = capfd.readouterr()
        assert captured.err == "\nhello\n"
        assert captured.out == ""

    def test_safe_say_writes_to_std_out(self, capfd):
        safe_say("out", sys.stdout)
        captured = capfd.readouterr()
        assert captured.out == "\nout\n"
        assert captured.err == ""
