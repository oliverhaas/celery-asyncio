import logging

from kombu.log import LOG_LEVELS, get_logger


class test_get_logger:
    def test_when_string(self):
        logger = get_logger("foo")

        assert logger is logging.getLogger("foo")
        h1 = logger.handlers[0]
        assert isinstance(h1, logging.NullHandler)

    def test_when_logger(self):
        logger = get_logger(logging.getLogger("foo"))
        h1 = logger.handlers[0]
        assert isinstance(h1, logging.NullHandler)

    def test_with_custom_handler(self):
        logger = logging.getLogger("bar")
        handler = logging.NullHandler()
        logger.addHandler(handler)

        logger = get_logger("bar")
        assert logger.handlers[0] is handler


class test_LOG_LEVELS:
    def test_maps_names_to_levels(self):
        assert LOG_LEVELS["DEBUG"] == logging.DEBUG
        assert LOG_LEVELS["ERROR"] == logging.ERROR
        assert LOG_LEVELS["FATAL"] == logging.FATAL

    def test_maps_levels_back_to_names(self):
        assert LOG_LEVELS[logging.INFO] == "INFO"
        assert LOG_LEVELS[logging.WARNING] == "WARNING"
