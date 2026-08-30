"""Scheduling primitives for celery-asyncio.

Provides Timer, Hub, and related scheduling components for the asyncio-based
worker. These are native asyncio implementations.
"""

import asyncio
import heapq
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = (
    # Hub and event loop
    "Hub",
    "get_event_loop",
    "set_event_loop",
    # Semaphores
    "DummyLock",
    "LaxBoundedSemaphore",
    # Timer
    "Timer",
    "Entry",
    "to_timestamp",
)

logger = logging.getLogger(__name__)

# =============================================================================
# Event Loop / Hub
# =============================================================================

_current_hub: Hub | None = None


class Hub:
    """Asyncio-based event hub.

    This is a simplified hub that wraps asyncio's event loop.
    In celery-asyncio, we use native asyncio instead of a custom hub.
    """

    def __init__(self, timer: Timer | None = None) -> None:
        self.timer = timer or Timer()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._callbacks: list[Callable[[], Any]] = []

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
        return self._loop

    def call_soon(self, callback: Callable[[], Any]) -> None:
        """Schedule callback to be called soon."""
        self._callbacks.append(callback)

    def call_later(self, delay: float, callback: Callable[[], Any]) -> None:
        """Schedule callback after delay seconds."""
        self.timer.call_after(delay, callback)

    def close(self) -> None:
        """Close the hub."""
        self._callbacks.clear()
        if self.timer:
            self.timer.clear()


def get_event_loop() -> Hub | None:
    """Get the current event loop/hub."""
    return _current_hub


def set_event_loop(hub: Hub) -> Hub:
    """Set the current event loop/hub."""
    global _current_hub
    _current_hub = hub
    return hub


# =============================================================================
# Semaphores
# =============================================================================


class DummyLock:
    """Dummy lock that does nothing.

    Used to replace threading locks in async contexts.
    """

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        return True

    def release(self) -> None:
        pass

    def __enter__(self) -> DummyLock:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class LaxBoundedSemaphore:
    """Bounded semaphore that allows releasing more than acquired.

    This is a synchronous semaphore for use in the worker.
    """

    def __init__(self, value: int = 1) -> None:
        self._initial_value = value
        self._value = value
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        """Acquire the semaphore."""
        with self._condition:
            if not blocking:
                if self._value <= 0:
                    return False
                self._value -= 1
                return True

            while self._value <= 0:
                if not self._condition.wait(timeout):
                    return False
            self._value -= 1
            return True

    def release(self) -> None:
        """Release the semaphore."""
        with self._condition:
            self._value += 1
            self._value = min(self._value, self._initial_value)
            self._condition.notify()

    def __enter__(self) -> LaxBoundedSemaphore:
        self.acquire()
        return self

    def __exit__(self, *args: Any) -> None:
        self.release()

    @property
    def value(self) -> int:
        return self._value


# =============================================================================
# Timer
# =============================================================================


def to_timestamp(dt: float | None, default_timezone: Any = None) -> float:
    """Convert to timestamp."""
    if dt is None:
        return time.time()
    if isinstance(dt, (int, float)):
        return float(dt)
    # Assume datetime
    return dt.timestamp()


def _monotonic_at(timestamp: float) -> float:
    """Map a wall-clock POSIX timestamp onto the monotonic timeline.

    The mapping is taken once, when the entry is scheduled, so a later
    clock step cannot delay every pending entry or fire them all at once.
    """
    return time.monotonic() + (timestamp - time.time())


@dataclass(order=True)
class Entry:
    """Timer entry."""

    eta: float
    priority: int = field(compare=True, default=0)
    fun: Callable[..., Any] = field(compare=False, default=lambda: None)
    args: tuple[Any, ...] = field(compare=False, default_factory=tuple)
    kwargs: dict[str, Any] = field(compare=False, default_factory=dict)
    cancelled: bool = field(compare=False, default=False)

    def __call__(self) -> Any:
        if not self.cancelled:
            return self.fun(*self.args, **self.kwargs)
        return None

    def cancel(self) -> None:
        """Cancel this entry."""
        self.cancelled = True


