# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""The ``celery list bindings`` command, used to inspect queue bindings."""

import asyncio

import click

from celery.bin.base import CeleryCommand, handle_preload_options


@click.group(name="list")
@click.pass_context
@handle_preload_options
def list_(ctx):
    """Get info from broker.

    Note:

        For RabbitMQ the management plugin is required.
    """


@list_.command(cls=CeleryCommand)
@click.pass_context
def bindings(ctx):
    """Inspect queue bindings."""
    # TODO: Consider using a table formatter for this command.
    app = ctx.obj.app

    async def _get_bindings():
        async with app.connection() as conn:
            consumer = app.amqp.TaskConsumer(conn)
            try:
                await consumer.declare()
            finally:
                await consumer.close()

            try:
                return conn.manager.get_bindings()
            except NotImplementedError:
                raise click.UsageError("Your transport cannot list bindings.")

    bindings_list = asyncio.run(_get_bindings())

    def fmt(q, e, r):
        ctx.obj.echo(f"{q:<28} {e:<28} {r}")

    fmt("Queue", "Exchange", "Routing Key")
    fmt("-" * 16, "-" * 16, "-" * 16)
    for b in bindings_list:
        fmt(b["destination"], b["source"], b["routing_key"])
