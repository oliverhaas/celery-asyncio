# Originally from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Exceptions."""

from types import TracebackType

__all__ = (
    "ChannelError",
    "ConnectionError",
    "ContentDisallowed",
    "DecodeError",
    "EncodeError",
    "InconsistencyError",
    "KombuError",
    "MessageStateError",
    "OperationalError",
    "SerializationError",
    "SerializerNotInstalled",
    "reraise",
)


def reraise[BaseExceptionType: BaseException](
    _tp: type[BaseExceptionType],
    value: BaseExceptionType,
    tb: TracebackType | None = None,
) -> BaseExceptionType:
    """Reraise exception."""
    if value.__traceback__ is not tb:
        raise value.with_traceback(tb)
    raise value


class KombuError(Exception):
    """Common subclass for all Kombu exceptions."""


class OperationalError(KombuError):
    """Recoverable message transport connection error."""


class SerializationError(KombuError):
    """Failed to serialize/deserialize content."""


class EncodeError(SerializationError):
    """Cannot encode object."""


class DecodeError(SerializationError):
    """Cannot decode object."""


class MessageStateError(KombuError):
    """The message has already been acknowledged."""


class SerializerNotInstalled(KombuError):
    """Support for the requested serialization type is not installed."""


class ContentDisallowed(SerializerNotInstalled):
    """Consumer does not allow this content-type."""


class ConnectionError(KombuError):
    """Connection error."""


class ChannelError(KombuError):
    """Channel error."""


class InconsistencyError(ConnectionError):
    """Data or environment has been found to be inconsistent.

    Depending on the cause it may be possible to retry the operation.
    """
