import time
from collections import deque

import pytest
from kombu.common import QoS

from celery.bootsteps import CLOSE, RUN
from celery.utils.scheduling import Timer
from celery.worker import loops, state
from celery.worker.loops import _check_restart_conditions, _enter_draining, asynloop


class BlueprintState:
    """Stand-in for the consumer blueprint: the loop only reads ``state``."""

    def __init__(self, initial=RUN):
        self.state = initial


class ScriptedConnection:
    """Plays a scripted sequence of ``drain_events()`` outcomes.

    An entry is raised when it is an exception and called otherwise, one entry
    per pass through the loop body. The script running out ends the loop.
    """

    def __init__(self, blueprint, *script):
        self.blueprint = blueprint
        self.script = deque(script)
        self.drained = 0

    async def drain_events(self, timeout=None):
        if timeout == 0:
            raise TimeoutError
        if not self.script:
            self.blueprint.state = CLOSE
            raise TimeoutError
        self.drained += 1
        step = self.script.popleft()
        if isinstance(step, BaseException):
            raise step
        step()


class FakeTaskConsumer:
    """Stand-in for the kombu task consumer the loop drives."""

    def __init__(self):
        self.callback = None
        self.consuming = False
        self.cancelled = False

    def register_callback(self, fun):
        self.callback = fun

    async def consume(self):
        self.consuming = True

    async def cancel(self):
        self.cancelled = True


class FakeConsumer:
    """Stand-in for :class:`celery.worker.consumer.Consumer`."""

    def __init__(self, app, timer=None, pool=None):
        self.app = app
        self.timer = timer if timer is not None else Timer(max_interval=0.01)
        self.pool = pool
        self.ready = False
        self.received = []

    def create_task_handler(self):
        return self.received.append

    def on_ready(self):
        self.ready = True


class FakePoolHandle:
    """Stand-in for the pool's ApplyResult: ``cancel()`` reports whether it took."""

    def __init__(self, releases):
        self.releases = releases
        self.cancels = 0

    def cancel(self):
        self.cancels += 1
        return self.releases


class FakeRequest:
    """Stand-in for :class:`celery.worker.request.Request`."""

    def __init__(self, id, handle=None):
        self.id = id
        self.name = "tests.add"
        self.handle = handle
        self.rejected = None
        # Request stores a weakref to the pool handle, so a callable it is.
        self._apply_result = None if handle is None else (lambda: self.handle)

    def reject(self, requeue=False):
        self.rejected = requeue


class FakePool:
    def __init__(self, stuck_thread_count=0):
        self._stuck_thread_count = stuck_thread_count


class LoopCase:
    def make_loop(self, *script, qos=None, timer=None, pool=None, blueprint=None):
        blueprint = blueprint if blueprint is not None else BlueprintState()
        obj = FakeConsumer(self.app, timer=timer, pool=pool)
        connection = ScriptedConnection(blueprint, *script)
        task_consumer = FakeTaskConsumer()
        coro = asynloop(obj, connection, task_consumer, blueprint, qos)
        return coro, obj, connection, task_consumer, blueprint


class test_asynloop_startup(LoopCase):
    async def test_registers_the_handler_and_announces_readiness(self):
        coro, obj, connection, task_consumer, blueprint = self.make_loop()

        await coro

        assert task_consumer.consuming
        assert obj.ready
        assert task_consumer.callback is not None

        message = object()
        task_consumer.callback(None, message)
        assert obj.received == [message]


class test_asynloop_connection_errors(LoopCase):
    async def test_broker_oserror_propagates_while_the_blueprint_runs(self):
        # Swallowing it returned into Consumer.start with the blueprint still
        # in RUN, which started a second set of bootsteps on top of the first.
        blueprint = BlueprintState()
        coro, _, connection, _, _ = self.make_loop(
            ConnectionResetError("broker went away"),
            blueprint=blueprint,
        )

        with pytest.raises(ConnectionResetError):
            await coro

        assert blueprint.state == RUN
        assert connection.drained == 1

    async def test_broker_oserror_ends_the_loop_once_the_blueprint_stopped(self):
        blueprint = BlueprintState()

        def close_then_fail():
            blueprint.state = CLOSE
            raise ConnectionResetError("broker went away during shutdown")

        coro, _, _, _, _ = self.make_loop(close_then_fail, blueprint=blueprint)

        await coro

        assert blueprint.state == CLOSE

    async def test_a_drain_timeout_is_not_an_error(self):
        coro, _, connection, _, _ = self.make_loop(TimeoutError(), TimeoutError())

        await coro

        assert connection.drained == 2


class test_asynloop_qos(LoopCase):
    async def test_pushes_the_prefetch_count_back_down_after_an_eta_task(self):
        applied = []
        pushed = []

        async def basic_qos(prefetch_count=None):
            pushed.append(prefetch_count)

        qos = QoS(basic_qos, 2)
        await qos.update()
        assert pushed == [2]

        timer = Timer(max_interval=0.01)

        def apply_eta_task():
            applied.append("eta")
            qos.decrement_eventually()

        # What strategy.default does for a task with an ETA: hold a prefetch
        # slot open for it and hand it to the timer.
        qos.increment_eventually()
        timer.call_at(time.time() - 1, apply_eta_task)

        coro, _, _, _, _ = self.make_loop(lambda: None, qos=qos, timer=timer)
        await coro

        assert applied == ["eta"]
        # Up while the task waited on the timer, back down once it ran. Before
        # the loop pushed changes, the increment held a slot for good.
        assert pushed == [2, 3, 2]
        assert qos.value == 2

    async def test_leaves_the_channel_alone_while_the_count_is_unchanged(self):
        pushed = []

        async def basic_qos(prefetch_count=None):
            pushed.append(prefetch_count)

        qos = QoS(basic_qos, 4)
        await qos.update()

        coro, _, _, _, _ = self.make_loop(lambda: None, lambda: None, qos=qos)
        await coro

        assert pushed == [4]

    async def test_runs_without_a_qos(self):
        coro, _, connection, _, _ = self.make_loop(lambda: None, qos=None)

        await coro

        assert connection.drained == 1


