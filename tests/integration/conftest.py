import json
import logging
import os
import re
import time
from urllib.parse import urlparse, urlunparse

import pytest

from celery.contrib.pytest import (  # noqa: F401
    celery_app,
    celery_parameters,
    celery_session_app,
    celery_session_worker,
    celery_worker_parameters,
    use_celery_app_trap,
)
from celery.contrib.testing.manager import Manager
from tests.integration.tasks import get_redis_connection

# We import the pytest plugin fixtures here since the plugin isn't
# registered via entry_points (no `python setup.py develop`).


logger = logging.getLogger(__name__)

# A database per xdist worker, so parallel workers do not share broker queues,
# fanout channels or the keys the test tasks push to. The suite deletes what it
# finds, so it keeps to the top six of Redis's sixteen and leaves the low ones,
# database 0 above all, to whatever else is on the machine.
FIRST_REDIS_DATABASE = 10
REDIS_DATABASES = 16 - FIRST_REDIS_DATABASE


def _worker_database() -> int:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    if not worker.startswith("gw"):
        return FIRST_REDIS_DATABASE
    index = int(worker.removeprefix("gw"))
    if index >= REDIS_DATABASES:
        raise RuntimeError(
            f"pytest-xdist worker {worker} has no Redis database of its own; "
            f"run tests/integration with at most {REDIS_DATABASES} workers."
        )
    return FIRST_REDIS_DATABASE + index


def _on_database(url: str, database: int) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"redis", "rediss", "valkey"}:
        return url
    # Rebuilt rather than urlunparse(parsed._replace(...)): the default TEST_BROKER
    # is "redis://", whose netloc is empty, and urlunparse drops the "//" when it
    # is, which yields "redis:/0" and no usable host.
    tail = urlunparse(("", "", f"/{database}", parsed.params, parsed.query, parsed.fragment))
    return f"{parsed.scheme}://{parsed.netloc}{tail}"


REDIS_DATABASE = _worker_database()

TEST_BROKER = _on_database(os.environ.get("TEST_BROKER", "redis://"), REDIS_DATABASE)
TEST_BACKEND = _on_database(os.environ.get("TEST_BACKEND", "redis://"), REDIS_DATABASE)


@pytest.fixture(scope="session", autouse=True)
def redis_database_env():
    """Point the test tasks at this xdist worker's Redis database.

    Read back by tasks.get_redis_connection, which the tasks themselves call.
    The embedded worker runs in this process, so the environment reaches it.
    Session scope, so the function-scoped monkeypatch fixture will not do.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REDIS_DB", str(REDIS_DATABASE))
        yield


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip the RabbitMQ-only tests when the broker under test is not AMQP.

    The `amqp` marker records that a test asserts on broker behaviour only
    RabbitMQ has: queue types, delayed-delivery bindings, quorum QoS. Run
    against the default Redis broker they do not fail meaningfully, they fail
    on the first connection argument the transport does not understand.
    """
    if TEST_BROKER.startswith(("amqp", "amqps", "pyamqp")):
        return
    marker = pytest.mark.skip(reason=f"requires an AMQP broker, TEST_BROKER is {TEST_BROKER}")
    for item in items:
        if item.get_closest_marker("amqp") is not None:
            item.add_marker(marker)


__all__ = (
    "celery_app",
    "celery_session_worker",
    "get_active_redis_channels",
)


def get_active_redis_channels():
    return get_redis_connection().execute_command("PUBSUB CHANNELS")


def check_for_logs(caplog, message: str, max_wait: float = 1.0, interval: float = 0.1) -> bool:
    start_time = time.monotonic()
    while time.monotonic() - start_time < max_wait:
        if any(re.search(message, record.message) for record in caplog.records):
            return True
        time.sleep(interval)
    return False


@pytest.fixture(scope="session")
def celery_config(request):
    config = {
        "broker_url": TEST_BROKER,
        "result_backend": TEST_BACKEND,
        "result_extended": True,
    }
    try:
        # To override the default configuration, create the integration-tests-config.json file
        # in Celery's root directory.
        # The file must contain a dictionary of valid configuration name/value pairs.
        with open(str(request.config.rootdir / "integration-tests-config.json")) as file:
            overrides = json.load(file)
        config.update(overrides)
    except OSError:
        pass
    return config


@pytest.fixture(scope="session")
def celery_enable_logging():
    return True


@pytest.fixture(scope="session")
def celery_worker_pool():
    return "asyncio"


@pytest.fixture(scope="session")
def celery_includes():
    return {"tests.integration.tasks"}


@pytest.fixture
def app(celery_app):
    return celery_app


@pytest.fixture
def manager(app, celery_session_worker):
    manager = Manager(app)
    yield manager
    try:
        manager.wait_until_idle()
    except Exception as e:
        logger.warning("Failed to stop Celery test manager cleanly: %s", e)


@pytest.fixture(autouse=True)
def ZZZZ_set_app_current(app):
    app.set_current()
    app.set_default()


@pytest.fixture(scope="session")
def celery_class_tasks():
    from tests.integration.tasks import ClassBasedAutoRetryTask

    return [ClassBasedAutoRetryTask]
