"""Example tasks using Django 6.0's @task decorator.

These tasks are dispatched via Celery under the hood, but the API is
pure Django — no Celery imports needed in application code.
"""

import asyncio

from django.tasks import task


@task
def add(x, y):
    """Simple synchronous task."""
    return x + y


@task
async def slow_add(x, y):
    """Async task that simulates I/O-bound work."""
    await asyncio.sleep(1)
    return x + y


@task(priority=50)
def high_priority_task(message):
    """Task with higher-than-default priority (runs sooner)."""
    return f"urgent: {message}"


@task(takes_context=True)
def task_with_context(context, data):
    """Task that receives a TaskContext as its first argument.

    The context provides ``context.attempt`` (number of times this
    task has been tried) and ``context.task_result`` for introspection.
    """
    return {
        "attempt": context.attempt,
        "task_id": context.task_result.id,
        "data": data,
    }
