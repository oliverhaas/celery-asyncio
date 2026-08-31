# Partially from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Python Compatibility Utilities."""

import numbers
from functools import wraps
from importlib import metadata as importlib_metadata
from io import UnsupportedOperation

FILENO_ERRORS = (AttributeError, ValueError, UnsupportedOperation)


def coro(gen):
    """Decorator to mark generator as co-routine."""

    @wraps(gen)
    def wind_up(*args, **kwargs):
        it = gen(*args, **kwargs)
        next(it)
        return it

    return wind_up


def detect_environment():
    """Detect the current environment. Always 'default' in asyncio mode."""
    return "default"


def entrypoints(namespace):
    """Return setuptools entrypoints for namespace."""
    entry_points = importlib_metadata.entry_points(group=namespace)

    return ((ep, ep.load()) for ep in entry_points)


def fileno(f):
    """Get fileno from file-like object."""
    if isinstance(f, numbers.Integral):
        return f
    return f.fileno()


def maybe_fileno(f):
    """Get object fileno, or :const:`None` if not defined."""
    try:
        return fileno(f)
    except FILENO_ERRORS:
        pass


# No-op in asyncio mode (no forking)
register_after_fork = None