_Entry = Entry


class Timer:
    """Scheduler for timed events.

    This is a heap-based timer that can be used both synchronously
    and with asyncio.
    """

    Entry = _Entry

    def __init__(
        self,
        max_interval: float | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.max_interval = max_interval or 1.0
        self.on_error = on_error
        self._queue: list[_Entry] = []
        self._lock = threading.Lock()

    @property
    def queue(self) -> list[_Entry]:
        return self._queue

    def __len__(self) -> int:
        return len(self._queue)

    def clear(self) -> None:
        """Clear all scheduled entries."""
        with self._lock:
            self._queue.clear()

    def _enter(
        self,
        eta: float,
        priority: int,
        fun: Callable[..., Any],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> _Entry:
        """Schedule entry at an absolute time on the monotonic clock."""
        kwargs = kwargs or {}
        entry = _Entry(eta=eta, priority=priority, fun=fun, args=args, kwargs=kwargs)
        with self._lock:
            heapq.heappush(self._queue, entry)
        return entry

    def enter_at(
        self,
        entry: _Entry,
        eta: float,
        priority: int | None = None,
    ) -> _Entry:
        """Enter entry at a specific wall-clock time."""
        entry.eta = _monotonic_at(eta)
        if priority is not None:
            entry.priority = priority
        with self._lock:
            heapq.heappush(self._queue, entry)
        return entry

    def enter_after(
        self,
        secs: float,
        entry: _Entry,
        priority: int = 0,
    ) -> _Entry:
        """Enter entry after delay."""
        entry.eta = time.monotonic() + secs
        entry.priority = priority
        with self._lock:
            heapq.heappush(self._queue, entry)
        return entry

    def call_at(
        self,
        eta: float,
        fun: Callable[..., Any],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        priority: int = 0,
    ) -> _Entry:
        """Call function at a specific wall-clock time."""
        return self._enter(_monotonic_at(eta), priority, fun, args, kwargs)

    def call_after(
        self,
        secs: float,
        fun: Callable[..., Any],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        priority: int = 0,
    ) -> _Entry:
        """Call function after delay."""
        return self._enter(time.monotonic() + secs, priority, fun, args, kwargs)

    def call_repeatedly(
        self,
        secs: float,
        fun: Callable[..., Any],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        priority: int = 0,
    ) -> _Entry:
        """Call function repeatedly."""

        def _repeat() -> None:
            try:
                fun(*args, **(kwargs or {}))
            finally:
                if not entry.cancelled:
                    entry.eta = time.monotonic() + secs
                    with self._lock:
                        heapq.heappush(self._queue, entry)

        entry = _Entry(eta=time.monotonic() + secs, priority=priority, fun=_repeat)
        with self._lock:
            heapq.heappush(self._queue, entry)
        return entry

    def cancel(self, entry: _Entry) -> None:
        """Cancel a scheduled entry."""
        entry.cancel()

    def apply_entry(self, entry: _Entry) -> float | None:
        """Apply scheduled entry."""
        if entry.cancelled:
            return None
        try:
            entry()
        except Exception as exc:
            if self.on_error:
                self.on_error(exc)
            else:
                logger.error("Timer error: %r", exc, exc_info=True)
        return None

    def empty(self) -> bool:
        """Return True if the queue is empty."""
        return not self._queue

    def stop(self) -> None:
        """Stop the timer (clear all entries)."""
        self.clear()

    def join(self, timeout: float | None = None) -> None:
        """No-op for compatibility (heap timer has no background thread)."""

    def __iter__(self) -> Iterator[tuple[float | None, _Entry | None]]:
        """Iterate over scheduled entries."""
        return self

    def __next__(self) -> tuple[float | None, _Entry | None]:
        """Get next entry to execute."""
        with self._lock:
            if not self._queue:
                return self.max_interval, None

            entry = self._queue[0]
            now = time.monotonic()

            if entry.eta <= now:
                heapq.heappop(self._queue)
                if entry.cancelled:
                    return 0, None
                return 0, entry

            delay = min(entry.eta - now, self.max_interval)
            return delay, None
