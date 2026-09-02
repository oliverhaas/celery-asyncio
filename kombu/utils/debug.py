# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Debugging support."""

import logging

from kombu.log import get_logger

__all__ = ("setup_logging",)


def setup_logging(loglevel: int | None = logging.DEBUG, loggers: list[str] | None = None) -> None:
    """Setup logging to stdout.

    A `loglevel` of :const:`None` attaches the handler and leaves each
    logger's level as it is.
    """
    loggers = ["kombu.connection", "kombu.channel"] if not loggers else loggers
    for logger_name in loggers:
        logger = get_logger(logger_name)
        logger.addHandler(logging.StreamHandler())
        if loglevel is not None:
            logger.setLevel(loglevel)
