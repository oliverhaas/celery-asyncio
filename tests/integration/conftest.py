import json
import logging
import os
import re
import time
from pathlib import Path
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

# Redis ships 16 databases. Each xdist worker takes one of its own, so parallel
# workers do not share broker queues, fanout channels or the keys the test tasks
# push to. Without this, `inspect` sees every worker's embedded worker and the
# queue assertions read each other's messages.
REDIS_DATABASES = 16


def _worker_database() -> int:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    if not worker.startswith("gw"):
        return 0
    index = int(worker.removeprefix("gw"))
    if index >= REDIS_DATABASES:
        raise RuntimeError(
            f"pytest-xdist worker {worker} has no Redis database of its own; "
            f"run tests/integration with at most {REDIS_DATABASES} workers."
        )
    return index


def _on_database(url: str, database: int) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"redis", "rediss", "valkey"}:
        return url
    return urlunparse(parsed._replace(path=f"/{database}"))


REDIS_DATABASE = _worker_database()

# Read back by tasks.get_redis_connection, which the tasks themselves call. The
# embedded worker runs in this process, so the environment reaches it.
os.environ["REDIS_DB"] = str(REDIS_DATABASE)

TEST_BROKER = _on_database(os.environ.get("TEST_BROKER", "redis://"), REDIS_DATABASE)
TEST_BACKEND = _on_database(os.environ.get("TEST_BACKEND", "redis://"), REDIS_DATABASE)

KNOWN_FAILURES_FILE = Path(__file__).parent / "known-failures.txt"


def _known_failures() -> set[str]:
    return {
        stripped for line in KNOWN_FAILURES_FILE.read_text().splitlines() if (stripped := line.split("#")[0].strip())
    }


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark the tests this fork is known to fail as xfail.

    Without this the suite cannot run in CI at all, and the tests that do pass
    guard nothing. `strict=False` because a listed test that starts passing
    should report XPASS, not turn the build red -- that is the cue to delete
    its line.
    """
    known_failures = _known_failures()
    marker = pytest.mark.xfail(reason=f"listed in {KNOWN_FAILURES_FILE.name}", strict=False)
    for item in items:
        if item.nodeid in known_failures:
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
