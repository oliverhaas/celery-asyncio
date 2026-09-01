import asyncio
import contextvars
import threading

import pytest

from kombu.utils.eventloop import LoopRunner, current_loop, default_loop_runner


class test_current_loop:
    def test_none_outside_a_loop(self):
        assert current_loop() is None

    async def test_the_running_one_inside(self):
        assert current_loop() is asyncio.get_running_loop()


class test_LoopRunner:
    @pytest.fixture
    def runner(self):
        runner = LoopRunner(name="test-loop")
        yield runner
        runner.stop()

    def test_returns_what_the_coroutine_returns(self, runner):
        async def answer():
            await asyncio.sleep(0)
            return 42

        assert runner.run(answer()) == 42

    def test_raises_what_the_coroutine_raises(self, runner):
        async def boom():
            raise KeyError("nope")

        with pytest.raises(KeyError, match="nope"):
            runner.run(boom())

    def test_every_call_lands_on_the_same_loop(self, runner):
        # The whole point: an object opened by one call is still usable by the
        # next, which is only true while the loop stays alive.
        async def which_loop():
            return asyncio.get_running_loop()

        assert runner.run(which_loop()) is runner.run(which_loop())

    def test_the_loop_is_not_the_calling_thread(self, runner):
        async def which_thread():
            return threading.current_thread()

        assert runner.run(which_thread()) is not threading.current_thread()

    def test_the_callers_context_comes_along(self, runner):
        var = contextvars.ContextVar("var")
        var.set("set by the caller")

        async def read():
            return var.get()

        assert runner.run(read()) == "set by the caller"

    async def test_refuses_to_block_a_running_loop(self, runner):
        # Blocking here would stall the caller's own loop, and the coroutine it
        # is waiting on may need that loop to make progress.
        async def answer():
            return 42

        coro = answer()
        with pytest.raises(RuntimeError, match="asend_task"):
            runner.run(coro)

    def test_stop_then_run_starts_a_fresh_loop(self, runner):
        async def which_loop():
            return asyncio.get_running_loop()

        first = runner.run(which_loop())
        runner.stop()
        assert first.is_closed()
        assert runner.run(which_loop()) is not first

    def test_stop_cancels_what_is_still_running(self, runner):
        # A transport leaves long-lived background tasks on the loop --
        # consumer iterations, heartbeats. Closing the loop with those still
        # pending is what produces "Task was destroyed but it is pending!" at
        # interpreter exit, so stop() has to unwind them first.
        cancelled = threading.Event()

        async def forever():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def spawn():
            return asyncio.get_running_loop().create_task(forever())

        task = runner.run(spawn())
        runner.stop()

        assert cancelled.wait(5)
        assert task.cancelled()

    def test_stop_is_harmless_before_anything_ran(self, runner):
        runner.stop()

    def test_a_loop_inherited_across_fork_is_not_reused(self, runner):
        # The thread driving it did not come with us, so it is neither ours to
        # stop nor able to run anything.
        before = runner.loop
        runner._pid = -1
        try:
            assert runner.loop is not before
        finally:
            # There is no thread to clean up after a real fork; there is here.
            before.call_soon_threadsafe(before.stop)


class test_default_loop_runner:
    def test_one_runner_for_the_process(self):
        assert default_loop_runner() is default_loop_runner()


class test_run_from_any_thread:
    def test_returns_the_result(self):
        runner = LoopRunner(name="test-loop")
        try:

            async def work():
                return 42

            assert runner.run_from_any_thread(work()) == 42
        finally:
            runner.stop()

    async def test_runs_where_run_would_refuse(self):
        runner = LoopRunner(name="test-loop")
        try:

            async def work():
                return asyncio.get_running_loop()

            with pytest.raises(RuntimeError, match="Cannot block on the background loop"):
                runner.run(work())

            where = runner.run_from_any_thread(work())
            assert where is runner.loop
            assert where is not asyncio.get_running_loop()
        finally:
            runner.stop()
