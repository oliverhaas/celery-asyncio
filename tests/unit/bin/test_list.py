import pytest
from click.testing import CliRunner

from celery.bin.celery import celery

_GLOBAL_OPTIONS = ["-A", "tests.unit.bin.proj.app"]


@pytest.mark.parametrize(
    ("broker", "driver_type"),
    [("memory://", "memory"), ("redis://localhost:6379/14", "redis"), ("amqp://localhost:5672", "amqp")],
)
def test_bindings_reports_the_transport_that_cannot_list_them(broker, driver_type, cli_runner: CliRunner):
    # It used to reach for `Connection.manager`, which kombu does not have, so
    # every invocation ended in an AttributeError traceback.
    res = cli_runner.invoke(celery, [*_GLOBAL_OPTIONS, "--broker", broker, "list", "bindings"])

    assert res.exit_code == 1, (res, res.output)
    assert res.output.strip() == f"Error: The {driver_type} transport cannot list bindings."
