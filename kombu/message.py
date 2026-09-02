# Partially from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Message class for pure asyncio Kombu."""

import sys
from collections.abc import Callable, Iterable
from collections.abc import Set as AbstractSet
from typing import TYPE_CHECKING, Any

from .compression import decompress
from .exceptions import MessageStateError, reraise
from .serialization import loads, prepare_accept_content

if TYPE_CHECKING:
    from .transport.base import Channel

__all__ = ("Message",)

ACK_STATES = {"ACK", "REJECTED", "REQUEUED"}


class Message:
    """Base class for received messages.

    Keyword Arguments:
        body: Message body.
        delivery_tag: Unique message identifier for acknowledgment.
        content_type: The message content type.
        content_encoding: The message encoding.
        delivery_info: Delivery metadata (exchange, routing_key, etc.).
        properties: Message properties.
        headers: Message headers.
        postencode: Encoding to apply to a body that arrived as text.
        accept: Content types the body may be decoded as, None for any.
        channel: The channel that the message was received on.
    """

    MessageStateError = MessageStateError

    errors: list | None = None

    __slots__ = (
        "__dict__",
        "_accept",
        "_decoded_cache",
        "_state",
        "body",
        "channel",
        "content_encoding",
        "content_type",
        "delivery_info",
        "delivery_tag",
        "headers",
        "properties",
    )

    def __init__(
        self,
        body: bytes | str | None = None,
        delivery_tag: str | None = None,
        content_type: str | None = None,
        content_encoding: str | None = None,
        delivery_info: dict | None = None,
        properties: dict | None = None,
        headers: dict | None = None,
        postencode: str | None = None,
        accept: AbstractSet[str] | None = None,
        channel: "Channel | None" = None,  # noqa: UP037
    ):
        delivery_info = {} if not delivery_info else delivery_info
        self.errors = [] if self.errors is None else self.errors
        self.channel = channel
        self.delivery_tag = delivery_tag
        self.content_type = content_type
        self.content_encoding = content_encoding
        self.delivery_info = delivery_info
        self.headers = headers or {}
        self.properties = properties or {}
        self._decoded_cache = None
        self._state = "RECEIVED"
        self.accept = accept

        compression = self.headers.get("compression")
        if not self.errors and compression:
            try:
                body = decompress(body, compression)
            except Exception:
                self.errors.append(sys.exc_info())

        if not self.errors and postencode and isinstance(body, str):
            try:
                body = body.encode(postencode)
            except Exception:
                self.errors.append(sys.exc_info())
        self.body = body

    @property
    def accept(self) -> AbstractSet[str] | None:
        """Content types this message may be decoded as, or None for any."""
        return self._accept

    @accept.setter
    def accept(self, accept: Iterable[str] | None) -> None:
        # Serializer names are accepted alongside content types, and a decode
        # made under a different restriction is no longer an answer to this one.
        self._accept = prepare_accept_content(accept)
        self._decoded_cache = None

    def _reraise_error(self, callback: Callable | None = None) -> None:
        try:
            reraise(*self.errors[0])  # type: ignore[index]  # ty: ignore[not-subscriptable]
        except Exception as exc:
            if not callback:
                raise
            callback(self, exc)

    async def ack(self, multiple: bool = False) -> None:
        """Acknowledge this message as being processed.

        This will remove the message from the queue.

        Raises:
            MessageStateError: If the message has already been
                acknowledged/requeued/rejected.
        """
        if self.channel is None:
            raise self.MessageStateError("This message does not have a receiving channel")
        if self.channel.no_ack_consumers is not None:
            try:
                consumer_tag = self.delivery_info["consumer_tag"]
            except KeyError:
                pass
            else:
                if consumer_tag in self.channel.no_ack_consumers:
                    return
        if self.acknowledged:
            raise self.MessageStateError(f"Message already acknowledged with state: {self._state}")
        await self.channel.basic_ack(self.delivery_tag, multiple=multiple)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        self._state = "ACK"

    async def ack_log_error(
        self,
        logger: Any,
        errors: tuple[type[Exception], ...],
        multiple: bool = False,
    ) -> None:
        try:
            await self.ack(multiple=multiple)
        except BrokenPipeError as exc:
            logger.critical(
                "Couldn't ack %r, reason:%r",
                self.delivery_tag,
                exc,
                exc_info=True,
            )
            raise
        except errors as exc:
            logger.critical(
                "Couldn't ack %r, reason:%r",
                self.delivery_tag,
                exc,
                exc_info=True,
            )

    async def reject_log_error(
        self,
        logger: Any,
        errors: tuple[type[Exception], ...],
        requeue: bool = False,
    ) -> None:
        try:
            await self.reject(requeue=requeue)
        except errors as exc:
            logger.critical(
                "Couldn't reject %r, reason: %r",
                self.delivery_tag,
                exc,
                exc_info=True,
            )

    async def reject(self, requeue: bool = False) -> None:
        """Reject this message.

        The message will be discarded by the server.

        Raises:
            MessageStateError: If the message has already been
                acknowledged/requeued/rejected.
        """
        if self.channel is None:
            raise self.MessageStateError("This message does not have a receiving channel")
        if self.acknowledged:
            raise self.MessageStateError(f"Message already acknowledged with state: {self._state}")
        await self.channel.basic_reject(self.delivery_tag, requeue=requeue)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        self._state = "REJECTED"

    async def requeue(self) -> None:
        """Reject this message and put it back on the queue.

        Warning:
            You must not use this method as a means of selecting messages
            to process.

        Raises:
            MessageStateError: If the message has already been
                acknowledged/requeued/rejected.
        """
        if self.channel is None:
            raise self.MessageStateError("This message does not have a receiving channel")
        if self.acknowledged:
            raise self.MessageStateError(f"Message already acknowledged with state: {self._state}")
        await self.channel.basic_reject(self.delivery_tag, requeue=True)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        self._state = "REQUEUED"

    def decode(self) -> Any:
        """Deserialize the message body.

        Returns the original python structure sent by the publisher.

        Note:
            The return value is memoized, use `_decode` to force
            re-evaluation.
        """
        if self._decoded_cache is None:
            self._decoded_cache = self._decode()
        return self._decoded_cache

    def _decode(self) -> Any:
        return loads(
            self.body,
            self.content_type,
            self.content_encoding,
            accept=self.accept,
        )

    @property
    def acknowledged(self) -> bool:
        """True if the message has been acknowledged."""
        return self._state in ACK_STATES

    @property
    def payload(self) -> Any:
        """The decoded message body."""
        return self._decoded_cache if self._decoded_cache is not None else self.decode()

    def __repr__(self) -> str:
        body_len = len(self.body) if self.body is not None else None
        return (
            f"<{type(self).__name__} object at {id(self):#x} "
            f"state={self._state!r} content_type={self.content_type!r} "
            f"delivery_tag={self.delivery_tag!r} body_length={body_len}>"
        )
