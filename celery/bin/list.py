# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""The ``celery list bindings`` command, used to inspect queue bindings."""

import click

from celery.bin.base import CeleryCommand, handle_preload_options
from celery.exceptions import CeleryCommandException


@click.group(name="list")
@click.pass_context
@handle_preload_options
def list_(ctx):
    """Get info from broker."""


@list_.command(cls=CeleryCommand)
@click.pass_context
def bindings(ctx):
    """Inspect queue bindings.

    None of the transports in this distribution can enumerate bindings. The
    AMQP transport speaks the protocol, which has no way to ask a broker what
    is bound, and the RabbitMQ management HTTP API upstream used for it is not
    part of kombu here. The Valkey/Redis transport keeps its binding tables
    under keys the transport owns and does not publish.
    """
    driver_type = ctx.obj.app.connection().info()["driver_type"]
    raise CeleryCommandException(
        message=f"The {driver_type} transport cannot list bindings.",
        exit_code=1,
    )
