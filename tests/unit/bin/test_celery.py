from click import Option
from click.testing import CliRunner

from celery.bin.celery import celery

_PYRAMID = ["-A", "tests.unit.bin.proj.pyramid_celery_app"]
_PLAIN = ["-A", "tests.unit.bin.proj.app"]


def test_preload_options_are_gone_again_for_the_next_app(cli_runner: CliRunner):
    # The options used to be appended to the module-level commands and stayed
    # there, so the next app in the same process inherited them.
    with_options = cli_runner.invoke(celery, [*_PYRAMID, "purge", "--help"], catch_exceptions=False)
    assert "--ini" in with_options.output

    without_options = cli_runner.invoke(celery, [*_PLAIN, "purge", "--help"], catch_exceptions=False)
    assert "--ini" not in without_options.output


def test_worker_options_are_gone_again_for_the_next_app(cli_runner: CliRunner):
    from tests.unit.bin.proj.pyramid_celery_app import app

    app.user_options["worker"].add(Option(("--replicas",), help="How many of them to run."))
    try:
        with_options = cli_runner.invoke(celery, [*_PYRAMID, "worker", "--help"], catch_exceptions=False)
    finally:
        app.user_options["worker"].clear()
    assert "--replicas" in with_options.output

    without_options = cli_runner.invoke(celery, [*_PLAIN, "worker", "--help"], catch_exceptions=False)
    assert "--replicas" not in without_options.output


def test_an_invocation_leaves_the_command_parameters_as_it_found_them(cli_runner: CliRunner):
    before = {name: command.params[:] for name, command in celery.commands.items()}

    cli_runner.invoke(celery, [*_PYRAMID, "purge", "--help"], catch_exceptions=False)

    assert {name: command.params for name, command in celery.commands.items()} == before
