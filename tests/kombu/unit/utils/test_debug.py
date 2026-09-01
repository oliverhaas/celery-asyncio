import logging
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from kombu.utils.debug import Logwrapped, setup_logging


def test_setup_logging_adds_handlers_sets_level():
    with patch("kombu.utils.debug.get_logger") as get_logger:
        logger = get_logger.return_value = Mock()
        setup_logging(loggers=["kombu.test"])

        get_logger.assert_called_with("kombu.test")

        logger.addHandler.assert_called()
        logger.setLevel.assert_called_with(logging.DEBUG)


def test_logwrapped_wraps():
    with patch("kombu.utils.debug.get_logger") as get_logger:
        logger = get_logger.return_value = Mock()

        W = Logwrapped(Mock(), "kombu.test")
        get_logger.assert_called_with("kombu.test")
        assert W.instance is not None
        assert W.logger is logger

        W.instance.__repr__ = lambda s: "foo"
        assert repr(W) == "foo"
        W.instance.some_attr = 303
        assert W.some_attr == 303

        W.instance.some_method.__name__ = "some_method"
        W.some_method(1, 2, kw=1)
        W.instance.some_method.assert_called_with(1, 2, kw=1)

        W.some_method()
        W.instance.some_method.assert_called_with()

        W.some_method(kw=1)
        W.instance.some_method.assert_called_with(kw=1)

        W.ident = "ident"
        W.some_method(kw=1)
        logger.debug.assert_called()
        assert "ident" in logger.debug.call_args[0][0]

        assert dir(W) == dir(W.instance)


def test_logwrapped_sync_context_manager_yields_the_wrapper():
    # Dunders are looked up on the type, so __getattr__ never sees them and
    # `with Logwrapped(...)` used to raise TypeError.
    instance = MagicMock()
    instance.__exit__.return_value = None
    W = Logwrapped(instance, "kombu.test")

    with W as entered:
        assert entered is W

    instance.__enter__.assert_called_once_with()
    instance.__exit__.assert_called_once_with(None, None, None)


def test_logwrapped_sync_exit_forwards_suppression():
    instance = MagicMock()
    instance.__exit__.return_value = True

    with Logwrapped(instance, "kombu.test"):
        raise RuntimeError("suppressed")


async def test_logwrapped_async_context_manager_yields_the_wrapper():
    instance = Mock()
    instance.__aenter__ = AsyncMock()
    instance.__aexit__ = AsyncMock(return_value=None)
    W = Logwrapped(instance, "kombu.test")

    async with W as entered:
        assert entered is W

    instance.__aenter__.assert_awaited_once_with()
    instance.__aexit__.assert_awaited_once_with(None, None, None)


async def test_logwrapped_async_exit_forwards_suppression():
    instance = Mock()
    instance.__aenter__ = AsyncMock()
    instance.__aexit__ = AsyncMock(return_value=True)

    async with Logwrapped(instance, "kombu.test"):
        raise RuntimeError("suppressed")
