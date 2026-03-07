import pytest
from click.testing import CliRunner

from celery.app.log import Logging
from celery.bin.celery import celery


@pytest.fixture(scope="session")
def use_celery_app_trap():
    return False


def test_cli(cli_runner: CliRunner):
    Logging._setup = True  # To avoid hitting the logging sanity checks
    res = cli_runner.invoke(
        celery,
        ["-A", "tests.unit.bin.proj.app", "beat", "-S", "tests.unit.bin.proj.scheduler.mScheduler"],
        catch_exceptions=True,
    )
    assert res.exit_code == 1, (res, res.stdout)
    assert res.stdout.startswith("celery beat")
    assert "Configuration ->" in res.stdout


def test_cli_quiet(cli_runner: CliRunner):
    Logging._setup = True  # To avoid hitting the logging sanity checks
    res = cli_runner.invoke(
        celery,
        ["-A", "tests.unit.bin.proj.app", "--quiet", "beat", "-S", "tests.unit.bin.proj.scheduler.mScheduler"],
        catch_exceptions=True,
    )
    assert res.exit_code == 1, (res, res.stdout)
    assert not res.stdout.startswith("celery beat")
    assert "Configuration -> " not in res.stdout
