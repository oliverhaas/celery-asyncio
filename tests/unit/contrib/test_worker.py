import asyncio
import time
from unittest.mock import patch

import pytest

# this import adds a @shared_task, which uses connect_on_app_finalize
# to install the celery.ping task that the test lib uses
import celery.contrib.testing.tasks  # noqa
from celery import Celery
from celery.contrib.testing.worker import TestWorkController, start_worker


@pytest.mark.usefixtures("restore_logging")
class test_worker:
    def setup_method(self):
        self.app = Celery("celerytest", backend="cache+memory://", broker="memory://")

        @self.app.task
        def add(x, y):
            return x + y

        self.add = add

        @self.app.task
        def error_task():
            raise NotImplementedError()

        self.error_task = error_task

        self.app.config_from_object(
            {
                "worker_hijack_root_logger": False,
            }
        )

        # to avoid changing the root logger level to ERROR,
        # we have to set both app.log.loglevel start_worker arg to 0
        # (see celery.app.log.setup_logging_subsystem)
        self.app.log.loglevel = 0

    def test_start_worker(self):
        with start_worker(app=self.app, loglevel=0):
            result = self.add.s(1, 2).apply_async()
            val = result.get(timeout=5)
        assert val == 3

    def test_start_worker_with_exception(self):
        """Make sure that start_worker does not hang on exception"""

        with pytest.raises(NotImplementedError), start_worker(app=self.app, loglevel=0):
            result = self.error_task.apply_async()
            result.get(timeout=5)

    def test_start_worker_with_hostname_config(self):
        """Make sure a custom hostname can be supplied to the TestWorkController"""
        test_hostname = "test_name@test_host"
        with start_worker(app=self.app, loglevel=0, hostname=test_hostname) as w:
            assert isinstance(w, TestWorkController)
            assert w.hostname == test_hostname

            result = self.add.s(1, 2).apply_async()
            val = result.get(timeout=5)
        assert val == 3

    def test_start_worker_propagates_a_startup_failure(self):
        """A worker that cannot start must not leave the caller waiting.

        WorkController.start turns an unrecoverable error into an exit code,
        so the ready callback never fired and start_worker blocked on it for
        good, skipping its own cleanup with it.
        """

        async def boom(*args, **kwargs):
            raise RuntimeError("bootstep refused to start")

        started = time.monotonic()
        with patch("celery.worker.components.Pool.start", boom):
            with pytest.raises(RuntimeError, match="bootstep refused to start"):
                with start_worker(app=self.app, loglevel=0, perform_ping_check=False, startup_timeout=10.0):
                    pytest.fail("start_worker yielded a worker that never started")

        assert time.monotonic() - started < 10.0

    def test_start_worker_gives_up_when_the_worker_never_becomes_ready(self):
        """A worker that neither starts nor fails is bounded by startup_timeout."""

        async def never_ready(*args, **kwargs):
            await asyncio.Event().wait()

        started = time.monotonic()
        with patch("celery.worker.components.Pool.start", never_ready):
            with pytest.raises(RuntimeError, match="was not ready within 0.5 seconds"):
                with start_worker(app=self.app, loglevel=0, perform_ping_check=False, startup_timeout=0.5):
                    pytest.fail("start_worker yielded a worker that never started")

        assert time.monotonic() - started < 5.0

    def test_start_worker_reports_a_stop_before_ready_without_an_error(self):
        """A blueprint that returns before the ready callback still raises."""

        async def stop_early(*args, **kwargs):
            return None

        with patch("celery.worker.components.Consumer.start", stop_early):
            with pytest.raises(RuntimeError, match="stopped before it was ready"):
                with start_worker(app=self.app, loglevel=0, perform_ping_check=False, startup_timeout=10.0):
                    pytest.fail("start_worker yielded a worker that never started")
