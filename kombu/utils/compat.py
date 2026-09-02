# Partially from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Python Compatibility Utilities."""

import numbers
from importlib import metadata as importlib_metadata
from io import UnsupportedOperation

FILENO_ERRORS = (AttributeError, ValueError, UnsupportedOperation)


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
        return None
