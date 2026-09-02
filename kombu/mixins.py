"""Mixins - Pure asyncio implementation."""

import asyncio
from contextlib import asynccontextmanager
from functools import partial
from itertools import count
from typing import TYPE_CHECKING, Any

from .common import ignore_errors
from .log import get_logger
from .messaging import Consumer, Producer
from .utils.encoding import safe_repr
from .utils.limits import TokenBucket
from .utils.objects import cached_property

if TYPE_CHECKING:
    from .connection import Connection
    from .transport.base import Channel

__all__ = ("ConsumerMixin", "ConsumerProducerMixin")

# ConsumerMixin.Consumer shadows the imported name inside the class body, which
# makes the bare name ambiguous in annotations there.
_Consumer = Consumer

logger = get_logger(__name__)
debug, info, warn, error = (logger.debug, logger.info, logger.warning, logger.error)

W_CONN_LOST = """\
Connection to broker lost, trying to re-establish connection...\
"""

W_CONN_ERROR = """\
Broker connection error, trying again in %s seconds: %r.\
"""


class ConsumerMixin:
    """Convenience mixin for implementing async consumer programs.

    Pure asyncio implementation.

    The basic class would need a :attr:`connection` attribute
    which must be a :class:`~kombu.Connection` instance,
    and define a :meth:`get_consumers` method that returns a list
    of :class:`kombu.Consumer` instances to use.

    Example:
        class Worker(ConsumerMixin):
            task_queue = Queue('tasks', Exchange('tasks'), 'tasks')

            def __init__(self, connection):
                self.connection = connection

            def get_consumers(self, Consumer, channel):
                return [Consumer(queues=[self.task_queue],
                                 callbacks=[self.on_task])]

            async def on_task(self, body, message):
                print(f'Got task: {body!r}')
                await message.ack()

        # Run the worker:
        async with Connection('redis://localhost') as conn:
            worker = Worker(conn)
            await worker.run()

    Methods:
        * extra_context(connection, channel)

            Optional extra async context manager that will be entered
            after the connection and consumers have been set up.
            Takes arguments (connection, channel).

        * on_connection_error(exc, interval)

            Handler called if the connection is lost or unavailable.
            Takes arguments (exc, interval), where interval is the time
            in seconds when the connection will be retried.
            The default handler will log the exception.

        * on_connection_revived()

            Handler called as soon as the connection is re-established
            after connection failure. Takes no arguments.

        * on_consume_ready(connection, channel, consumers)

            Handler called when the consumer is ready to accept messages.
            Takes arguments (connection, channel, consumers).

        * on_consume_end(connection, channel)

            Handler called after the consumers are canceled.
            Takes arguments (connection, channel).

        * on_iteration()

            Handler called for every iteration while draining events.
            Takes no arguments.

        * on_decode_error(message, exc)

            Handler called if a consumer was unable to decode
            the body of a message. Takes arguments (message, exc).
    """

    #: The connection to use
    connection: Connection

    #: maximum number of retries trying to re-establish the connection,
    #: if the connection is lost/unavailable.
    connect_max_retries: int | None = None

    #: When this is set to true the consumer should stop consuming
    #: and return, so that it can be joined if it is the implementation
    #: of a task.
    should_stop: bool = False

    def get_consumers(self, ConsumerFactory, channel: Channel) -> list[_Consumer]:
        """Return list of consumers.

        Override this method to define the consumers.

        Args:
            ConsumerFactory: Partial Consumer class bound to channel.
            channel: The channel to use.

        Returns:
            List of Consumer instances.
        """
        raise NotImplementedError("Subclass responsibility")

    async def on_connection_revived(self) -> None:
        """Called when connection is re-established after failure."""

    async def on_consume_ready(
        self,
        connection: Connection,
        channel: Channel,
        consumers: list[_Consumer],
        **kwargs: Any,
    ) -> None:
        """Called when consumer is ready to receive messages."""

    async def on_consume_end(self, connection: Connection, channel: Channel) -> None:
        """Called after consumers are canceled."""

    async def on_iteration(self) -> None:
        """Called for every iteration while draining events."""

    async def on_decode_error(self, message: Any, exc: Exception) -> None:
        """Called when message body cannot be decoded."""
        error(
            "Can't decode message body: %r (type:%r encoding:%r raw:%r')",
            exc,
            message.content_type,
            message.content_encoding,
            safe_repr(message.body),
        )
        await message.ack()

    def on_connection_error(self, exc: Exception, interval: float) -> None:
        """Called when connection error occurs."""
        warn(W_CONN_ERROR, interval, exc, exc_info=1)

    @asynccontextmanager
    async def extra_context(self, connection: Connection, channel: Channel):
        """Optional extra async context manager.

        Override to add custom context around consumption.
        """
        yield

    async def run(self, _tokens: int = 1, **kwargs: Any) -> None:
        """Run the consumer.

        This will loop forever (or until should_stop is True),
        consuming messages and handling connection errors.
        """
        restart_limit = self.restart_limit

        while not self.should_stop:
            try:
                if restart_limit.can_consume(_tokens):
                    async for _ in self.consume(limit=None, **kwargs):
                        pass
                else:
                    await asyncio.sleep(restart_limit.expected_time(_tokens))
            except Exception:
                warn(W_CONN_LOST, exc_info=1)
                await asyncio.sleep(1.0)  # Brief pause before retry

    @asynccontextmanager
    async def consumer_context(self, **kwargs: Any):
        """Async context manager for consumer setup.

        Yields (connection, channel, consumers) tuple.
        """
        async with self.Consumer() as (connection, channel, consumers), self.extra_context(connection, channel):
            await self.on_consume_ready(connection, channel, consumers, **kwargs)
            yield connection, channel, consumers

    async def consume(
        self,
        limit: int | None = None,
        timeout: float | None = None,
        safety_interval: float = 1.0,
        **kwargs: Any,
    ):
        """Async generator for consuming messages.

        Args:
            limit: Maximum number of messages to consume.
            timeout: Overall timeout in seconds.
            safety_interval: Timeout for each drain_events call.
            **kwargs: Additional arguments passed to consumer_context.

        Yields:
            None after each message is processed.
        """
        elapsed = 0.0

        async with self.consumer_context(**kwargs) as (conn, channel, consumers):
            for _i in range(limit) if limit else count():
                if self.should_stop:
                    break

                await self.on_iteration()

                try:
                    await conn.drain_events(timeout=safety_interval)
                    yield
                    elapsed = 0.0
                except TimeoutError:
                    elapsed += safety_interval
                    if timeout and elapsed >= timeout:
                        raise
                except OSError:
                    if not self.should_stop:
                        raise

        debug("consume exiting")

    async def maybe_conn_error(self, fun) -> Any:
        """Execute function ignoring connection errors."""
        async with ignore_errors(self.connection):
            result = fun()
            if asyncio.iscoroutine(result):
                return await result
            return result

    def create_connection(self) -> Connection:
        """Create a new connection (clone of current)."""
        return self.connection.clone()

    @asynccontextmanager
    async def establish_connection(self):
        """Async context manager to establish and yield connection."""
        conn = self.create_connection()
        try:
            await conn.ensure_connection(
                errback=self.on_connection_error,
                max_retries=self.connect_max_retries,
            )
            yield conn
        finally:
            await conn.close()

    @asynccontextmanager
    async def Consumer(self):
        """Async context manager for consumer setup.

        Yields (connection, channel, consumers) tuple.
        """
        async with self.establish_connection() as conn:
            await self.on_connection_revived()
            info("Connected to %s", conn._url)

            channel = await conn.default_channel()
            ConsumerFactory = partial(
                Consumer,
                conn,
                channel=channel,
            )

            consumers = self.get_consumers(ConsumerFactory, channel)

            # Enter all consumers
            for consumer in consumers:
                await consumer.consume()

            try:
                yield conn, channel, consumers
            finally:
                # Cancel all consumers
                for consumer in consumers:
                    await consumer.cancel()

                debug("Consumers canceled")
                await self.on_consume_end(conn, channel)

        debug("Connection closed")

    @cached_property
    def restart_limit(self) -> TokenBucket:
        """Rate limiter for connection restarts."""
        return TokenBucket(1)


class ConsumerProducerMixin(ConsumerMixin):
    """Consumer and Producer mixin.

    Version of ConsumerMixin with separate connection for publishing.

    Example:
        class Worker(ConsumerProducerMixin):

            def __init__(self, connection):
                self.connection = connection

            def get_consumers(self, Consumer, channel):
                return [Consumer(queues=[Queue('foo')],
                                 callbacks=[self.handle_message])]

            async def handle_message(self, body, message):
                await self.producer.publish(
                    {'message': 'hello to you'},
                    exchange='',
                    routing_key=message.properties['reply_to'],
                )
                await message.ack()
    """

    _producer_connection: Connection | None = None
    _producer: Producer | None = None

    async def on_consume_end(self, connection: Connection, channel: Channel) -> None:
        """Clean up producer connection on consume end."""
        if self._producer_connection is not None:
            await self._producer_connection.close()
            self._producer_connection = None
            self._producer = None

    @property
    def producer(self) -> Producer:
        """Get producer instance (creates connection if needed).

        Note: You should call ensure_producer() before using this
        in an async context.
        """
        if self._producer is None:
            raise RuntimeError("Producer not initialized. Call ensure_producer() first.")
        return self._producer

    async def ensure_producer(self) -> Producer:
        """Ensure producer is ready and return it.

        Creates producer connection if not already connected.
        """
        if self._producer is None:
            conn = await self._get_producer_connection()
            self._producer = Producer(conn)
        return self._producer

    async def _get_producer_connection(self) -> Connection:
        """Get or create producer connection."""
        if self._producer_connection is None:
            conn = self.connection.clone()
            await conn.ensure_connection(
                errback=self.on_connection_error,
                max_retries=self.connect_max_retries,
            )
            self._producer_connection = conn
        return self._producer_connection
