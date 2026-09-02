# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""The ``celery purge`` program, used to delete messages from queues."""

import asyncio

import click

from celery.bin.base import COMMA_SEPARATED_LIST, CeleryCommand, CeleryOption, handle_preload_options
from celery.utils import text


@click.command(cls=CeleryCommand, context_settings={"allow_extra_args": True})
@click.option(
    "-f", "--force", cls=CeleryOption, is_flag=True, help_group="Purging Options", help="Don't prompt for verification."
)
@click.option(
    "-Q",
    "--queues",
    cls=CeleryOption,
    type=COMMA_SEPARATED_LIST,
    help_group="Purging Options",
    help="Comma separated list of queue names to purge.",
)
@click.option(
    "-X",
    "--exclude-queues",
    cls=CeleryOption,
    type=COMMA_SEPARATED_LIST,
    help_group="Purging Options",
    help="Comma separated list of queues names not to purge.",
)
@click.pass_context
@handle_preload_options
def purge(ctx, force, queues, exclude_queues, **kwargs):
    """Erase all messages from all known task queues.

    Warning:

        There's no undo operation for this command.
    """
    app = ctx.obj.app
    queues = set(queues or app.amqp.queues.keys())
    exclude_queues = set(exclude_queues or [])
    names = queues - exclude_queues
    qnum = len(names)

    if names:
        queues_headline = text.pluralize(qnum, "queue")
        if not force:
            queue_names = ", ".join(sorted(names))
            click.confirm(
                f"{ctx.obj.style('WARNING', fg='red')}:"
                "This will remove all tasks from "
                f"{queues_headline}: {queue_names}.\n"
                "         There is no undo for this operation!\n\n"
                "(to skip this prompt use the -f option)\n"
                "Are you sure you want to delete all tasks?",
                abort=True,
            )

        async def _purge_all():
            async with app.connection_for_write() as conn:
                channel = await conn.default_channel()
                total = 0
                failed = []
                for queue in sorted(names):
                    try:
                        total += await channel.queue_purge(queue) or 0
                    except conn.channel_errors as exc:
                        # A queue the broker does not have is a channel error,
                        # and the broker closes the channel it raised on. The
                        # queues after it went to a dead channel and failed the
                        # same way, so a single missing queue silently purged
                        # nothing at all.
                        failed.append((queue, exc))
                        channel = await conn.channel()
                return total, failed

        messages, failed = asyncio.run(_purge_all())

        for queue, exc in failed:
            ctx.obj.error(f"Cannot purge {queue}: {exc}", fg="red")

        if messages:
            messages_headline = text.pluralize(messages, "message")
            ctx.obj.echo(f"Purged {messages} {messages_headline} from {qnum} known task {queues_headline}.")
        else:
            ctx.obj.echo(f"No messages purged from {qnum} {queues_headline}.")
