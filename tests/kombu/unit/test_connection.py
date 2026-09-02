"""Tests for kombu.connection - async Connection."""

import asyncio

import pytest

from kombu import Connection
from kombu.utils.eventloop import default_loop_runner


class test_Connection:
    """Tests for Connection class."""

    def test_init_redis(self):
        conn = Connection("redis://localhost:6379")
        assert conn._url == "redis://localhost:6379"
        assert conn._scheme == "redis"
        assert not conn.is_connected
        assert conn.transport is None

    def test_init_memory(self):
        conn = Connection("memory://")
        assert conn._scheme == "memory"

    def test_init_unsupported_scheme(self):
        with pytest.raises(ValueError, match="Unsupported transport scheme"):
            Connection("ftp://localhost")

    def test_init_default_url(self):
        conn = Connection()
        assert conn._url == "redis://localhost:6379"

    def test_repr(self):
        conn = Connection("memory://")
        r = repr(conn)
        assert "memory://" in r
        assert "connected=False" in r

    def test_connected_alias(self):
        conn = Connection("memory://")
        assert conn.connected is conn.is_connected

    async def test_connect(self):
        conn = Connection("memory://")
        result = await conn.connect()
        assert result is conn
        assert conn.is_connected
        assert conn.transport is not None
        await conn.close()

    async def test_close(self):
        conn = Connection("memory://")
        await conn.connect()
        assert conn.is_connected
        await conn.close()
        assert not conn.is_connected
        assert conn.transport is None

    async def test_close_idempotent(self):
        conn = Connection("memory://")
        await conn.connect()
        await conn.close()
        await conn.close()  # Should not raise

    async def test_channel(self):
        conn = Connection("memory://")
        channel = await conn.channel()
        assert channel is not None
        assert conn.is_connected  # Auto-connected
        await conn.close()

    async def test_default_channel(self):
        conn = Connection("memory://")
        ch1 = await conn.default_channel()
        ch2 = await conn.default_channel()
        assert ch1 is ch2  # Same instance
        await conn.close()

    async def test_context_manager(self):
        async with Connection("memory://") as conn:
            assert conn.is_connected
        assert not conn.is_connected

    async def test_release_alias(self):
        conn = Connection("memory://")
        await conn.connect()
        await conn.release()
        assert not conn.is_connected

    def test_clone(self):
        conn = Connection("memory://", transport_options={"foo": "bar"})
        cloned = conn.clone()
        assert cloned._url == conn._url
        assert cloned._transport_options == conn._transport_options
        assert cloned is not conn

    def test_a_broker_setting_that_belongs_in_the_url_is_rejected(self):
        with pytest.raises(TypeError, match="userid"):
            Connection("memory://", userid="guest")

    def test_clone_override(self):
        conn = Connection("memory://")
        cloned = conn.clone(hostname="redis://localhost")
        assert cloned._url == "redis://localhost"

    async def test_ensure_connection(self):
        conn = Connection("memory://")
        result = await conn.ensure_connection(max_retries=3)
        assert result is conn
        assert conn.is_connected
        await conn.close()

    async def test_ensure_connection_retry_callbacks(self):
        conn = Connection("memory://")
        calls = []

        async def connect_once():
            if not calls:
                calls.append("boom")
                raise OSError("broker down")

        conn.connect = connect_once
        # celery passes `maybe_shutdown` here, which takes no arguments.
        result = await conn.ensure_connection(
            errback=lambda exc, interval: calls.append(("errback", interval)),
            max_retries=3,
            interval_start=0,
            interval_step=0,
            callback=lambda: calls.append("callback"),
        )
        assert result is conn
        assert calls == ["boom", ("errback", 0), "callback"]

    async def test_drain_events_timeout(self):
        async with Connection("memory://") as conn:
            # With no consumers and a timeout, should raise TimeoutError
            with pytest.raises(TimeoutError):
                await conn.drain_events(timeout=0.01)

    def test_producer_factory(self):
        conn = Connection("memory://")
        producer = conn.Producer()
        assert producer is not None
        assert producer._connection is conn

    def test_consumer_factory(self):
        from kombu import Queue

        conn = Connection("memory://")
        queue = Queue("test_q")
        consumer = conn.Consumer([queue])
        assert consumer is not None
        assert consumer._connection is conn

    def test_simple_queue_factory(self):
        conn = Connection("memory://")
        sq = conn.SimpleQueue("test")
        assert sq is not None

    def test_connection_errors(self):
        conn = Connection("memory://")
        errors = conn.connection_errors
        assert isinstance(errors, tuple)
        assert all(issubclass(e, Exception) for e in errors)

    async def test_connection_errors_from_transport(self):
        async with Connection("memory://") as conn:
            errors = conn.connection_errors
            assert isinstance(errors, tuple)

    def test_channel_errors(self):
        conn = Connection("memory://")
        errors = conn.channel_errors
        assert isinstance(errors, tuple)
        assert all(issubclass(e, Exception) for e in errors)

    def test_error_tuples_are_the_transports_before_connecting(self):
        from kombu.transport.valkey_redis import Transport

        conn = Connection("redis://localhost:6379")
        assert conn.transport is None
        assert conn.connection_errors == Transport.connection_errors
        assert conn.channel_errors == Transport.channel_errors
        assert conn.resource_locked_errors == Transport.resource_locked_errors

    def test_as_uri(self):
        conn = Connection("redis://user:secret@localhost:6379/0")
        uri = conn.as_uri()
        assert "secret" not in uri
        assert "**" in uri
        assert "localhost" in uri

    def test_as_uri_include_password(self):
        conn = Connection("redis://user:secret@localhost:6379/0")
        uri = conn.as_uri(include_password=True)
        assert "secret" in uri

    def test_as_uri_no_password(self):
        conn = Connection("memory://")
        uri = conn.as_uri()
        assert "memory://" in uri

    def test_info(self):
        conn = Connection("memory://")
        info = conn.info()
        assert isinstance(info, dict)
        assert "transport" in info
        assert info["transport"] == "memory"
        assert "is_connected" in info

    async def test_info_connected(self):
        async with Connection("memory://") as conn:
            info = conn.info()
            assert info["is_connected"] is True
            assert info["driver_type"] == "memory"


