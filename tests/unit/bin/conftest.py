import contextlib
import os
from unittest.mock import patch

import pytest

from celery.app.log import Logging
from celery.bin.celery import celery


@pytest.fixture
def logging_already_set_up(monkeypatch):
    """Skip the logging sanity checks the CLI runs on the way up.

    `_setup` used to be set on the class and never put back, which left every
    later `setup_logging_subsystem` in the session returning early.
    """
    monkeypatch.setattr(Logging, "_setup", True)


@pytest.fixture(autouse=True)
def restore_cli_module_state():
    """Undo what invoking the CLI leaves behind in module-level state.

    The group callback appends the app's preload options to every command it
    knows about and exports `--broker` into the environment. Both outlive the
    invocation: an app with a preload option handed `--ini` to every later
    test, and a broker URL sent the next worker test at a live broker.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ))
        for command in celery.commands.values():
            # Shallow copies: preload options are appended, the rest is shared.
            stack.enter_context(patch.object(command, "params", command.params[:]))
        yield
