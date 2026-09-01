# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Debugging support."""

import logging
from functools import wraps
from typing import TYPE_CHECKING

from kombu.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from logging import Logger
    from types import TracebackType
    from typing import Any

    from kombu.transport.base import Transport

__all__ = ("Logwrapped", "setup_logging")


def setup_logging(loglevel: int | None = logging.DEBUG, loggers: list[str] | None = None) -> None:
    """Setup logging to stdout."""
    loggers = ["kombu.connection", "kombu.channel"] if not loggers else loggers
    for logger_name in loggers:
        logger = get_logger(logger_name)
        logger.addHandler(logging.StreamHandler())
        logger.setLevel(loglevel)


class Logwrapped:
    """Wrap all object methods, to log on call."""

    def __init__(self, instance: Transport, logger: Logger | None = None, ident: str | None = None):
        self.instance = instance
        self.logger = get_logger(logger or __name__)
        self.ident = ident

    def __getattr__(self, key: str) -> Callable:
        meth = getattr(self.instance, key)

        if not callable(meth):
            return meth

        @wraps(meth)
        def __wrapped(*args: list[Any], **kwargs: dict[str, Any]) -> Callable:
            info = ""
            if self.ident:
                info += self.ident.format(self.instance)
            info += f"{meth.__name__}("
            if args:
                info += ", ".join(map(repr, args))
            if kwargs:
                if args:
                    info += ", "
                info += ", ".join(f"{key}={value!r}" for key, value in kwargs.items())
            info += ")"
            self.logger.debug(info)
            return meth(*args, **kwargs)

        return __wrapped

    # Python looks dunders up on the type, never on the instance, so __getattr__
    # is not consulted for them and `with Logwrapped(channel)` used to raise
    # TypeError. Both protocols are forwarded because everything worth wrapping
    # here is an async context manager, while Connection is also a sync one.
    def __enter__(self) -> Logwrapped:
        self.instance.__enter__()  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return self.instance.__exit__(exc_type, exc_val, exc_tb)  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

    async def __aenter__(self) -> Logwrapped:
        await self.instance.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return await self.instance.__aexit__(exc_type, exc_val, exc_tb)

    def __repr__(self) -> str:
        return repr(self.instance)

    def __dir__(self) -> list[str]:
        return dir(self.instance)
