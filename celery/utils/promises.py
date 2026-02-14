"""Promise utilities for callback chaining.

Simple promise implementations for celery-asyncio. These provide callback
chaining without the full vine dependency.
"""
from __future__ import annotations

import weakref
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = (
    "Thenable",
    "promise",
    "starpromise",
    "barrier",
)


class Thenable(ABC):
    """Abstract base class for promise-like objects.

    Classes that implement `then()` can register with this ABC.
    """

    @abstractmethod
    def then(self, callback: Callable, on_error: Callable | None = None) -> "Thenable":
        """Add callback to be called when promise is fulfilled."""
        raise NotImplementedError()

    # Override register to return the subclass for use as a decorator
    @classmethod
    def register(cls, subclass: type) -> type:
        """Register a virtual subclass and return it for decorator use."""
        ABC.register(cls, subclass)
        return subclass

# Fix the ABC.register call - it's actually an instance method on ABCMeta
_original_abc_register = ABC.register.__func__

def _thenable_register(cls, subclass: type) -> type:
    """Register a virtual subclass and return it for decorator use."""
    _original_abc_register(cls, subclass)
    return subclass

Thenable.register = classmethod(_thenable_register)


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
            try:
                fun = weakref.ref(fun)
            except TypeError:
                # Not weakly referenceable
                pass
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

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Execute the promise."""
        fun = self._get_fun()
        if fun is None:
            return None
        try:
            call_args = args if args else self.args
            call_kwargs = {**self.kwargs, **kwargs} if kwargs else self.kwargs
            result = fun(*call_args, **call_kwargs)
            self._value = result
            self._ready = True
            # Execute chained callbacks
            for callback, error_handler in self._callbacks:
                try:
                    callback(result)
                except Exception as exc:
                    if error_handler:
                        error_handler(exc)
            return result
        except Exception as exc:
            self._failed = True
            if self.on_error:
                self.on_error(exc)
            raise

    def then(
        self,
        callback: Callable,
        on_error: Callable | None = None,
    ) -> "promise":
        """Add callback to be called when this promise is fulfilled."""
        if self._ready:
            # Already fulfilled, call immediately
            try:
                callback(self._value)
            except Exception as exc:
                if on_error:
                    on_error(exc)
        else:
            self._callbacks.append((callback, on_error))
        return self

    def throw(self, exc: BaseException) -> None:
        """Signal that the promise failed."""
        self._failed = True
        if self.on_error:
            self.on_error(exc)


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
    """Synchronization barrier for multiple promises.

    Waits for multiple results/promises to complete before
    calling the final callback.
    """

    def __init__(self, results: list | None = None, callback: Callable | None = None) -> None:
        self._results = list(results) if results else []
        self._callback = callback
        self._pending = set(range(len(self._results)))
        self._values: dict[int, Any] = {}
        self._ready = False

    def add(self, result: Any) -> None:
        """Add a result to wait for."""
        idx = len(self._results)
        self._results.append(result)
        self._pending.add(idx)

    def __call__(self, result: Any = None) -> Any:
        """Called when all results are ready."""
        self._ready = True
        if self._callback:
            return self._callback(self._values)
        return self._values

    def then(self, callback: Callable, on_error: Callable | None = None) -> "barrier":
        """Set callback to be called when all results are ready."""
        self._callback = callback
        if self._ready:
            callback(self._values)
        return self

    def finalize(self) -> None:
        """Signal that no more results will be added."""
        if not self._pending:
            self()
