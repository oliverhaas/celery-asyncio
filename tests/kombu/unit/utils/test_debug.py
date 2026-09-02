import logging
from unittest.mock import Mock, patch

from kombu.utils.debug import setup_logging


def test_setup_logging_adds_handlers_sets_level():
    with patch("kombu.utils.debug.get_logger") as get_logger:
        logger = get_logger.return_value = Mock()
        setup_logging(loggers=["kombu.test"])

        get_logger.assert_called_with("kombu.test")

        logger.addHandler.assert_called()
        logger.setLevel.assert_called_with(logging.DEBUG)


def test_setup_logging_without_a_level_keeps_the_logger_level():
    # setLevel(None) raises TypeError, so the documented `loglevel=None`
    # crashed instead of leaving the level alone.
    logger = logging.getLogger("kombu.test.nolevel")
    logger.setLevel(logging.WARNING)
    try:
        setup_logging(None, loggers=["kombu.test.nolevel"])

        assert logger.level == logging.WARNING
        assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)
    finally:
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)
