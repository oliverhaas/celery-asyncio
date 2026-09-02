# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""DEPRECATED - Import from modules below."""

from .div import emergency_dump_state
from .functional import fxrange, fxrangemax, maybe_list, reprcall, reprkwargs, retry_over_time
from .imports import symbol_by_name
from .objects import cached_property
from .uuid import uuid

__all__ = (
    "cached_property",
    "emergency_dump_state",
    "fxrange",
    "fxrangemax",
    "maybe_list",
    "reprcall",
    "reprkwargs",
    "retry_over_time",
    "symbol_by_name",
    "uuid",
)
