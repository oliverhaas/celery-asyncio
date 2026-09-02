import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from celery.app.log import Logging
from celery.apps.worker import Worker
from celery.bin.celery import celery
from celery.bin.worker import strip_detach_options

from .proj.app import app as proj_app


@pytest.fixture(scope="session")
def use_celery_app_trap():
    return False


@pytest.fixture
def restore_app_conf():
    """Undo what a CLI invocation writes into the shared app configuration."""
    changes = dict(proj_app.conf.changes)
    yield proj_app
    proj_app.conf.changes.clear()
    proj_app.conf.changes.update(changes)


def run_worker(cli_runner, argv):
    """Invoke `celery worker` with `argv` and return the worker it built."""
    started = []

    async def record(self):
        started.append(self)

    with patch.object(Worker, "start", record):
        res = cli_runner.invoke(
            celery,
            ["-A", "tests.unit.bin.proj.app", "worker", "--pool", "asyncio", *argv],
            catch_exceptions=False,
        )

    assert started, (res, res.output)
    return started[0]


@pytest.mark.usefixtures("logging_already_set_up")
def test_cli(cli_runner: CliRunner):
    res = cli_runner.invoke(
        celery, ["-A", "tests.unit.bin.proj.app", "worker", "--pool", "asyncio"], catch_exceptions=False
    )
    assert res.exit_code == 1, (res, res.stdout)


@pytest.mark.usefixtures("logging_already_set_up")
def test_cli_skip_checks(cli_runner: CliRunner):
    with patch.dict(os.environ, clear=True):
        res = cli_runner.invoke(
            celery,
            ["-A", "tests.unit.bin.proj.app", "--skip-checks", "worker", "--pool", "asyncio"],
            catch_exceptions=False,
        )
        assert res.exit_code == 1, (res, res.stdout)
        assert os.environ["CELERY_SKIP_CHECKS"] == "true", "should set CELERY_SKIP_CHECKS"


def test_setup_logging_subsystem_is_not_left_disabled(cli_runner: CliRunner):
    # The three tests above used to set the flag on the class and never put it
    # back, so every later call to setup_logging_subsystem returned early.
    assert Logging._setup is False


@pytest.mark.usefixtures("logging_already_set_up")
@pytest.mark.parametrize(
    ("flag", "argument", "setting", "expected"),
    [
        ("--time-limit", "30", "task_time_limit", 30.0),
        ("--soft-time-limit", "20", "task_soft_time_limit", 20.0),
        ("--max-tasks-per-child", "7", "worker_max_tasks_per_child", 7),
        ("--max-memory-per-child", "1024", "worker_max_memory_per_child", 1024),
    ],
)
def test_limit_flags_reach_the_setting_the_worker_reads(
    flag, argument, setting, expected, cli_runner: CliRunner, restore_app_conf
):
    # They used to be handed to the pool as constructor keywords, which parked
    # them in `BasePool.options` where nothing ever looked them up.
    worker = run_worker(cli_runner, [flag, argument])

    assert worker.app.conf[setting] == expected


@pytest.mark.usefixtures("logging_already_set_up")
def test_time_limit_flags_reach_the_task(cli_runner: CliRunner, restore_app_conf):
    worker = run_worker(cli_runner, ["--time-limit", "30", "--soft-time-limit", "20"])

    @worker.app.task(name="tests.unit.bin.test_worker.limited")
    def limited():
        pass

    assert limited.time_limit == 30.0
    assert limited.soft_time_limit == 20.0


@pytest.mark.usefixtures("logging_already_set_up")
@pytest.mark.parametrize(
    ("flag", "attribute"),
    [
        ("--loop-workers", "loop_workers"),
        ("--loop-concurrency", "loop_concurrency"),
        ("--sync-workers", "sync_workers"),
    ],
)
def test_pool_sizing_flags_reach_the_worker(flag, attribute, cli_runner: CliRunner, restore_app_conf):
    worker = run_worker(cli_runner, [flag, "3"])

    assert getattr(worker, attribute) == 3


@pytest.mark.parametrize("removed", ["-O", "--optimization", "--disable-prefetch"])
def test_removed_options_are_rejected(removed, cli_runner: CliRunner):
    # `-O fair` and `--disable-prefetch` both described a prefork worker.
    res = cli_runner.invoke(celery, ["-A", "tests.unit.bin.proj.app", "worker", removed, "fair"])

    assert res.exit_code == 2, (res, res.output)
    assert "No such option" in res.output


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["worker", "--detach"], ["worker"]),
        (["worker", "-D"], ["worker"]),
        (["worker", "--detach", "--uid", "1000", "--gid", "2000"], ["worker"]),
        (["worker", "-D", "--uid=1000", "--gid=2000"], ["worker"]),
        (["worker", "-D", "--uid", "nobody", "-l", "INFO"], ["worker", "-l", "INFO"]),
        (["worker", "-l", "INFO", "-Q", "gid"], ["worker", "-l", "INFO", "-Q", "gid"]),
    ],
)
def test_strip_detach_options(argv, expected):
    assert strip_detach_options(argv) == expected


def test_detach_reexecutes_without_the_uid_and_gid_values(cli_runner: CliRunner):
    # The option names were removed but their values were not, so the detached
    # worker got "1000" as a stray positional and died on a usage error with
    # stdout and stderr already closed.
    command = [
        "celery",
        "-A",
        "tests.unit.bin.proj.app",
        "worker",
        "--detach",
        "--uid",
        "1000",
        "--gid",
        "2000",
    ]
    with patch("celery.bin.worker.detach", return_value=0) as detach, patch.object(sys, "argv", command):
        res = cli_runner.invoke(celery, command[1:], catch_exceptions=False)

    assert res.exit_code == 0, (res, res.output)
    assert detach.call_args.args[1] == ["-m", "celery", "-A", "tests.unit.bin.proj.app", "worker"]
    assert detach.call_args.kwargs["uid"] == "1000"
    assert detach.call_args.kwargs["gid"] == "2000"
