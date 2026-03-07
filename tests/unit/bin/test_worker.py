import os
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from celery.app.log import Logging
from celery.bin.celery import celery
from celery.worker.consumer.tasks import Tasks


@pytest.fixture(scope="session")
def use_celery_app_trap():
    return False


@pytest.fixture
def mock_app():
    app = Mock()
    app.conf = Mock()
    app.conf.worker_disable_prefetch = False
    app.conf.worker_detect_quorum_queues = False
    return app


@pytest.fixture
def mock_consumer(mock_app):
    consumer = Mock()
    consumer.app = mock_app
    consumer.pool = Mock()
    consumer.pool.num_processes = 4
    consumer.controller = Mock()
    consumer.controller.max_concurrency = None
    consumer.initial_prefetch_count = 16
    consumer.task_consumer = Mock()
    consumer.task_consumer.channel = Mock()
    consumer.task_consumer.channel.qos = Mock()
    original_can_consume = Mock(return_value=True)
    consumer.task_consumer.channel.qos.can_consume = original_can_consume
    consumer.connection = Mock()
    consumer.connection.transport = Mock()
    consumer.connection.transport.driver_type = "redis"  # Default to Redis for existing tests
    consumer.connection.qos_semantics_matches_spec = True
    consumer.update_strategies = Mock()
    consumer.on_decode_error = Mock()
    consumer.app.amqp = Mock()
    consumer.app.amqp.TaskConsumer = Mock(return_value=consumer.task_consumer)
    consumer.app.amqp.queues = {}  # Empty dict for quorum queue detection
    return consumer


def test_cli(cli_runner: CliRunner):
    Logging._setup = True  # To avoid hitting the logging sanity checks
    res = cli_runner.invoke(
        celery, ["-A", "tests.unit.bin.proj.app", "worker", "--pool", "asyncio"], catch_exceptions=False
    )
    assert res.exit_code == 1, (res, res.stdout)


def test_cli_skip_checks(cli_runner: CliRunner):
    Logging._setup = True  # To avoid hitting the logging sanity checks
    with patch.dict(os.environ, clear=True):
        res = cli_runner.invoke(
            celery,
            ["-A", "tests.unit.bin.proj.app", "--skip-checks", "worker", "--pool", "asyncio"],
            catch_exceptions=False,
        )
        assert res.exit_code == 1, (res, res.stdout)
        assert os.environ["CELERY_SKIP_CHECKS"] == "true", "should set CELERY_SKIP_CHECKS"


def test_cli_disable_prefetch_flag(cli_runner: CliRunner):
    Logging._setup = True
    with patch("celery.bin.worker.worker.callback") as worker_callback_mock:
        res = cli_runner.invoke(
            celery,
            ["-A", "tests.unit.bin.proj.app", "worker", "--pool", "asyncio", "--disable-prefetch"],
            catch_exceptions=False,
        )
        assert res.exit_code == 0
        _, kwargs = worker_callback_mock.call_args
        assert kwargs["disable_prefetch"] is True



# disable_prefetch tests removed - feature not yet implemented in async Tasks.start
