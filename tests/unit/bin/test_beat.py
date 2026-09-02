import pytest
from click.testing import CliRunner

from celery.bin.celery import celery


@pytest.fixture(scope="session")
def use_celery_app_trap():
    return False


@pytest.mark.usefixtures("logging_already_set_up")
def test_cli(cli_runner: CliRunner):
    res = cli_runner.invoke(
        celery,
        ["-A", "tests.unit.bin.proj.app", "beat", "-S", "tests.unit.bin.proj.scheduler.mScheduler"],
        catch_exceptions=True,
    )
    assert res.exit_code == 1, (res, res.stdout)
    assert res.stdout.startswith("celery beat")
    assert "Configuration ->" in res.stdout


@pytest.mark.usefixtures("logging_already_set_up")
def test_cli_quiet(cli_runner: CliRunner):
    res = cli_runner.invoke(
        celery,
        ["-A", "tests.unit.bin.proj.app", "--quiet", "beat", "-S", "tests.unit.bin.proj.scheduler.mScheduler"],
        catch_exceptions=True,
    )
    assert res.exit_code == 1, (res, res.stdout)
    assert not res.stdout.startswith("celery beat")
    assert "Configuration -> " not in res.stdout
