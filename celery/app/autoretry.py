# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""Tasks auto-retry functionality."""

import inspect
from functools import wraps

from celery.exceptions import Ignore, Retry
from celery.utils.time import get_exponential_backoff_interval


def add_autoretry_behaviour(task, **options):
    """Wrap task's `run` method with auto-retry functionality."""
    autoretry_for = tuple(options.get("autoretry_for", getattr(task, "autoretry_for", ())))
    dont_autoretry_for = tuple(options.get("dont_autoretry_for", getattr(task, "dont_autoretry_for", ())))
    retry_kwargs = dict(options.get("retry_kwargs", getattr(task, "retry_kwargs", {})))
    retry_backoff = float(options.get("retry_backoff", getattr(task, "retry_backoff", False)))
    retry_backoff_max = int(options.get("retry_backoff_max", getattr(task, "retry_backoff_max", 600)))
    retry_jitter = options.get("retry_jitter", getattr(task, "retry_jitter", True))

    def attempt_kwargs():
        """Build the keyword arguments for one retry attempt."""
        # A copy per attempt: `retry_kwargs` is closed over and therefore shared
        # by every call to this task, so writing the countdown into it lets one
        # attempt's backoff leak into a concurrent one (upstream 583fa06af).
        # With an asyncio pool that is the normal case, not a rare race.
        kwargs = retry_kwargs.copy()
        if retry_backoff:
            kwargs["countdown"] = get_exponential_backoff_interval(
                factor=int(max(1.0, retry_backoff)),
                retries=task.request.retries,
                maximum=retry_backoff_max,
                full_jitter=retry_jitter,
            )
        # Override max_retries
        if hasattr(task, "override_max_retries"):
            kwargs["max_retries"] = getattr(task, "override_max_retries", task.max_retries)
        return kwargs

    def stop_propagation():
        if hasattr(task, "override_max_retries"):
            delattr(task, "override_max_retries")

    def raise_retry(exc):
        """Turn a caught exception into the Retry to raise in its place."""
        ret = task.retry(exc=exc, **attempt_kwargs())
        stop_propagation()
        raise ret

    async def araise_retry(exc):
        """Async version of `raise_retry`, which does not stall the loop."""
        ret = await task.aretry(exc=exc, **attempt_kwargs())
        stop_propagation()
        raise ret

    if autoretry_for and not hasattr(task, "_orig_run"):
        if inspect.iscoroutinefunction(task.run):
            # Calling a coroutine function only builds the coroutine; nothing in
            # it runs until it is awaited. Wrapping the call alone in try/except
            # therefore caught nothing at all, so `autoretry_for` was silently
            # ignored on exactly the tasks this fork exists for.
            @wraps(task.run)
            async def run(*args, **kwargs):
                try:
                    return await task._orig_run(*args, **kwargs)
                except Ignore:
                    # If Ignore signal occurs task shouldn't be retried,
                    # even if it suits autoretry_for list
                    raise
                except Retry:
                    raise
                except dont_autoretry_for:
                    raise
                except autoretry_for as exc:
                    await araise_retry(exc)

        else:

            @wraps(task.run)
            def run(*args, **kwargs):
                try:
                    return task._orig_run(*args, **kwargs)
                except Ignore:
                    raise
                except Retry:
                    raise
                except dont_autoretry_for:
                    raise
                except autoretry_for as exc:
                    raise_retry(exc)

        task._orig_run, task.run = task.run, run
