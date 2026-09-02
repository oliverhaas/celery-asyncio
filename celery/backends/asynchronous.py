"""Async I/O backend support utilities."""

import time

from kombu.utils.encoding import bytes_to_str

from celery import states
from celery.exceptions import TimeoutError

__all__ = ("AsyncBackendMixin",)


class AsyncBackendMixin:
    """Mixin for backends that enables the async API.

    Replaces the old PUBSUB-based notification mechanism with simple
    polling.  ``wait_for_pending()`` polls a single key with GET;
    ``iter_native()`` polls multiple keys with MGET.
    """

    @staticmethod
    def _poll_interval(timeout, interval=None):
        """Seconds between polls: the caller's interval, or one from timeout.

        The derived value is timeout / 20, clamped to [0.1, 10.0], and 0.5s
        for a wait with no timeout.
        """
        if interval is not None:
            return interval
        if timeout is None:
            return 0.5
        return max(0.1, min(timeout / 20, 10.0))

    @staticmethod
    def _wait_before_next_poll(deadline, poll):
        """Sleep until the next poll is due.

        The sleep is trimmed to what is left of the deadline, so a wait with
        a short timeout does not run a full poll interval past it.

        Raises:
            celery.exceptions.TimeoutError: if the deadline has passed.
        """
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("The operation timed out.")
            poll = min(poll, remaining)
        time.sleep(poll)

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
        poll = self._poll_interval(timeout, interval)
        deadline = None if timeout is None else time.monotonic() + timeout

        while True:
            meta = self.get_task_meta(result.id)
            if on_message:
                on_message(meta)
            if meta["status"] in states.READY_STATES:
                result._maybe_set_cache(meta)
                return result.maybe_throw(callback=callback, propagate=propagate)
            if on_interval:
                on_interval()
            self._wait_before_next_poll(deadline, poll)

    def iter_native(
        self, result, timeout=None, interval=None, no_ack=True, on_message=None, on_interval=None, **kwargs
    ):
        """Iterate over task results using MGET polling."""
        from celery.result import ResultSet

        self._ensure_not_eager()
        results = result.results
        if not results:
            return

        poll = self._poll_interval(timeout, interval)
        deadline = None if timeout is None else time.monotonic() + timeout

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
            if remaining:
                self._wait_before_next_poll(deadline, poll)

    def add_pending_result(self, result, weak=False, start_drainer=True):
        return result

    def remove_pending_result(self, result):
        return result

    @property
    def is_async(self):
        return True
