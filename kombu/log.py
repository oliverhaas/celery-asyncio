# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Logging Utilities."""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from logging import Logger

__all__ = ("LOG_LEVELS", "get_logger")

# Deliberately bidirectional: name -> level and level -> name in one mapping.
LOG_LEVELS: dict[str | int, str | int] = {}
LOG_LEVELS.update(logging._nameToLevel.items())
LOG_LEVELS.update(logging._levelToName.items())
LOG_LEVELS.setdefault("FATAL", logging.FATAL)
LOG_LEVELS.setdefault(logging.FATAL, "FATAL")


def get_logger(logger: str | Logger):
    """Get logger by name."""
    if isinstance(logger, str):
        logger = logging.getLogger(logger)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger
