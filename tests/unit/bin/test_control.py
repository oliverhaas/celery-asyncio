import os
import re
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner
from kombu.exceptions import OperationalError

from celery.bin.base import handle_remote_command_error
from celery.bin.control import _compile_arguments
from celery.bin.celery import celery
from celery.platforms import EX_UNAVAILABLE

_GLOBAL_OPTIONS = ["-A", "tests.unit.bin.proj.app_with_custom_cmds", "--broker", "memory://"]
_INSPECT_OPTIONS = ["--timeout", "0"]  # Avoid waiting for the zero workers to reply


@pytest.fixture(autouse=True)
def clean_os_environ():
    # Celery modifies os.environ when given the CLI option --broker memory://
    # This interferes with other tests, so we need to reset os.environ
    with patch.dict(os.environ, clear=True):
        yield


@pytest.mark.parametrize(
    ("celery_cmd", "custom_cmd"),
    [
        ("inspect", ("custom_inspect_cmd", "123")),
        ("control", ("custom_control_cmd", "123", "456")),
    ],
)
def test_custom_remote_command(celery_cmd, custom_cmd, cli_runner: CliRunner):
    res = cli_runner.invoke(
        celery,
        [*_GLOBAL_OPTIONS, celery_cmd, *_INSPECT_OPTIONS, *custom_cmd],
        catch_exceptions=False,
    )
    assert res.exit_code == EX_UNAVAILABLE, (res, res.output)
    assert res.output.strip() == "Error: No nodes replied within time constraint"


@pytest.mark.parametrize(
    ("celery_cmd", "remote_cmd"),
    [
        # Test nonexistent commands
        ("inspect", "this_command_does_not_exist"),
        ("control", "this_command_does_not_exist"),
        # Test commands that exist, but are of the wrong type
        ("inspect", "custom_control_cmd"),
        ("control", "custom_inspect_cmd"),
    ],
)
def test_unrecognized_remote_command(celery_cmd, remote_cmd, cli_runner: CliRunner):
    res = cli_runner.invoke(
        celery,
        [*_GLOBAL_OPTIONS, celery_cmd, *_INSPECT_OPTIONS, remote_cmd],
        catch_exceptions=False,
    )
    assert res.exit_code == 2, (res, res.output)
    assert f"Error: Command {remote_cmd} not recognized. Available {celery_cmd} commands: " in res.output


_expected_inspect_regex = "\n  custom_inspect_cmd x\\s+Ask the workers to reply with x\\.\n"
_expected_control_regex = "\n  custom_control_cmd a b\\s+Ask the workers to reply with a and b\\.\n"


@pytest.mark.parametrize(
    ("celery_cmd", "expected_regex"),
    [
        ("inspect", re.compile(_expected_inspect_regex, re.MULTILINE)),
        ("control", re.compile(_expected_control_regex, re.MULTILINE)),
    ],
)
def test_listing_remote_commands(celery_cmd, expected_regex, cli_runner: CliRunner):
    res = cli_runner.invoke(
        celery,
        [*_GLOBAL_OPTIONS, celery_cmd, "--list"],
    )
    assert res.exit_code == 0, (res, res.stdout)
    assert expected_regex.search(res.stdout)


# A broker that is down is the single most likely reason any of these three
# commands fails, and printing the kombu traceback for it tells the operator
# nothing actionable (upstream 7735d2ba9). Each command is checked twice: once
# for the broker case, which gets its own message, and once for anything else,
# which gets summarised rather than dumped.
_REMOTE_COMMANDS = [
    ("celery.app.control.Inspect.ping", ["status"], "status"),
    (
        "celery.app.control.Inspect._request",
        ["inspect", *_INSPECT_OPTIONS, "custom_inspect_cmd", "1"],
        "inspect custom_inspect_cmd",
    ),
    (
        "celery.app.control.Control.broadcast",
        ["control", *_INSPECT_OPTIONS, "custom_control_cmd", "1", "2"],
        "control custom_control_cmd",
    ),
]


@pytest.mark.parametrize(("target", "argv", "label"), _REMOTE_COMMANDS, ids=["status", "inspect", "control"])
def test_friendly_error_when_broker_unreachable(target, argv, label, cli_runner: CliRunner):
    with patch(target, side_effect=OperationalError("[Errno 61] Connection refused")):
        res = cli_runner.invoke(celery, [*_GLOBAL_OPTIONS, *argv], catch_exceptions=False)

    assert res.exit_code == EX_UNAVAILABLE, (res, res.output)
    assert "Error: Could not connect to the message broker." in res.output
    assert "Reason: [Errno 61] Connection refused" in res.output
    assert "Traceback" not in res.output


@pytest.mark.parametrize(("target", "argv", "label"), _REMOTE_COMMANDS, ids=["status", "inspect", "control"])
def test_unexpected_error_is_summarized(target, argv, label, cli_runner: CliRunner):
    with patch(target, side_effect=RuntimeError("boom")):
        res = cli_runner.invoke(celery, [*_GLOBAL_OPTIONS, *argv], catch_exceptions=False)

    assert res.exit_code == EX_UNAVAILABLE, (res, res.output)
    assert f"Error: Unable to run the `{label}` command. Reason: boom" in res.output
    assert "Traceback" not in res.output


def test_handle_remote_command_error_reraises_click_exception():
    # A ClickException already carries its own message and exit code, so
    # wrapping it would only bury both.
    original = click.ClickException("original click error")

    with pytest.raises(click.ClickException) as exc_info:
        handle_remote_command_error("any", original)

    assert exc_info.value is original


def test_control_with_preload_option(cli_runner: CliRunner):
    # `control` was the one remote command whose callback took no **kwargs, so
    # an app-registered preload option arrived as an unexpected keyword
    # argument (upstream 4886d5d0c). `status` and `inspect` already had it.
    # Upstream's own test passes `--workdir`, which is a global option and
    # never reaches the callback at all; a real preload option does. This is
    # the same `--ini` that tests/unit/app/test_preload_cli.py exercises.
    # The app's connection is mocked for `purge`, not for a broadcast, so the
    # broadcast itself is stubbed out. What is under test is whether the
    # callback can be *called* at all with --ini in kwargs.
    with patch("celery.app.control.Control.broadcast", return_value={}) as broadcast:
        res = cli_runner.invoke(
            celery,
            [
                "-A",
                "tests.unit.bin.proj.pyramid_celery_app",
                "--broker",
                "memory://",
                "control",
                *_INSPECT_OPTIONS,
                "revoke",
                "some-task-id",
                "--ini",
                "some_ini.ini",
            ],
            catch_exceptions=False,
        )

    assert broadcast.called
    assert res.exit_code == EX_UNAVAILABLE, (res, res.output)
    assert res.output.strip() == "Error: No nodes replied within time constraint"


@pytest.mark.parametrize(
    ("command", "argv", "expected"),
    [
        # The variadic used to be handed the last positional as well, so
        # `terminate SIGTERM` asked the workers to kill a task id "SIGTERM".
        ("terminate", ["SIGTERM"], {"signal": "SIGTERM", "task_id": []}),
        ("terminate", ["SIGTERM", "id1"], {"signal": "SIGTERM", "task_id": ["id1"]}),
        ("terminate", ["SIGTERM", "id1", "id2"], {"signal": "SIGTERM", "task_id": ["id1", "id2"]}),
        ("revoke", ["id1", "id2"], {"task_id": ["id1", "id2"]}),
        ("rate_limit", ["t.add", "10/s"], {"task_name": "t.add", "rate_limit": "10/s"}),
    ],
)
def test_compile_arguments(command, argv, expected):
    assert _compile_arguments(command, list(argv)) == expected


def test_compile_arguments_leaves_no_positional_behind():
    args = ["SIGTERM", "id1"]
    _compile_arguments("terminate", args)
    assert args == ["id1"]


def test_status_quiet_omits_the_node_count(cli_runner: CliRunner):
    # `-q` is a global option, so it arrives on ctx.obj, never in **kwargs.
    with patch("celery.app.control.Inspect.ping", return_value={"node@host": {"ok": "pong"}}):
        res = cli_runner.invoke(celery, [*_GLOBAL_OPTIONS, "-q", "status"], catch_exceptions=False)

    assert res.exit_code == 0, (res, res.output)
    assert "online" not in res.output


def test_status_reports_the_node_count_without_quiet(cli_runner: CliRunner):
    with patch("celery.app.control.Inspect.ping", return_value={"node@host": {"ok": "pong"}}):
        res = cli_runner.invoke(celery, [*_GLOBAL_OPTIONS, "status"], catch_exceptions=False)

    assert res.exit_code == 0, (res, res.output)
    assert "1 node online." in res.output
