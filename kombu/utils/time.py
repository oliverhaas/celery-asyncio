# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Time Utilities."""

__all__ = ("maybe_s_to_ms",)


def maybe_s_to_ms(v: float | None) -> int | None:
    """Convert seconds to milliseconds, but return None for None."""
    return int(float(v) * 1000.0) if v is not None else v
