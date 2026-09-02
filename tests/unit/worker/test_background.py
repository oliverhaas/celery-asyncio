import asyncio
import gc
import logging
import threading

from celery.worker.background import _running, spawn, spawn_threadsafe


class test_spawn:
    async def test_the_task_survives_a_collection_while_it_waits(self):
        # The loop holds only a weak reference to a running task, so without
        # one of its own a fire-and-forget ack could vanish before it is sent.
        started, finished = asyncio.Event(), []

        async def work():
            started.set()
            await asyncio.sleep(0)
            finished.append(True)

        spawn(work(), name="ack")
        await started.wait()
        gc.collect()
        assert [task.get_name() for task in _running] == ["ack"]

        await asyncio.sleep(0.01)

        assert finished == [True]
        assert not _running

    async def test_reports_a_failure_instead_of_dropping_it(self, caplog):
        async def work():
            raise ValueError("no ack for you")

        with caplog.at_level(logging.ERROR):
            task = spawn(work(), name="ack")
            await asyncio.wait([task])
            await asyncio.sleep(0)

        assert "no ack for you" in caplog.text
        assert not _running

    async def test_a_cancelled_task_is_not_reported_as_a_failure(self, caplog):
        async def work():
            await asyncio.Event().wait()

        with caplog.at_level(logging.ERROR):
            task = spawn(work())
            task.cancel()
            await asyncio.wait([task])
            await asyncio.sleep(0)

        assert caplog.text == ""
        assert not _running


class test_spawn_threadsafe:
    async def test_runs_the_coroutine_on_the_given_loop(self):
        loop = asyncio.get_running_loop()
        ran = []

        async def work():
            ran.append(threading.current_thread())

        thread = threading.Thread(target=spawn_threadsafe, args=(work(), loop))
        thread.start()
        thread.join(5)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if ran:
                break

        assert ran == [threading.current_thread()]
