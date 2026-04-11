# Partially from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""System information utilities."""

import os
from math import ceil

__all__ = ("load_average",)


def load_average() -> tuple[float, ...]:
    """Return system load average as a triple."""
    return tuple(ceil(l * 1e2) / 1e2 for l in os.getloadavg())
