from unittest.mock import Mock

from celery.apps.worker import Worker


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