class test_sync_context_manager:
    """The sync `with Connection(...)` path, used by Flower and `worker --purge`."""

    @staticmethod
    def _recording(conn, loops):
        """Wrap connect/close so each records the loop it was awaited on."""
        connect, close = conn.connect, conn.close

        async def recording_connect():
            loops.append(asyncio.get_running_loop())
            return await connect()

        async def recording_close():
            loops.append(asyncio.get_running_loop())
            await close()

        conn.connect, conn.close = recording_connect, recording_close

    def test_enter_and_exit_run_on_one_loop(self):
        conn = Connection("memory://")
        loops = []
        self._recording(conn, loops)

        with conn as entered:
            assert entered is conn
            assert conn.is_connected

        assert not conn.is_connected
        assert len(loops) == 2
        assert loops[0] is loops[1]

    def test_body_reaches_the_same_loop_as_enter(self):
        conn = Connection("memory://")
        loops = []
        self._recording(conn, loops)

        async def body():
            return asyncio.get_running_loop(), await conn.default_channel()

        with conn:
            body_loop, channel = default_loop_runner().run(body())

        assert channel is not None
        assert body_loop is loops[0]

    def test_consecutive_blocks_share_one_loop(self):
        conn = Connection("memory://")
        loops = []
        self._recording(conn, loops)

        with conn:
            pass
        with conn:
            pass

        assert len(loops) == 4
        assert len(set(map(id, loops))) == 1

    async def test_works_from_inside_a_running_loop(self):
        conn = Connection("memory://")
        loops = []
        self._recording(conn, loops)
        caller_loop = asyncio.get_running_loop()

        with conn:
            assert conn.is_connected

        assert not conn.is_connected
        assert loops[0] is loops[1]
        assert loops[0] is not caller_loop


class test_Connection_default_channel:
    async def test_concurrent_first_callers_share_one_channel(self):
        conn = Connection("memory://")
        await conn.connect()
        create_channel = conn.transport.create_channel
        opened = 0

        async def slow_create_channel():
            nonlocal opened
            opened += 1
            await asyncio.sleep(0)
            return await create_channel()

        conn.transport.create_channel = slow_create_channel
        try:
            first, second = await asyncio.gather(conn.default_channel(), conn.default_channel())
            assert first is second
            assert opened == 1
        finally:
            await conn.close()


class test_Connection_close:
    async def test_a_failing_close_leaves_the_connection_retryable(self):
        conn = Connection("memory://")
        await conn.connect()
        transport = conn.transport
        attempts = []

        async def flaky_close():
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("broker went away")
            transport._connected = False

        transport.close = flaky_close

        with pytest.raises(OSError, match="broker went away"):
            await conn.close()

        assert conn.transport is transport
        assert conn.is_connected

        await conn.close()
        assert conn.transport is None
        assert not conn.is_connected
