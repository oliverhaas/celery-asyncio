"""Async I/O backend support utilities."""

import time

from kombu.utils.encoding import bytes_to_str

from celery import states
from celery.exceptions import TimeoutError

__all__ = (
    "AsyncBackendMixin",
    "BaseResultConsumer",
)


class AsyncBackendMixin:
    """Mixin for backends that enables the async API.

    Replaces the old PUBSUB-based notification mechanism with simple
    polling.  ``wait_for_pending()`` polls a single key with GET;
    ``iter_native()`` polls multiple keys with MGET.
    """

    @staticmethod
    def _poll_interval(timeout):
        """Calculate polling interval from timeout.

        Formula: timeout / 20, clamped to [0.1, 10.0].
        For no-timeout waits, returns 0.5s as a sensible default.
        """
        if timeout is None:
            return 0.5
        return max(0.1, min(timeout / 20, 10.0))

    def wait_for_pending(
        self,
        result,
        timeout=None,
        interval=None,
        callback=None,
        propagate=True,
        on_interval=None,
        on_message=None,
        **kwargs,
    ):
        """Wait for a single task result by polling."""
        self._ensure_not_eager()
        poll = self._poll_interval(timeout)
        time_start = time.monotonic()

        while True:
            meta = self.get_task_meta(result.id)
            if on_message:
                on_message(meta)
            if meta["status"] in states.READY_STATES:
                result._maybe_set_cache(meta)
                return result.maybe_throw(callback=callback, propagate=propagate)
            if on_interval:
                on_interval()
            if timeout is not None and time.monotonic() - time_start >= timeout:
                raise TimeoutError("The operation timed out.")
            time.sleep(poll)

    def iter_native(
        self, result, timeout=None, interval=None, no_ack=True, on_message=None, on_interval=None, **kwargs
    ):
        """Iterate over task results using MGET polling."""
        from celery.result import ResultSet

        self._ensure_not_eager()
        results = result.results
        if not results:
            return

        poll = self._poll_interval(timeout)
        time_start = time.monotonic()

        # Yield already-cached results and handle GroupResult/ResultSet
        # members immediately (they don't have individual task keys).
        remaining = {}
        for r in results:
            if isinstance(r, ResultSet):
                yield r.id, r.results
            elif hasattr(r, "_cache") and r._cache:
                yield r.id, r._cache
            else:
                remaining[r.id] = r

        # Poll for the rest with MGET
        while remaining:
            keys = list(remaining.keys())
            mget_keys = [self.get_key_for_task(tid) for tid in keys]
            values = self.mget(mget_keys)
            r = self._mget_to_results(values, keys, states.READY_STATES)

            for task_id, meta in r.items():
                task_id_str = bytes_to_str(task_id)
                if on_message:
                    on_message(meta)
                res = remaining.pop(task_id_str, None)
                if res:
                    res._maybe_set_cache(meta)
                    yield task_id_str, meta

            if on_interval:
                on_interval()
            if timeout is not None and time.monotonic() - time_start >= timeout:
                raise TimeoutError("The operation timed out.")
            if remaining:
                time.sleep(poll)

    def add_pending_result(self, result, weak=False, start_drainer=True):
        return result

    def remove_pending_result(self, result):
        return result

    @property
    def is_async(self):
        return True


class BaseResultConsumer:
    """Minimal base for result consumers.

    With polling-based result fetching, the consumer is a no-op stub.
    Subclasses only need to exist to satisfy the backend's
    ``ResultConsumer`` class attribute.
    """

    def __init__(self, backend, app, accept, pending_results, pending_messages):
        self.backend = backend
        self.app = app

    def start(self, initial_task_id, **kwargs):
        pass

    def stop(self):
        pass

    def consume_from(self, task_id):
        pass

    def cancel_for(self, task_id):
        pass
