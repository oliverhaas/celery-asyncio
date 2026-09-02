"""Promise utilities for callback chaining."""

import weakref
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from celery.utils.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = (
    "Thenable",
    "promise",
    "starpromise",
    "barrier",
)

logger = get_logger(__name__)


class Thenable(ABC):
    """Abstract base class for promise-like objects.

    Classes that implement `then()` can register with this ABC.
    """

    @abstractmethod
    def then(self, callback: Callable, on_error: Callable | None = None) -> Thenable:
        """Add callback to be called when promise is fulfilled."""
        raise NotImplementedError()


def _weaken(fun: Callable) -> Any:
    """Return a weak reference to ``fun``, or ``fun`` if it cannot be weakened.

    A bound method is rebuilt on every attribute lookup, so a plain
    :class:`weakref.ref` to one is dead on arrival; :class:`weakref.WeakMethod`
    follows the object the method is bound to instead.
    """
    try:
        if hasattr(fun, "__self__") and hasattr(fun, "__func__"):
            return weakref.WeakMethod(fun)
        return weakref.ref(fun)
    except TypeError:
        return fun


class promise:
    """Simple promise for callback chaining.

    This is a minimal implementation that supports the `then()` pattern
    for chaining callbacks.
    """

    def __init__(
        self,
        fun: Callable[..., Any] | None = None,
        *args: Any,
        on_error: Callable[[Exception], Any] | None = None,
        weak: bool = False,
        **kwargs: Any,
    ) -> None:
        if fun is not None and weak:
            fun = _weaken(fun)
        self._fun = fun
        self._weak = weak
        self.args = args
        self.kwargs = kwargs
        self.on_error = on_error
        self._callbacks: list[tuple[Callable, Callable | None]] = []
        self._value: Any = None
        self._ready = False
        self._failed = False

    @property
    def ready(self) -> bool:
        """Whether this promise has been fulfilled."""
        return self._ready

    def _get_fun(self) -> Callable | None:
        """Get the function, dereferencing if weak."""
        if self._fun is None:
            return None
        if self._weak and isinstance(self._fun, weakref.ref):
            return self._fun()
        return self._fun

    def _fulfil(self, value: Any) -> Any:
        self._value = value
        self._ready = True
        for callback, error_handler in self._callbacks:
            _call_back(callback, value, error_handler)
        return value

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Execute the promise."""
        fun = self._get_fun()
        call_args = args or self.args
        if fun is None:
            # No function, act as a simple "event" marker.
            return self._fulfil(call_args[0] if call_args else None)
        try:
            call_kwargs = {**self.kwargs, **kwargs} if kwargs else self.kwargs
            result = fun(*call_args, **call_kwargs)
        except Exception as exc:
            self._failed = True
            if self.on_error:
                self.on_error(exc)
            raise
        return self._fulfil(result)

    def then(
        self,
        callback: Callable,
        on_error: Callable | None = None,
    ) -> promise:
        """Add callback to be called when this promise is fulfilled."""
        if self._ready:
            # Already fulfilled, call immediately
            _call_back(callback, self._value, on_error)
        else:
            self._callbacks.append((callback, on_error))
        return self

    def throw(self, exc: Exception, tb: Any = None) -> None:
        """Signal that the promise failed by re-raising the exception."""
        self._failed = True
        if tb is not None:
            exc.__traceback__ = tb
        if self.on_error:
            self.on_error(exc)
        raise exc


def _call_back(callback: Callable, value: Any, error_handler: Callable | None) -> None:
    """Run one chained callback, reporting rather than swallowing its errors.

    A callback that raises must not stop its siblings from running, so the
    exception goes to the error handler, or to the log when there is none.
    """
    try:
        callback(value)
    except Exception as exc:
        if error_handler is not None:
            error_handler(exc)
        else:
            logger.exception("Error in promise callback %r: %r", callback, exc)


class starpromise(promise):
    """Promise that unpacks arguments when called."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Execute the promise, unpacking args if they're a tuple."""
        fun = self._get_fun()
        if fun is None:
            return None
        # If single argument is iterable, unpack it
        if args and len(args) == 1 and hasattr(args[0], "__iter__") and not isinstance(args[0], (str, bytes)):
            args = tuple(args[0])
        return super().__call__(*args, **kwargs)


class barrier:
    """Calls a callback once every result it waits for has completed.

    A result reports completion by calling the barrier, which is what
    ``result.then(barrier)`` arranges. The callback runs once the last of them
    has reported in and the barrier has been finalized, whichever happens
    later, so a barrier still being filled cannot fire early.

    Passing ``results`` hands over a complete set: they are subscribed to and
    the barrier is finalized at once, so it fires as soon as they are all done.
    A barrier built without them is filled through :meth:`add`, or by bumping
    :attr:`size` and subscribing by hand where the subscription has to be weak,
    and whoever fills it calls :meth:`finalize`.
    """

    def __init__(self, results: list | None = None, callback: Callable | None = None) -> None:
        self._callback = callback
        self._arrived = 0
        self._ready = False
        self.cancelled = False
        self.finalized = False
        self.size = 0
        if results is not None:
            for result in results:
                self.add(result)
            self.finalize()

    @property
    def ready(self) -> bool:
        """Return True if all results are ready."""
        return self._ready

    def add(self, result: Any) -> None:
        """Wait for one more result, reopening the barrier if it already fired."""
        if self.cancelled:
            return
        self.size += 1
        self._ready = False
        result.then(self)

    def cancel(self) -> None:
        """Stop the barrier from ever firing."""
        self.cancelled = True

    def __call__(self, result: Any = None) -> None:
        """Record that one of the results completed."""
        if self._ready or self.cancelled:
            return
        self._arrived += 1
        self._maybe_fire()

    def then(self, callback: Callable, on_error: Callable | None = None) -> barrier:
        """Set callback to be called when all results are ready."""
        self._callback = callback
        if self._ready:
            callback()
        return self

    def finalize(self) -> None:
        """Signal that no more results will be added."""
        self.finalized = True
        self._maybe_fire()

    def _maybe_fire(self) -> None:
        if self._ready or self.cancelled or not self.finalized or self._arrived < self.size:
            return
        self._ready = True
        if self._callback is not None:
            self._callback()
