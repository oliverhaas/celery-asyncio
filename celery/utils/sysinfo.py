"""System information utilities."""

import os
from math import ceil

__all__ = ("load_average",)


def load_average() -> tuple[float, ...]:
    """Return system load average as a triple."""
    return tuple(ceil(l * 1e2) / 1e2 for l in os.getloadavg())