class test_enter_draining:
    async def test_requeues_only_the_tasks_the_pool_released(self):
        released = FakeRequest("released", FakePoolHandle(releases=True))
        queued = FakeRequest("queued", FakePoolHandle(releases=False))
        running = FakeRequest("running", FakePoolHandle(releases=False))
        for req in (released, queued, running):
            state.task_reserved(req)
        state.task_accepted(running)
        task_consumer = FakeTaskConsumer()

        await _enter_draining(task_consumer, "max tasks per child (1) reached")

        assert task_consumer.cancelled
        assert released.rejected is True
        # Requeuing these would run them here as well as on the worker that
        # gets the redelivery.
        assert queued.rejected is None
        assert running.rejected is None
        assert running.handle.cancels == 0
        assert set(state.reserved_requests) == {queued, running}

    async def test_keeps_a_task_whose_pool_handle_is_gone(self):
        req = FakeRequest("collected")
        state.task_reserved(req)

        await _enter_draining(FakeTaskConsumer(), "memory limit exceeded")

        assert req.rejected is None
        assert set(state.reserved_requests) == {req}


class test_check_restart_conditions(LoopCase):
    def make_obj(self, **conf):
        self.app.conf.update(conf)
        return FakeConsumer(self.app)

    def test_no_reason_below_the_limits(self):
        obj = self.make_obj(worker_max_tasks_per_child=10, worker_max_memory_per_child=None)
        state.all_total_count[0] = 9

        assert _check_restart_conditions(obj, FakePool()) is None

    def test_max_tasks_per_child_reached(self):
        obj = self.make_obj(worker_max_tasks_per_child=10)
        state.all_total_count[0] = 10

        assert _check_restart_conditions(obj, FakePool()) == "max tasks per child (10) reached"

    def test_max_memory_per_child_exceeded(self, monkeypatch):
        monkeypatch.setattr(loops, "_get_rss_kib", lambda: 2048)
        obj = self.make_obj(worker_max_tasks_per_child=None, worker_max_memory_per_child=1024)

        assert _check_restart_conditions(obj, FakePool()) == "memory limit exceeded (RSS 2048 KiB > 1024 KiB)"

    def test_memory_is_only_sampled_every_few_seconds(self, monkeypatch):
        samples = []
        monkeypatch.setattr(loops, "_get_rss_kib", lambda: samples.append(1) or 2048)
        obj = self.make_obj(worker_max_tasks_per_child=None, worker_max_memory_per_child=1024)

        assert _check_restart_conditions(obj, FakePool())
        assert _check_restart_conditions(obj, FakePool()) is None
        assert len(samples) == 1

    def test_stuck_threads(self):
        obj = self.make_obj(worker_max_tasks_per_child=None, worker_max_memory_per_child=None)

        reason = _check_restart_conditions(obj, FakePool(stuck_thread_count=1))

        assert reason == "stuck thread(s) detected after hard timeout"

    def test_a_drain_waits_for_a_task_the_pool_would_not_release(self, monkeypatch):
        restarts = []
        monkeypatch.setattr(loops, "_trigger_restart", restarts.append)
        obj = self.make_obj()
        state.is_draining = True
        queued = FakeRequest("queued", FakePoolHandle(releases=False))
        state.task_reserved(queued)

        assert _check_restart_conditions(obj, FakePool()) is None
        assert restarts == []

        state.task_ready(queued)

        assert _check_restart_conditions(obj, FakePool()) is None
        assert restarts == ["all tasks finished during drain"]


class test_asynloop_draining(LoopCase):
    async def test_drains_and_restarts_when_max_tasks_per_child_is_reached(self, monkeypatch):
        restarts = []
        monkeypatch.setattr(loops, "_trigger_restart", restarts.append)
        self.app.conf.worker_max_tasks_per_child = 1
        state.all_total_count[0] = 1
        released = FakeRequest("released", FakePoolHandle(releases=True))
        queued = FakeRequest("queued", FakePoolHandle(releases=False))
        state.task_reserved(released)
        state.task_reserved(queued)
        blueprint = BlueprintState()

        def finish_the_kept_task():
            state.task_ready(queued)
            blueprint.state = CLOSE

        coro, _, _, task_consumer, _ = self.make_loop(
            lambda: None,
            finish_the_kept_task,
            pool=FakePool(),
            blueprint=blueprint,
        )
        await coro

        assert task_consumer.cancelled
        assert released.rejected is True
        assert queued.rejected is None
        assert restarts == ["all tasks finished during drain"]


@pytest.fixture(autouse=True)
def _reset_worker_state():
    state.reset_state()
    yield
    state.reset_state()
