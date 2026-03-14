"""Minimal compatibility stub for AbortableAsyncResult.

Flower imports this module. The full abortable task implementation
was removed from celery-asyncio (use task revocation instead).
"""

from celery.result import AsyncResult

__all__ = ("AbortableAsyncResult",)


class AbortableAsyncResult(AsyncResult):
    """AsyncResult subclass that adds an abort() method (stub).

    In celery-asyncio, task cancellation is handled via revocation.
    This class exists only for compatibility with tools like Flower.
    """

    def abort(self):
        """Abort the task by revoking it."""
        self.revoke(terminate=True)

    def is_aborted(self):
        """Return whether the task was aborted (revoked)."""
        return self.state == "REVOKED"
