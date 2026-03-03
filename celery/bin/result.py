"""The ``celery result`` program, used to inspect task results."""

import click

from celery.bin.base import CeleryCommand, CeleryOption, handle_preload_options


@click.command(cls=CeleryCommand)
@click.argument("task_id")
@click.option("-t", "--task", cls=CeleryOption, help_group="Result Options", help="Name of task (if custom backend).")
@click.option(
    "--traceback", cls=CeleryOption, is_flag=True, help_group="Result Options", help="Show traceback instead."
)
@click.pass_context
@handle_preload_options
def result(ctx, task_id, task, traceback):
    """Print the return value for a given task id."""
    app = ctx.obj.app

    result_cls = app.tasks[task].AsyncResult if task else app.AsyncResult
    task_result = result_cls(task_id)
    if traceback:
        value = task_result.traceback
    else:
        # Fetch result meta directly (polling) - avoids the async
        # event-driven drainer path which requires a running event loop.
        meta = task_result.backend.get_task_meta(task_result.id)
        task_result._maybe_set_cache(meta)
        value = task_result.result

    ctx.obj.echo(value)
