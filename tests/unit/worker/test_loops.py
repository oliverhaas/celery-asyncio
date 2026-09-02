import time
from collections import deque

import pytest
from kombu.common import QoS

from celery.bootsteps import CLOSE, RUN
from celery.utils.scheduling import Timer
from celery.worker import state
from celery.worker.loops import asynloop


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


@pytest.fixture(autouse=True)
def _reset_worker_state():
    yield
    state.reset_state()
