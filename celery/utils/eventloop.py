"""One long-lived event loop, for reaching async code from synchronous callers.

Re-exported from :mod:`kombu.utils.eventloop`. A broker connection is opened by
kombu and used by celery, so both have to reach the same loop: an asyncio
transport belongs to the loop that opened it and cannot outlive it.
"""

from kombu.utils.eventloop import LoopRunner, current_loop, default_loop_runner

__all__ = ("LoopRunner", "current_loop", "default_loop_runner")
