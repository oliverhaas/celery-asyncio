import os
from unittest.mock import patch

import pytest

from celery.app.log import Logging


@pytest.fixture
def logging_already_set_up(monkeypatch):
    """Skip the logging sanity checks the CLI runs on the way up.

    `_setup` used to be set on the class and never put back, which left every
    later `setup_logging_subsystem` in the session returning early.
    """
    monkeypatch.setattr(Logging, "_setup", True)


@pytest.fixture(autouse=True)
def restore_the_environment():
    """Undo the settings the CLI exports for the app to pick up.

    `--broker` becomes `CELERY_BROKER_URL`, and that outlives the invocation:
    it sent the next worker test at a live broker.
    """
    with patch.dict(os.environ):
        yield
