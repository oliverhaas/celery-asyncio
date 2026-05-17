"""Unit tests for the pure asyncio Redis transport.

All Redis operations are mocked — no Redis server required.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from kombu.entity import Exchange, Queue
from kombu.transport.valkey_redis import (
    BINDING_SEP,
    DEFAULT_MAX_RESTORE_COUNT,
    DEFAULT_VISIBILITY_TIMEOUT,
    MESSAGE_KEY_PREFIX,
    MESSAGES_INDEX_PREFIX,
    MIN_QUEUE_EXPIRES,
    QUEUE_KEY_PREFIX,
    Channel,
    Transport,
    _parse_db_from_url,
    _queue_score,
    _topic_match,
)

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_transport(**opts) -> Transport:
    """Create a Transport with mocked Redis clients."""
    transport = Transport.__new__(Transport)
    transport._url = "redis://localhost:6379"
    transport._options = opts
    transport._client = MagicMock()
    transport._subclient = MagicMock()
    transport._channels = []
    transport._connection_id = "test-conn"
    transport._connected = True
    transport._db = "0"
    return transport


def _make_channel(**opts) -> Channel:
    """Create a Channel with a mocked transport."""
    transport = _make_transport(**opts)
    return Channel(transport, "test-conn")


class _MockPipeline:
    """Async context manager that collects pipeline calls."""

    def __init__(self):
        self.calls = []
        self._mock = AsyncMock()

    async def __aenter__(self):
        return self._mock

    async def __aexit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestQueueScore:
    def test_basic_score(self):
        score = _queue_score(0, 1000.0)
        assert score > 0

    def test_higher_priority_lower_score(self):
        low = _queue_score(0, 1000.0)
        high = _queue_score(255, 1000.0)
        assert high < low

    def test_same_priority_earlier_first(self):
        earlier = _queue_score(5, 1000.0)
        later = _queue_score(5, 2000.0)
        assert earlier < later

    def test_clamps_priority(self):
        score_neg = _queue_score(-10, 1000.0)
        score_zero = _queue_score(0, 1000.0)
        assert score_neg == score_zero

        score_high = _queue_score(999, 1000.0)
        score_max = _queue_score(255, 1000.0)
        assert score_high == score_max


class TestTopicMatch:
    def test_exact(self):
        assert _topic_match("user.created", "user.created") is True

    def test_star(self):
        assert _topic_match("user.created", "user.*") is True
        assert _topic_match("user.profile.updated", "user.*") is False

    def test_hash(self):
        assert _topic_match("user.profile.updated", "user.#") is True
        assert _topic_match("user.created", "user.#") is True

    def test_no_match(self):
        assert _topic_match("user.created", "order.*") is False


class TestParseDbFromUrl:
    def test_default_db(self):
        assert _parse_db_from_url("redis://localhost:6379") == "0"

    def test_explicit_db(self):
        assert _parse_db_from_url("redis://localhost:6379/3") == "3"

    def test_empty_path(self):
        assert _parse_db_from_url("redis://localhost:6379/") == "0"


# ---------------------------------------------------------------------------
# Channel key helpers
# ---------------------------------------------------------------------------


class TestChannelKeyHelpers:
    def test_prefixed_no_prefix(self):
        ch = _make_channel()
        assert ch._prefixed("foo") == "foo"

    def test_prefixed_with_prefix(self):
        ch = _make_channel(global_keyprefix="myapp:")
        assert ch._prefixed("foo") == "myapp:foo"

    def test_unprefixed(self):
        ch = _make_channel(global_keyprefix="myapp:")
        assert ch._unprefixed("myapp:foo") == "foo"
        assert ch._unprefixed("other:foo") == "other:foo"

    def test_queue_key(self):
        ch = _make_channel(global_keyprefix="p:")
        assert ch._queue_key("celery") == "p:queue:celery"

    def test_message_key(self):
        ch = _make_channel(global_keyprefix="p:")
        assert ch._message_key("tag1") == "p:message:tag1"

    def test_messages_index_key(self):
        ch = _make_channel(global_keyprefix="p:")
        assert ch._messages_index_key("celery") == "p:messages_index:celery"


# ---------------------------------------------------------------------------
# Channel init
# ---------------------------------------------------------------------------


class TestChannelInit:
    def test_defaults(self):
        ch = _make_channel()
        assert ch._visibility_timeout == DEFAULT_VISIBILITY_TIMEOUT
        assert ch._message_ttl == -1
        assert ch._max_restore_count is None
        assert ch._consume_fast_mode is True
        assert ch._global_keyprefix == ""

    def test_custom_options(self):
        ch = _make_channel(
            visibility_timeout=120,
            message_ttl=3600,
            max_restore_count=5,
            global_keyprefix="test:",
        )
        assert ch._visibility_timeout == 120
        assert ch._message_ttl == 3600
        assert ch._max_restore_count == 5
        assert ch._global_keyprefix == "test:"

    def test_fanout_prefix_default(self):
        ch = _make_channel()
        assert ch._fanout_prefix == "/0."

    def test_fanout_prefix_custom(self):
        ch = _make_channel(fanout_prefix="/custom/{db}.")
        assert ch._fanout_prefix == "/custom/0."

    def test_fanout_prefix_disabled(self):
        ch = _make_channel(fanout_prefix=False)
        assert ch._fanout_prefix == ""


# ---------------------------------------------------------------------------
# Exchange operations
# ---------------------------------------------------------------------------


class TestExchangeOps:
    async def test_declare_exchange(self):
        ch = _make_channel()
        ex = Exchange("test_ex", type="direct")
        await ch.declare_exchange(ex)
        assert "test_ex" in ch._exchanges
        assert ch._exchanges["test_ex"]["type"] == "direct"

    async def test_exchange_delete(self):
        ch = _make_channel()
        ch.client.delete = AsyncMock()
        ch._exchanges["test_ex"] = {"type": "direct"}
        ch._bindings["test_ex"] = [("q1", "rk1")]
        await ch.exchange_delete("test_ex")
        assert "test_ex" not in ch._exchanges
        assert "test_ex" not in ch._bindings


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------


class TestQueueOps:
    async def test_declare_queue_auto_name(self):
        ch = _make_channel()
        ch.client.sadd = AsyncMock()
        q = Queue("")
        name = await ch.declare_queue(q)
        assert name.startswith("amq.gen-")

    async def test_declare_queue_with_expires(self):
        ch = _make_channel()
        ch.client.sadd = AsyncMock()
        q = Queue("test_q")
        q.queue_arguments = {"x-expires": 20_000}
        q.exchange = Exchange("ex", type="direct")
        q.routing_key = "rk"
        await ch.declare_queue(q)
        assert ch._expires["test_q"] == 20_000

    async def test_declare_queue_expires_clamped(self):
        ch = _make_channel()
        ch.client.sadd = AsyncMock()
        q = Queue("test_q")
        q.queue_arguments = {"x-expires": 5_000}  # Below MIN_QUEUE_EXPIRES
        await ch.declare_queue(q)
        assert ch._expires["test_q"] == MIN_QUEUE_EXPIRES

    async def test_queue_bind(self):
        ch = _make_channel()
        ch.client.sadd = AsyncMock()
        await ch.queue_bind("q1", "ex1", "rk1")
        assert ("q1", "rk1") in ch._bindings["ex1"]
        ch.client.sadd.assert_called_once()

    async def test_queue_unbind(self):
        ch = _make_channel()
        ch.client.srem = AsyncMock()
        ch._bindings["ex1"] = [("q1", "rk1")]
        await ch.queue_unbind("q1", "ex1", "rk1")
        assert ("q1", "rk1") not in ch._bindings["ex1"]

    async def test_queue_purge_cleans_hashes(self):
        ch = _make_channel()
        ch.client.zcard = AsyncMock(return_value=2)
        ch.client.zrange = AsyncMock(
            side_effect=[
                [b"tag1", b"tag2"],  # queue tags
                [b"tag1", b"tag3"],  # index tags (tag3 is extra)
            ],
        )
        mock_pipe = AsyncMock()
        mock_pipe.delete = AsyncMock()
        mock_pipe.execute = AsyncMock()

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())
        size = await ch.queue_purge("myqueue")
        assert size == 2
        # Should have called delete for queue, index, and each tag's message hash
        assert mock_pipe.delete.call_count >= 4  # queue, index, tag1, tag2, tag3

    async def test_queue_delete_cleans_hashes(self):
        ch = _make_channel()
        ch.client.zcard = AsyncMock(return_value=1)
        ch.client.zrange = AsyncMock(
            side_effect=[
                [b"tag1"],  # queue tags
                [b"tag1"],  # index tags (same)
            ],
        )

        mock_pipe = AsyncMock()
        mock_pipe.delete = AsyncMock()
        mock_pipe.execute = AsyncMock()

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())
        size = await ch.queue_delete("myqueue")
        assert size == 1
        # queue_key, index_key, message:tag1 = 3 delete calls
        assert mock_pipe.delete.call_count == 3

    async def test_queue_delete_if_empty(self):
        ch = _make_channel()
        ch.client.zcard = AsyncMock(return_value=5)
        result = await ch.queue_delete("myqueue", if_empty=True)
        assert result == 0  # Not deleted because not empty


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


class TestPublish:
    async def test_direct_publish_default_exchange(self):
        ch = _make_channel()

        # Mock _put_message
        ch._put_message = AsyncMock()
        await ch.publish(b'{"body": "hi"}', exchange="", routing_key="myqueue")
        ch._put_message.assert_called_once_with("myqueue", b'{"body": "hi"}')

    async def test_fanout_publish(self):
        ch = _make_channel()
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch.client.xadd = AsyncMock()
        await ch.publish(b'{"body": "hi"}', exchange="fanout_ex", routing_key="")
        ch.client.xadd.assert_called_once()

    async def test_topic_publish(self):
        ch = _make_channel()
        ch._exchanges["topic_ex"] = {"type": "topic"}
        ch.client.smembers = AsyncMock(
            return_value={
                (BINDING_SEP.join(["user.*", "user.*", "q1"])).encode(),
            },
        )
        ch._put_message = AsyncMock()
        await ch.publish(b'{"body": "hi"}', exchange="topic_ex", routing_key="user.created")
        ch._put_message.assert_called_once()

    async def test_put_message_queue_at_includes_rci(self):
        """queue_at should include +RCI compensation."""
        ch = _make_channel(visibility_timeout=300)

        mock_pipe = AsyncMock()
        mock_pipe.hset = AsyncMock()
        mock_pipe.expire = AsyncMock()
        mock_pipe.zadd = AsyncMock()
        mock_pipe.pexpire = AsyncMock()
        mock_pipe.execute = AsyncMock()

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())

        with patch("kombu.transport.valkey_redis.time") as mock_time:
            mock_time.return_value = 1000.0
            await ch._put_message("q1", b'{"body": "test", "properties": {}, "headers": {}}')

        # Find the zadd call for messages_index
        for call in mock_pipe.zadd.call_args_list:
            args, kwargs = call
            key = args[0]
            if "messages_index" in key:
                mapping = args[1]
                for score in mapping.values():
                    # score should be now + VT + RCI = 1000 + 300 + 60 = 1360
                    assert score == 1360.0, f"Expected 1360.0, got {score}"
                break

    async def test_put_message_stores_restore_count(self):
        """New messages should have restore_count=0."""
        ch = _make_channel()

        mock_pipe = AsyncMock()
        mock_pipe.hset = AsyncMock()
        mock_pipe.zadd = AsyncMock()
        mock_pipe.execute = AsyncMock()

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())
        await ch._put_message("q1", b'{"body": "test", "properties": {}, "headers": {}}')

        # Check hset mapping includes restore_count=0
        hset_call = mock_pipe.hset.call_args
        mapping = hset_call[1]["mapping"]
        assert mapping["restore_count"] == 0


# ---------------------------------------------------------------------------
# FAST/SLOW consume
# ---------------------------------------------------------------------------


class TestFastSlowConsume:
    async def test_fast_consume_success(self):
        ch = _make_channel()
        ch._consume_fast_mode = True

        # Register a consumer
        cb = MagicMock()
        ch._consumers["tag1"] = ("q1", cb, True)

        # Mock consume script
        consume_script = AsyncMock(
            return_value=[
                b"q1",
                b"delivery-tag-1",
                b'{"body": "hello", "properties": {}, "headers": {}}',
                b"0",
            ],
        )
        ch._consume_script = consume_script

        result = await ch._fast_consume(["q1"])
        assert result is True
        cb.assert_called_once()

    async def test_fast_consume_empty_switches_to_slow(self):
        ch = _make_channel()
        ch._consume_fast_mode = True

        # Mock consume script returns nil (empty)
        consume_script = AsyncMock(return_value=None)
        ch._consume_script = consume_script

        result = await ch._fast_consume(["q1"])
        assert result is False
        # _consume_regular should have switched to slow mode

    async def test_consume_regular_fast_then_slow(self):
        ch = _make_channel()
        ch._consume_fast_mode = True

        # FAST returns nil
        consume_script = AsyncMock(return_value=None)
        ch._consume_script = consume_script

        # SLOW also returns nothing
        ch.client.bzmpop = AsyncMock(return_value=None)

        result = await ch._consume_regular(["q1"], timeout=1.0)
        assert result is False
        assert ch._consume_fast_mode is False  # Should have switched to SLOW

    async def test_slow_consume_switches_back_to_fast(self):
        ch = _make_channel()
        ch._consume_fast_mode = False

        # Register consumer
        cb = MagicMock()
        ch._consumers["tag1"] = ("q1", cb, True)

        # SLOW returns a message
        ch.client.bzmpop = AsyncMock(
            return_value=(
                b"queue:q1",
                [(b"delivery-tag-1", 1000.0)],
            ),
        )

        mock_pipe = AsyncMock()
        mock_pipe.zadd = AsyncMock()
        mock_pipe.hmget = AsyncMock()
        mock_pipe.execute = AsyncMock(
            return_value=[
                None,  # zadd result
                [b'{"body": "hello", "properties": {}, "headers": {}}', b"0"],  # hmget result
            ],
        )

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())

        result = await ch._slow_consume(["q1"], timeout=1.0)
        assert result is True
        # After successful SLOW, _consume_regular should switch back to FAST
        # (this happens in _consume_regular, not _slow_consume itself)

    async def test_fast_consume_with_restore_count(self):
        """FAST consume should inject x-restore-count header."""
        ch = _make_channel()
        ch._consume_fast_mode = True

        cb = MagicMock()
        ch._consumers["tag1"] = ("q1", cb, True)

        consume_script = AsyncMock(
            return_value=[
                b"q1",
                b"tag-1",
                b'{"body": "hello", "properties": {}, "headers": {}}',
                b"3",  # restore_count = 3
            ],
        )
        ch._consume_script = consume_script

        result = await ch._fast_consume(["q1"])
        assert result is True
        # Check the message passed to callback has x-restore-count
        call_args = cb.call_args
        msg = call_args[0][1]
        assert msg.headers.get("x-restore-count") == 3


# ---------------------------------------------------------------------------
# Ack / Reject / Recover
# ---------------------------------------------------------------------------


class TestAckReject:
    async def test_basic_ack_uses_lua_script(self):
        ch = _make_channel()
        ack_script = AsyncMock()
        ch._ack_script = ack_script

        # Track a delivered message
        msg = MagicMock()
        msg.delivery_tag = "tag1"
        ch._delivered["tag1"] = ("q1", msg)

        await ch.basic_ack("tag1")
        ack_script.assert_called_once()
        keys = ack_script.call_args[1]["keys"]
        assert len(keys) == 2  # index key, message key
        assert "tag1" not in ch._delivered

    async def test_basic_ack_fanout_skips_redis(self):
        ch = _make_channel()
        ack_script = AsyncMock()
        ch._ack_script = ack_script

        ch._fanout_tags.add("ftag1")
        ch._delivered["ftag1"] = ("q1", MagicMock())

        await ch.basic_ack("ftag1")
        ack_script.assert_not_called()
        assert "ftag1" not in ch._fanout_tags

    async def test_basic_reject_requeue(self):
        ch = _make_channel()
        ch._requeue_by_tag = AsyncMock(return_value=True)
        msg = MagicMock()
        ch._delivered["tag1"] = ("q1", msg)

        await ch.basic_reject("tag1", requeue=True)
        ch._requeue_by_tag.assert_called_once_with("tag1", leftmost=True)

    async def test_basic_reject_no_requeue_uses_lua(self):
        ch = _make_channel()
        ack_script = AsyncMock()
        ch._ack_script = ack_script
        msg = MagicMock()
        ch._delivered["tag1"] = ("q1", msg)

        await ch.basic_reject("tag1", requeue=False)
        ack_script.assert_called_once()

    async def test_basic_recover_requeues_all(self):
        ch = _make_channel()
        ch._requeue_by_tag = AsyncMock(return_value=True)
        ch._delivered["tag1"] = ("q1", MagicMock())
        ch._delivered["tag2"] = ("q2", MagicMock())

        await ch.basic_recover(requeue=True)
        assert ch._requeue_by_tag.call_count == 2
        assert len(ch._delivered) == 0


# ---------------------------------------------------------------------------
# Requeue (updated script args)
# ---------------------------------------------------------------------------


class TestRequeue:
    async def test_requeue_passes_new_args(self):
        ch = _make_channel(visibility_timeout=120)
        requeue_script = AsyncMock(return_value=1)
        ch._requeue_script = requeue_script

        result = await ch._requeue_by_tag("tag1", leftmost=True)
        assert result is True

        call_args = requeue_script.call_args
        args = call_args[1]["args"]
        assert args[0] == 1  # leftmost
        assert args[5] == MESSAGE_KEY_PREFIX  # message_key_prefix
        assert args[6] == 120  # visibility_timeout
        assert args[7] == MESSAGES_INDEX_PREFIX  # messages_index_prefix


# ---------------------------------------------------------------------------
# Enqueue due messages
# ---------------------------------------------------------------------------


class TestEnqueueDueMessages:
    async def test_enqueue_returns_tuple(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), False)

        enqueue_script = AsyncMock(return_value=[5, 2])  # {enqueued, dropped}
        ch._enqueue_script = enqueue_script

        enqueued, dropped = await ch._enqueue_due_messages()
        assert enqueued == 5
        assert dropped == 2

    async def test_enqueue_passes_max_restore_count(self):
        ch = _make_channel(max_restore_count=10)
        ch._consumers["tag1"] = ("q1", MagicMock(), False)

        enqueue_script = AsyncMock(return_value=[1, 0])
        ch._enqueue_script = enqueue_script

        await ch._enqueue_due_messages()
        call_args = enqueue_script.call_args
        args = call_args[1]["args"]
        assert args[7] == 10  # max_restore_count

    async def test_enqueue_no_limit(self):
        ch = _make_channel()  # max_restore_count=None (default)
        ch._consumers["tag1"] = ("q1", MagicMock(), False)

        enqueue_script = AsyncMock(return_value=[1, 0])
        ch._enqueue_script = enqueue_script

        await ch._enqueue_due_messages()
        call_args = enqueue_script.call_args
        args = call_args[1]["args"]
        assert args[7] == -1  # -1 = no limit

    async def test_enqueue_no_active_queues(self):
        ch = _make_channel()
        # No consumers
        enqueued, dropped = await ch._enqueue_due_messages()
        assert enqueued == 0
        assert dropped == 0


# ---------------------------------------------------------------------------
# Update messages index (heartbeat)
# ---------------------------------------------------------------------------


class TestUpdateMessagesIndex:
    async def test_heartbeat_includes_rci(self):
        ch = _make_channel(visibility_timeout=300)
        ch._delivered["tag1"] = ("q1", MagicMock())

        mock_pipe = AsyncMock()
        mock_pipe.zadd = AsyncMock()
        mock_pipe.execute = AsyncMock()

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())

        with patch("kombu.transport.valkey_redis.time") as mock_time:
            mock_time.return_value = 1000.0
            await ch._update_messages_index()

        # Should be now + VT + RCI = 1000 + 300 + 60 = 1360
        zadd_call = mock_pipe.zadd.call_args
        mapping = zadd_call[0][1]
        assert mapping["tag1"] == 1360.0


# ---------------------------------------------------------------------------
# get() uses consume script
# ---------------------------------------------------------------------------


class TestGet:
    async def test_get_uses_consume_script(self):
        ch = _make_channel()

        consume_script = AsyncMock(
            return_value=[
                b"q1",
                b"tag-1",
                b'{"body": "hello", "properties": {}, "headers": {}}',
                b"0",
            ],
        )
        ch._consume_script = consume_script

        msg = await ch.get("q1", no_ack=False)
        assert msg is not None
        assert msg.delivery_tag == "tag-1"
        assert "tag-1" in ch._delivered

    async def test_get_no_ack(self):
        ch = _make_channel()

        consume_script = AsyncMock(
            return_value=[
                b"q1",
                b"tag-1",
                b'{"body": "hello", "properties": {}, "headers": {}}',
                b"0",
            ],
        )
        ch._consume_script = consume_script

        msg = await ch.get("q1", no_ack=True)
        assert msg is not None
        assert "tag-1" not in ch._delivered

    async def test_get_empty(self):
        ch = _make_channel()

        consume_script = AsyncMock(return_value=None)
        ch._consume_script = consume_script

        msg = await ch.get("q1")
        assert msg is None

    async def test_get_with_restore_count(self):
        ch = _make_channel()

        consume_script = AsyncMock(
            return_value=[
                b"q1",
                b"tag-1",
                b'{"body": "hello", "properties": {}, "headers": {}}',
                b"7",  # restore_count=7
            ],
        )
        ch._consume_script = consume_script

        msg = await ch.get("q1", no_ack=True)
        assert msg.headers["x-restore-count"] == 7


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


class TestClose:
    async def test_close_requeues_delivered(self):
        ch = _make_channel()
        ch._requeue_by_tag = AsyncMock(return_value=True)
        ch._delivered["tag1"] = ("q1", MagicMock())
        ch._delivered["tag2"] = ("q2", MagicMock())
        ch._start_periodic_tasks = MagicMock()  # prevent actual tasks

        await ch.close()
        assert ch._requeue_by_tag.call_count == 2
        assert len(ch._delivered) == 0
        assert ch._closed is True

    async def test_close_skips_fanout_tags(self):
        ch = _make_channel()
        ch._requeue_by_tag = AsyncMock(return_value=True)
        ch._fanout_tags.add("ftag1")
        ch._delivered["ftag1"] = ("q1", MagicMock())
        ch._delivered["tag1"] = ("q2", MagicMock())

        await ch.close()
        # Only tag1 should be requeued, not ftag1
        ch._requeue_by_tag.assert_called_once()

    async def test_close_deletes_auto_delete_queues(self):
        ch = _make_channel()
        ch._requeue_by_tag = AsyncMock()
        ch.queue_delete = AsyncMock()
        ch.auto_delete_queues.add("auto_q")

        await ch.close()
        ch.queue_delete.assert_called_once_with("auto_q")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class TestTransport:
    def test_init(self):
        t = Transport(url="redis://localhost:6379/2")
        assert t._db == "2"
        assert not t._connected

    def test_default_port(self):
        assert Transport.default_port == 6379

    def test_driver_type(self):
        assert Transport.driver_type == "redis"

    def test_connection_errors(self):
        assert ConnectionRefusedError in Transport.connection_errors
        assert TimeoutError in Transport.connection_errors

    def test_client_kwargs_filters_transport_options(self):
        t = Transport(
            url="redis://localhost:6379",
            global_keyprefix="p:",
            visibility_timeout=120,
            max_restore_count=5,
            credential_provider="some.module.Provider",
            socket_timeout=10,
        )
        kw = t._client_kwargs()
        assert "global_keyprefix" not in kw
        assert "visibility_timeout" not in kw
        assert "max_restore_count" not in kw
        assert "credential_provider" not in kw
        assert kw["socket_timeout"] == 10

    async def test_connect(self):
        t = Transport(url="redis://localhost:6379")
        mock_aiolib = MagicMock()
        mock_client = AsyncMock()
        mock_subclient = AsyncMock()
        mock_aiolib.from_url.side_effect = [mock_client, mock_subclient]
        mock_client.ping = AsyncMock()
        mock_subclient.ping = AsyncMock()
        t._aiolib = mock_aiolib

        await t.connect()
        assert t._connected is True
        assert mock_aiolib.from_url.call_count == 2

    async def test_close(self):
        t = _make_transport()
        ch = Channel(t, "conn")
        ch._closed = True  # Skip close logic
        t._channels = [ch]
        t._client.aclose = AsyncMock()
        t._subclient.aclose = AsyncMock()

        await t.close()
        assert not t._connected
        assert t._client is None
        assert t._subclient is None

    async def test_create_channel(self):
        t = _make_transport()
        ch = await t.create_channel()
        assert isinstance(ch, Channel)
        assert ch in t._channels

    def test_is_connected(self):
        t = _make_transport()
        assert t.is_connected is True
        t._connected = False
        assert t.is_connected is False

    def test_driver_version(self):
        t = _make_transport()
        version = t.driver_version()
        # Should return something (either version string or "N/A")
        assert isinstance(version, str)


# ---------------------------------------------------------------------------
# Credential provider
# ---------------------------------------------------------------------------


class TestCredentialProvider:
    def test_no_credential_provider(self):
        t = Transport(url="redis://localhost:6379")
        kw = t._process_credential_provider()
        assert kw == {}

    def test_credential_provider_instance(self):
        """Test with a mock credential provider instance."""
        t = Transport(url="redis://localhost:6379")

        # Create a mock that satisfies the CredentialProvider check
        mock_provider = MagicMock()

        # Mock the _lib.credentials.CredentialProvider to accept our mock
        mock_credentials = MagicMock()
        mock_credentials.CredentialProvider = type(mock_provider)

        with patch.dict(t._options, {"credential_provider": mock_provider}):
            t._lib = MagicMock()
            t._lib.credentials = mock_credentials
            kw = t._process_credential_provider()
            assert kw["credential_provider"] is mock_provider

    def test_credential_provider_none(self):
        t = Transport(url="redis://localhost:6379")
        t._options["credential_provider"] = None
        kw = t._process_credential_provider()
        assert kw == {}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_min_queue_expires(self):
        assert MIN_QUEUE_EXPIRES == 10_000

    def test_default_max_restore_count(self):
        assert DEFAULT_MAX_RESTORE_COUNT is None


# ---------------------------------------------------------------------------
# Drain events
# ---------------------------------------------------------------------------


class TestDrainEvents:
    async def test_drain_no_consumers(self):
        ch = _make_channel()
        result = await ch.drain_events(timeout=0.01)
        assert result is False

    async def test_drain_events_regular_queue(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), True)

        # Mock _consume_regular to return True
        ch._consume_regular = AsyncMock(return_value=True)

        result = await ch.drain_events(timeout=0.5)
        assert result is True


# ---------------------------------------------------------------------------
# Parse consume result
# ---------------------------------------------------------------------------


class TestParseConsumeResult:
    def test_bytes_result(self):
        ch = _make_channel()
        result = [b"q1", b"tag1", b'{"body": "hi"}', b"5"]
        q, tag, payload, rc = ch._parse_consume_result(result)
        assert q == "q1"
        assert tag == "tag1"
        assert payload == '{"body": "hi"}'
        assert rc == 5

    async def test_string_result(self):
        ch = _make_channel()
        result = ["q1", "tag1", '{"body": "hi"}', "0"]
        q, tag, payload, rc = ch._parse_consume_result(result)
        assert q == "q1"
        assert tag == "tag1"
        assert rc == 0

    async def test_none_restore_count(self):
        ch = _make_channel()
        result = [b"q1", b"tag1", b'{"body": "hi"}', None]
        _, _, _, rc = ch._parse_consume_result(result)
        assert rc == 0


# ---------------------------------------------------------------------------
# Lua script loading
# ---------------------------------------------------------------------------


class TestLuaScripts:
    async def test_consume_script_loaded(self):
        ch = _make_channel()
        mock_script = MagicMock()
        ch.client.register_script = MagicMock(return_value=mock_script)

        script = await ch._get_consume_script()
        assert script is mock_script
        ch.client.register_script.assert_called_once()

    async def test_ack_script_loaded(self):
        ch = _make_channel()
        mock_script = MagicMock()
        ch.client.register_script = MagicMock(return_value=mock_script)

        script = await ch._get_ack_script()
        assert script is mock_script
        ch.client.register_script.assert_called_once()

    async def test_scripts_cached(self):
        ch = _make_channel()
        mock_script = MagicMock()
        ch.client.register_script = MagicMock(return_value=mock_script)

        s1 = await ch._get_consume_script()
        s2 = await ch._get_consume_script()
        assert s1 is s2
        assert ch.client.register_script.call_count == 1


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------


class TestLoadBindings:
    async def test_load_sep_format(self):
        ch = _make_channel()
        binding = BINDING_SEP.join(["rk1", "rk1", "q1"])
        ch.client.smembers = AsyncMock(return_value={binding.encode()})

        bindings = await ch._load_bindings("ex1")
        assert bindings == [("q1", "rk1")]

    async def test_load_json_format(self):
        ch = _make_channel()
        ch.client.smembers = AsyncMock(
            return_value={
                b'{"queue": "q1", "routing_key": "rk1"}',
            },
        )

        bindings = await ch._load_bindings("ex1")
        assert bindings == [("q1", "rk1")]


# ---------------------------------------------------------------------------
# basic_consume / basic_cancel
# ---------------------------------------------------------------------------


class TestBasicConsume:
    async def test_basic_consume_returns_tag(self):
        ch = _make_channel()
        ch._start_periodic_tasks = MagicMock()
        tag = await ch.basic_consume("q1", MagicMock(), no_ack=False)
        assert tag in ch._consumers
        assert ch._consumers[tag][0] == "q1"

    async def test_basic_consume_custom_tag(self):
        ch = _make_channel()
        ch._start_periodic_tasks = MagicMock()
        tag = await ch.basic_consume("q1", MagicMock(), consumer_tag="my-tag")
        assert tag == "my-tag"
        assert "my-tag" in ch._consumers

    async def test_basic_consume_no_ack_tracked(self):
        ch = _make_channel()
        ch._start_periodic_tasks = MagicMock()
        tag = await ch.basic_consume("q1", MagicMock(), no_ack=True)
        assert tag in ch.no_ack_consumers

    async def test_basic_consume_fanout_queue(self):
        ch = _make_channel()
        ch._start_periodic_tasks = MagicMock()
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch._fanout_queues["fq1"] = ("fanout_ex", "*")

        await ch.basic_consume("fq1", MagicMock())
        assert "fq1" in ch.active_fanout_queues
        assert ch._fanout_to_queue["fanout_ex"] == "fq1"

    async def test_basic_consume_starts_periodic_tasks(self):
        ch = _make_channel()
        ch._start_periodic_tasks = MagicMock()
        await ch.basic_consume("q1", MagicMock())
        ch._start_periodic_tasks.assert_called_once()

    async def test_basic_cancel_removes_consumer(self):
        ch = _make_channel()
        ch._start_periodic_tasks = MagicMock()
        tag = await ch.basic_consume("q1", MagicMock())
        await ch.basic_cancel(tag)
        assert tag not in ch._consumers

    async def test_basic_cancel_removes_no_ack(self):
        ch = _make_channel()
        ch._start_periodic_tasks = MagicMock()
        tag = await ch.basic_consume("q1", MagicMock(), no_ack=True)
        assert tag in ch.no_ack_consumers
        await ch.basic_cancel(tag)
        assert tag not in ch.no_ack_consumers

    async def test_basic_cancel_cleans_fanout(self):
        ch = _make_channel()
        ch._start_periodic_tasks = MagicMock()
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch._fanout_queues["fq1"] = ("fanout_ex", "*")

        tag = await ch.basic_consume("fq1", MagicMock())
        assert "fq1" in ch.active_fanout_queues
        assert "fanout_ex" in ch._fanout_to_queue

        await ch.basic_cancel(tag)
        assert "fq1" not in ch.active_fanout_queues
        assert "fanout_ex" not in ch._fanout_to_queue

    async def test_basic_cancel_nonexistent_tag(self):
        ch = _make_channel()
        # Should not raise
        await ch.basic_cancel("nonexistent-tag")


# ---------------------------------------------------------------------------
# _xread_wait (fanout consumption)
# ---------------------------------------------------------------------------


class TestXreadWait:
    async def test_xread_wait_no_streams(self):
        ch = _make_channel()
        # No active fanout queues → no streams → False
        result = await ch._xread_wait(1.0)
        assert result is False

    async def test_xread_wait_empty_result(self):
        ch = _make_channel()
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch._fanout_queues["fq1"] = ("fanout_ex", "*")
        ch.active_fanout_queues.add("fq1")

        ch._transport._subclient.xread = AsyncMock(return_value=None)
        result = await ch._xread_wait(1.0)
        assert result is False

    async def test_xread_wait_delivers_message(self):
        ch = _make_channel()
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch._fanout_queues["fq1"] = ("fanout_ex", "*")
        ch.active_fanout_queues.add("fq1")

        cb = MagicMock()
        ch._consumers["tag1"] = ("fq1", cb, True)

        stream_key = ch._fanout_stream_key("fanout_ex")
        payload = '{"body": "fanout_msg", "properties": {}, "headers": {}}'
        ch._transport._subclient.xread = AsyncMock(
            return_value=[
                (
                    stream_key.encode(),
                    [(b"1234-0", {b"uuid": b"abc", b"payload": payload.encode()})],
                ),
            ],
        )

        result = await ch._xread_wait(1.0)
        assert result is True
        cb.assert_called_once()

        # Check message was created correctly
        msg = cb.call_args[0][1]
        assert msg.delivery_tag in ch._fanout_tags

    async def test_xread_wait_updates_stream_offset(self):
        ch = _make_channel()
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch._fanout_queues["fq1"] = ("fanout_ex", "*")
        ch.active_fanout_queues.add("fq1")
        ch._consumers["tag1"] = ("fq1", MagicMock(), True)

        stream_key = ch._fanout_stream_key("fanout_ex")
        payload = '{"body": "test", "properties": {}, "headers": {}}'
        ch._transport._subclient.xread = AsyncMock(
            return_value=[
                (
                    stream_key.encode(),
                    [(b"5678-0", {b"payload": payload.encode()})],
                ),
            ],
        )

        await ch._xread_wait(1.0)
        assert ch._stream_offsets[stream_key] == "5678-0"

    async def test_xread_wait_uses_last_offset(self):
        ch = _make_channel()
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch._fanout_queues["fq1"] = ("fanout_ex", "*")
        ch.active_fanout_queues.add("fq1")

        stream_key = ch._fanout_stream_key("fanout_ex")
        ch._stream_offsets[stream_key] = "9999-0"

        ch._transport._subclient.xread = AsyncMock(return_value=None)
        await ch._xread_wait(1.0)

        # Should pass the stored offset, not "$"
        call_args = ch._transport._subclient.xread.call_args
        streams_arg = call_args[0][0]
        assert streams_arg[stream_key] == "9999-0"

    async def test_xread_wait_xread_exception(self):
        ch = _make_channel()
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch._fanout_queues["fq1"] = ("fanout_ex", "*")
        ch.active_fanout_queues.add("fq1")

        ch._transport._subclient.xread = AsyncMock(side_effect=ConnectionError("lost"))
        result = await ch._xread_wait(1.0)
        assert result is False

    async def test_xread_wait_missing_payload_skips(self):
        ch = _make_channel()
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch._fanout_queues["fq1"] = ("fanout_ex", "*")
        ch.active_fanout_queues.add("fq1")
        ch._consumers["tag1"] = ("fq1", MagicMock(), True)

        stream_key = ch._fanout_stream_key("fanout_ex")
        # Message with no "payload" field
        ch._transport._subclient.xread = AsyncMock(
            return_value=[
                (
                    stream_key.encode(),
                    [(b"1111-0", {b"uuid": b"abc"})],
                ),
            ],
        )

        result = await ch._xread_wait(1.0)
        assert result is False

    async def test_xread_wait_unmatched_stream_skips(self):
        ch = _make_channel()
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch._fanout_queues["fq1"] = ("fanout_ex", "*")
        ch.active_fanout_queues.add("fq1")

        # Return a stream key that doesn't match any registered fanout queue
        ch._transport._subclient.xread = AsyncMock(
            return_value=[
                (
                    b"unknown_stream_key",
                    [(b"1111-0", {b"payload": b'{"body":"x","properties":{},"headers":{}}'})],
                ),
            ],
        )

        result = await ch._xread_wait(1.0)
        assert result is False

    async def test_xread_wait_with_global_prefix(self):
        ch = _make_channel(global_keyprefix="myapp:")
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch._fanout_queues["fq1"] = ("fanout_ex", "*")
        ch.active_fanout_queues.add("fq1")
        ch._consumers["tag1"] = ("fq1", MagicMock(), True)

        stream_key = ch._fanout_stream_key("fanout_ex")
        payload = '{"body": "prefixed", "properties": {}, "headers": {}}'
        ch._transport._subclient.xread = AsyncMock(
            return_value=[
                (
                    stream_key.encode(),
                    [(b"2222-0", {b"payload": payload.encode()})],
                ),
            ],
        )

        result = await ch._xread_wait(1.0)
        assert result is True


# ---------------------------------------------------------------------------
# _drain_expired_and_deliver
# ---------------------------------------------------------------------------


class TestDrainExpiredAndDeliver:
    async def test_empty_queue(self):
        ch = _make_channel()
        ch.client.zpopmin = AsyncMock(return_value=[])
        result = await ch._drain_expired_and_deliver("q1")
        assert result is False

    async def test_valid_message_found(self):
        ch = _make_channel()
        cb = MagicMock()
        ch._consumers["tag1"] = ("q1", cb, True)

        ch.client.zpopmin = AsyncMock(
            return_value=[(b"tag-valid", 1000.0)],
        )
        ch.client.hmget = AsyncMock(
            return_value=[
                b'{"body": "found", "properties": {}, "headers": {}}',
                b"0",
            ],
        )

        result = await ch._drain_expired_and_deliver("q1")
        assert result is True
        cb.assert_called_once()

    async def test_expired_then_valid(self):
        ch = _make_channel()
        cb = MagicMock()
        ch._consumers["tag1"] = ("q1", cb, True)

        # First zpopmin: expired (payload=None), second: valid
        ch.client.zpopmin = AsyncMock(
            side_effect=[
                [(b"expired-tag", 500.0)],
                [(b"valid-tag", 1000.0)],
            ],
        )
        ch.client.hmget = AsyncMock(
            side_effect=[
                [None, None],  # expired
                [b'{"body": "ok", "properties": {}, "headers": {}}', b"2"],  # valid with restore_count=2
            ],
        )
        ch.client.zrem = AsyncMock()

        result = await ch._drain_expired_and_deliver("q1")
        assert result is True
        # Should have cleaned up the expired tag from index
        ch.client.zrem.assert_called_once()
        # Callback should have the message with x-restore-count
        msg = cb.call_args[0][1]
        assert msg.headers.get("x-restore-count") == 2

    async def test_all_expired(self):
        ch = _make_channel()

        # First zpopmin: expired, second: empty
        ch.client.zpopmin = AsyncMock(
            side_effect=[
                [(b"expired-1", 500.0)],
                [],  # queue now empty
            ],
        )
        ch.client.hmget = AsyncMock(return_value=[None, None])
        ch.client.zrem = AsyncMock()

        result = await ch._drain_expired_and_deliver("q1")
        assert result is False


# ---------------------------------------------------------------------------
# drain_events (full path with fanout racing)
# ---------------------------------------------------------------------------


class TestDrainEventsFull:
    async def test_drain_no_consumers(self):
        ch = _make_channel()
        result = await ch.drain_events(timeout=0.01)
        assert result is False

    async def test_drain_regular_only(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), True)
        ch._consume_regular = AsyncMock(return_value=True)

        result = await ch.drain_events(timeout=0.5)
        assert result is True

    async def test_drain_fanout_only(self):
        ch = _make_channel()
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch._fanout_queues["fq1"] = ("fanout_ex", "*")
        ch.active_fanout_queues.add("fq1")
        ch._consumers["tag1"] = ("fq1", MagicMock(), True)
        ch._xread_wait = AsyncMock(return_value=True)

        result = await ch.drain_events(timeout=0.5)
        assert result is True

    async def test_drain_regular_and_fanout(self):
        ch = _make_channel()
        # Regular consumer
        ch._consumers["tag1"] = ("q1", MagicMock(), True)
        # Fanout consumer
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch._fanout_queues["fq1"] = ("fanout_ex", "*")
        ch.active_fanout_queues.add("fq1")
        ch._consumers["tag2"] = ("fq1", MagicMock(), True)

        ch._consume_regular = AsyncMock(return_value=True)

        async def slow_xread(timeout):
            await asyncio.sleep(10)
            return False

        ch._xread_wait = slow_xread

        result = await ch.drain_events(timeout=1.0)
        assert result is True

    async def test_drain_all_return_false(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), True)
        ch._consume_regular = AsyncMock(return_value=False)

        result = await ch.drain_events(timeout=0.1)
        assert result is False

    async def test_drain_handles_task_exception(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), True)
        ch._consume_regular = AsyncMock(side_effect=RuntimeError("boom"))

        # Should not propagate, just return False
        result = await ch.drain_events(timeout=0.1)
        assert result is False


# ---------------------------------------------------------------------------
# Persistent consumer task lifecycle
# ---------------------------------------------------------------------------


class TestPersistentConsumerTasks:
    async def test_drain_events_never_cancels_iteration(self):
        # Core invariant: drain_events must not cancel the consumer iteration.
        # Cancellation mid-FAST-script or post-BZMPOP strands a message in
        # messages_index for ~6 min.
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), True)

        cancelled = False

        async def watch_for_cancel(*args, **kwargs):
            nonlocal cancelled
            try:
                await asyncio.sleep(0.05)
                return True
            except asyncio.CancelledError:
                cancelled = True
                raise

        ch._consume_regular = watch_for_cancel

        result = await ch.drain_events(timeout=1.0)
        assert result is True
        assert cancelled is False

    async def test_safety_net_leaves_iteration_pending(self, monkeypatch):
        # When the asyncio.wait safety net fires (Redis hung), the iteration
        # must be left pending — not cancelled — so any in-flight broker
        # state stays in sync if/when Redis recovers.
        from kombu.transport import valkey_redis

        monkeypatch.setattr(valkey_redis, "CONSUMER_WAIT_HEADROOM", 0.0)

        ch = _make_channel(block_timeout=0.05)
        ch._consumers["tag1"] = ("q1", MagicMock(), True)

        cancelled = False
        will_finish = asyncio.Event()

        async def slow_iteration(*args, **kwargs):
            nonlocal cancelled
            try:
                await asyncio.sleep(0.2)  # longer than safety net
                will_finish.set()
                return False
            except asyncio.CancelledError:
                cancelled = True
                raise

        ch._consume_regular = slow_iteration

        result = await ch.drain_events(timeout=1.0)
        assert result is False
        assert ch._consume_iter_task is not None
        assert not ch._consume_iter_task.done()
        assert cancelled is False

        # Let the iteration finish naturally so we don't leak a task.
        await will_finish.wait()

    async def test_pending_iteration_reused_by_next_drain(self, monkeypatch):
        # A pending iteration left behind by a safety-net fire is reused by
        # the next drain_events call — _consume_regular runs only once.
        from kombu.transport import valkey_redis

        monkeypatch.setattr(valkey_redis, "CONSUMER_WAIT_HEADROOM", 0.0)

        ch = _make_channel(block_timeout=0.05)
        ch._consumers["tag1"] = ("q1", MagicMock(), True)

        invocations = 0

        async def slow_then_deliver(*args, **kwargs):
            nonlocal invocations
            invocations += 1
            await asyncio.sleep(0.1)
            return True

        ch._consume_regular = slow_then_deliver

        result = await ch.drain_events(timeout=1.0)
        assert result is False
        assert invocations == 1
        first_task = ch._consume_iter_task
        assert first_task is not None

        # Restore a generous headroom so the next call harvests the same
        # iteration when it completes.
        monkeypatch.setattr(valkey_redis, "CONSUMER_WAIT_HEADROOM", 5.0)
        result = await ch.drain_events(timeout=1.0)
        assert result is True
        assert invocations == 1
        assert first_task.done()

    async def test_close_awaits_inflight_iteration(self):
        # close() must let the in-flight iteration finish so a message that
        # was already popped server-side is delivered, not stranded.
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), True)
        ch._delivered = {}

        delivered = asyncio.Event()

        async def deliver_then_finish(*args, **kwargs):
            await asyncio.sleep(0.05)
            delivered.set()
            return True

        ch._consume_regular = deliver_then_finish
        ch._consume_iter_task = asyncio.create_task(
            ch._safe_consume_iter(["q1"]),
        )

        await ch.close()
        assert delivered.is_set()
        assert ch._consume_iter_task is None
        assert ch._closing is True

    async def test_warns_when_only_one_iteration_stalls(self, monkeypatch, caplog):
        # If asyncio.wait's FIRST_COMPLETED fires on a healthy iteration while
        # the sibling is hung, the hung-task warning must still fire.
        from kombu.transport import valkey_redis

        monkeypatch.setattr(valkey_redis, "CONSUMER_WAIT_HEADROOM", 0.0)

        ch = _make_channel(block_timeout=0.05)
        ch._consumers["tag1"] = ("q1", MagicMock(), True)
        ch._exchanges["fan"] = {"type": "fanout"}
        ch._fanout_queues["fq1"] = ("fan", "*")
        ch.active_fanout_queues.add("fq1")
        ch._consumers["tag2"] = ("fq1", MagicMock(), True)

        # Regular delivers fast on every call; xread is hung.
        async def fast_deliver(*args, **kwargs):
            await asyncio.sleep(0.005)
            return True

        async def hung(*args, **kwargs):
            await asyncio.sleep(10)
            return False

        ch._consume_regular = fast_deliver
        ch._xread_wait = hung

        # First drain bootstraps both tasks. Regular delivers immediately,
        # xread is left pending with a fresh started_at.
        await ch.drain_events(timeout=1.0)
        assert ch._xread_iter_task is not None
        assert not ch._xread_iter_task.done()

        # Wait so xread's age exceeds (block_timeout + headroom).
        await asyncio.sleep(0.1)

        with caplog.at_level("WARNING", logger="kombu.transport.valkey_redis"):
            await ch.drain_events(timeout=1.0)

        assert any("xread_wait" in rec.message and "stalled" in rec.message.lower() for rec in caplog.records)
        # Warn-once: a third call must not log a second xread warning.
        caplog.clear()
        with caplog.at_level("WARNING", logger="kombu.transport.valkey_redis"):
            await ch.drain_events(timeout=1.0)
        assert not any("xread_wait" in rec.message for rec in caplog.records)

    async def test_close_cancels_hung_iteration_with_warning(self, monkeypatch, caplog):
        # If the iteration hangs past block_timeout + CLOSE_DRAIN_HEADROOM,
        # close cancels it as a last resort and logs a warning. Stranding at
        # shutdown is acceptable since visibility-timeout restore recovers
        # those on next worker boot.
        from kombu.transport import valkey_redis

        monkeypatch.setattr(valkey_redis, "CLOSE_DRAIN_HEADROOM", 0.0)

        ch = _make_channel(block_timeout=0.05)
        ch._consumers["tag1"] = ("q1", MagicMock(), True)
        ch._delivered = {}

        async def hung_iteration(*args, **kwargs):
            await asyncio.sleep(10)
            return False

        ch._consume_regular = hung_iteration
        ch._consume_iter_task = asyncio.create_task(
            ch._safe_consume_iter(["q1"]),
        )

        with caplog.at_level("WARNING", logger="kombu.transport.valkey_redis"):
            await ch.close()
        assert ch._consume_iter_task is None
        assert any("did not drain" in rec.message for rec in caplog.records)


class TestPeriodicTasks:
    async def test_start_periodic_tasks_creates_tasks(self):
        ch = _make_channel()
        # Patch the async methods to sleep forever then get cancelled
        ch._periodic_enqueue_due = AsyncMock(side_effect=asyncio.CancelledError)
        ch._periodic_heartbeat = AsyncMock(side_effect=asyncio.CancelledError)

        ch._start_periodic_tasks()
        assert ch._enqueue_task is not None
        assert ch._heartbeat_task is not None

        # Cleanup
        ch._enqueue_task.cancel()
        ch._heartbeat_task.cancel()
        try:
            await ch._enqueue_task
        except (asyncio.CancelledError, Exception):  # fmt: skip
            pass
        try:
            await ch._heartbeat_task
        except (asyncio.CancelledError, Exception):  # fmt: skip
            pass

    async def test_start_periodic_tasks_idempotent(self):
        ch = _make_channel()
        ch._periodic_enqueue_due = AsyncMock(side_effect=asyncio.CancelledError)
        ch._periodic_heartbeat = AsyncMock(side_effect=asyncio.CancelledError)

        ch._start_periodic_tasks()
        task1 = ch._enqueue_task
        ch._start_periodic_tasks()
        task2 = ch._enqueue_task
        # Should reuse the same task since it's not done
        assert task1 is task2

        # Cleanup
        for t in (ch._enqueue_task, ch._heartbeat_task):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # fmt: skip
                    pass

    async def test_periodic_enqueue_due_runs_and_cancels(self):
        ch = _make_channel()
        call_count = 0

        async def mock_enqueue():
            nonlocal call_count
            call_count += 1
            return (0, 0)

        ch._enqueue_due_messages = mock_enqueue

        with patch("kombu.transport.valkey_redis.DEFAULT_REQUEUE_CHECK_INTERVAL", 0.01):
            task = asyncio.ensure_future(ch._periodic_enqueue_due())
            await asyncio.sleep(0.05)
            ch._closed = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert call_count >= 1

    async def test_periodic_heartbeat_runs_and_cancels(self):
        ch = _make_channel(visibility_timeout=0.03)
        call_count = 0

        async def mock_heartbeat():
            nonlocal call_count
            call_count += 1

        ch._update_messages_index = mock_heartbeat

        task = asyncio.ensure_future(ch._periodic_heartbeat())
        await asyncio.sleep(0.05)
        ch._closed = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert call_count >= 1

    async def test_periodic_enqueue_due_handles_exception(self):
        ch = _make_channel()
        call_count = 0

        async def mock_enqueue():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")
            return (0, 0)

        ch._enqueue_due_messages = mock_enqueue

        with patch("kombu.transport.valkey_redis.DEFAULT_REQUEUE_CHECK_INTERVAL", 0.01):
            task = asyncio.ensure_future(ch._periodic_enqueue_due())
            await asyncio.sleep(0.05)
            ch._closed = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Should have retried after exception
        assert call_count >= 2

    async def test_periodic_refresh_expires(self):
        ch = _make_channel()
        ch._expires = {"q1": 30_000}
        ch._refresh_queue_expires = AsyncMock()

        task = asyncio.ensure_future(ch._periodic_refresh_expires())
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # interval = 30_000 / 2 / 1000 = 15s, so with 0.02s wait it won't fire
        # But check it doesn't crash

    async def test_periodic_refresh_expires_no_queues(self):
        ch = _make_channel()
        ch._expires = {}  # no queues with TTL
        # Should return immediately
        await ch._periodic_refresh_expires()

    async def test_update_expires_task_starts(self):
        ch = _make_channel()
        ch._expires = {"q1": 20_000}
        ch._refresh_queue_expires = AsyncMock()
        ch._periodic_refresh_expires = AsyncMock(side_effect=asyncio.CancelledError)

        ch._update_expires_task()
        assert ch._expires_task is not None

        # Cleanup
        ch._expires_task.cancel()
        try:
            await ch._expires_task
        except (asyncio.CancelledError, Exception):  # fmt: skip
            pass

    async def test_update_expires_task_restarts(self):
        ch = _make_channel()
        ch._expires = {"q1": 20_000}
        ch._periodic_refresh_expires = AsyncMock(side_effect=asyncio.CancelledError)

        ch._update_expires_task()
        first_task = ch._expires_task

        ch._update_expires_task()
        second_task = ch._expires_task
        assert first_task is not second_task

        # Cleanup
        for t in (first_task, second_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # fmt: skip
                    pass


# ---------------------------------------------------------------------------
# Close with periodic task cancellation
# ---------------------------------------------------------------------------


class TestClosePeriodicTasks:
    async def test_close_cancels_periodic_tasks(self):
        ch = _make_channel()
        ch._requeue_by_tag = AsyncMock()

        # Create mock tasks that simulate real asyncio tasks
        async def forever():
            await asyncio.sleep(999)

        ch._enqueue_task = asyncio.ensure_future(forever())
        ch._heartbeat_task = asyncio.ensure_future(forever())
        ch._expires_task = asyncio.ensure_future(forever())

        await ch.close()

        assert ch._enqueue_task.cancelled()
        assert ch._heartbeat_task.cancelled()
        assert ch._expires_task.cancelled()

    async def test_close_handles_task_already_done(self):
        ch = _make_channel()
        ch._requeue_by_tag = AsyncMock()

        # A task that's already finished
        async def instant():
            return

        ch._enqueue_task = asyncio.ensure_future(instant())
        await asyncio.sleep(0)  # let it complete
        assert ch._enqueue_task.done()

        # Should not raise
        await ch.close()

    async def test_close_requeue_error_logged(self):
        ch = _make_channel()
        ch._requeue_by_tag = AsyncMock(side_effect=ConnectionError("lost"))
        ch._delivered["tag1"] = ("q1", MagicMock())

        # Should not raise despite requeue failure
        await ch.close()
        assert len(ch._delivered) == 0

    async def test_close_auto_delete_error_ignored(self):
        ch = _make_channel()
        ch._requeue_by_tag = AsyncMock()
        ch.queue_delete = AsyncMock(side_effect=RuntimeError("delete failed"))
        ch.auto_delete_queues.add("auto_q")

        # Should not raise despite delete failure
        await ch.close()


# ---------------------------------------------------------------------------
# _slow_consume error paths
# ---------------------------------------------------------------------------


class TestSlowConsumeErrors:
    async def test_slow_consume_bzmpop_error(self):
        ch = _make_channel()
        ch.client.bzmpop = AsyncMock(side_effect=ConnectionError("lost"))
        result = await ch._slow_consume(["q1"], timeout=1.0)
        assert result is False

    async def test_slow_consume_empty_result(self):
        ch = _make_channel()
        ch.client.bzmpop = AsyncMock(return_value=None)
        result = await ch._slow_consume(["q1"], timeout=1.0)
        assert result is False

    async def test_slow_consume_expired_hash_drains(self):
        """When hash expired after BZMPOP, should fall back to drain."""
        ch = _make_channel()
        cb = MagicMock()
        ch._consumers["tag1"] = ("q1", cb, True)

        ch.client.bzmpop = AsyncMock(
            return_value=(
                b"queue:q1",
                [(b"tag-expired", 1000.0)],
            ),
        )

        mock_pipe = AsyncMock()
        mock_pipe.zadd = AsyncMock()
        mock_pipe.hmget = AsyncMock()
        mock_pipe.execute = AsyncMock(
            return_value=[
                None,  # zadd
                [None, None],  # hmget: payload is None (expired)
            ],
        )

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())
        ch.client.zrem = AsyncMock()
        ch._drain_expired_and_deliver = AsyncMock(return_value=False)

        result = await ch._slow_consume(["q1"], timeout=1.0)
        assert result is False
        # Should have tried to clean up the index
        ch.client.zrem.assert_called_once()
        # Should have called drain_expired_and_deliver
        ch._drain_expired_and_deliver.assert_called_once_with("q1")

    async def test_slow_consume_restore_count_injected(self):
        ch = _make_channel()
        cb = MagicMock()
        ch._consumers["tag1"] = ("q1", cb, True)

        ch.client.bzmpop = AsyncMock(
            return_value=(
                b"queue:q1",
                [(b"tag-restored", 1000.0)],
            ),
        )

        mock_pipe = AsyncMock()
        mock_pipe.zadd = AsyncMock()
        mock_pipe.hmget = AsyncMock()
        mock_pipe.execute = AsyncMock(
            return_value=[
                None,
                [b'{"body": "hi", "properties": {}, "headers": {}}', b"5"],
            ],
        )

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())

        result = await ch._slow_consume(["q1"], timeout=1.0)
        assert result is True
        msg = cb.call_args[0][1]
        assert msg.headers["x-restore-count"] == 5

    async def test_slow_consume_with_global_prefix(self):
        ch = _make_channel(global_keyprefix="app:")
        cb = MagicMock()
        ch._consumers["tag1"] = ("q1", cb, True)

        ch.client.bzmpop = AsyncMock(
            return_value=(
                b"app:queue:q1",  # prefixed key returned by Redis
                [(b"tag-1", 1000.0)],
            ),
        )

        mock_pipe = AsyncMock()
        mock_pipe.zadd = AsyncMock()
        mock_pipe.hmget = AsyncMock()
        mock_pipe.execute = AsyncMock(
            return_value=[
                None,
                [b'{"body": "ok", "properties": {}, "headers": {}}', b"0"],
            ],
        )

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())

        result = await ch._slow_consume(["q1"], timeout=1.0)
        assert result is True
        # Check the message was delivered to q1 (unprefixed)
        cb.assert_called_once()


# ---------------------------------------------------------------------------
# _put_message fanout XADD
# ---------------------------------------------------------------------------


class TestPutMessageFanout:
    async def test_fanout_publish_uses_xadd(self):
        ch = _make_channel()
        ch._exchanges["fanout_ex"] = {"type": "fanout"}
        ch.client.xadd = AsyncMock()

        await ch._fanout_publish("fanout_ex", b'{"body": "hello"}')

        ch.client.xadd.assert_called_once()
        call_kw = ch.client.xadd.call_args[1]
        assert "uuid" in call_kw["fields"]
        assert call_kw["fields"]["payload"] == '{"body": "hello"}'
        assert call_kw["maxlen"] == ch._stream_maxlen
        assert call_kw["approximate"] is True

    async def test_fanout_publish_custom_maxlen(self):
        ch = _make_channel(stream_maxlen=500)
        ch.client.xadd = AsyncMock()

        await ch._fanout_publish("fanout_ex", b'{"body": "test"}')
        call_kw = ch.client.xadd.call_args[1]
        assert call_kw["maxlen"] == 500

    async def test_put_message_with_message_ttl(self):
        ch = _make_channel(message_ttl=3600)

        mock_pipe = AsyncMock()
        mock_pipe.hset = AsyncMock()
        mock_pipe.expire = AsyncMock()
        mock_pipe.zadd = AsyncMock()
        mock_pipe.execute = AsyncMock()

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())
        await ch._put_message("q1", b'{"body": "test", "properties": {}, "headers": {}}')

        # Should call expire with TTL
        mock_pipe.expire.assert_called_once()
        ttl_arg = mock_pipe.expire.call_args[0][1]
        assert ttl_arg == 3600

    async def test_put_message_with_queue_ttl(self):
        ch = _make_channel()
        ch._expires["q1"] = 30_000

        mock_pipe = AsyncMock()
        mock_pipe.hset = AsyncMock()
        mock_pipe.zadd = AsyncMock()
        mock_pipe.pexpire = AsyncMock()
        mock_pipe.execute = AsyncMock()

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())
        await ch._put_message("q1", b'{"body": "test", "properties": {}, "headers": {}}')

        # Should call pexpire for queue TTL
        assert mock_pipe.pexpire.call_count >= 2  # queue_key + index_key

    async def test_put_message_native_delayed(self):
        ch = _make_channel()

        mock_pipe = AsyncMock()
        mock_pipe.hset = AsyncMock()
        mock_pipe.zadd = AsyncMock()
        mock_pipe.execute = AsyncMock()

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())

        # ETA far in the future (> RCI)
        future_eta = 9999999999.0
        msg = f'{{"body": "delayed", "properties": {{"eta": {future_eta}}}, "headers": {{}}}}'
        with patch("kombu.transport.valkey_redis.time", return_value=1000.0):
            await ch._put_message("q1", msg.encode())

        # Should set native_delayed=1 in hash
        hset_call = mock_pipe.hset.call_args
        mapping = hset_call[1]["mapping"]
        assert mapping["native_delayed"] == 1
        assert mapping["eta"] == future_eta

        # Should NOT add to queue sorted set (only to index)
        zadd_calls = mock_pipe.zadd.call_args_list
        assert any(MESSAGES_INDEX_PREFIX in str(c) for c in zadd_calls)
        # With native delayed, queue zadd should NOT happen
        assert not any(QUEUE_KEY_PREFIX in str(c) and MESSAGES_INDEX_PREFIX not in str(c) for c in zadd_calls)


# ---------------------------------------------------------------------------
# _refresh_queue_expires
# ---------------------------------------------------------------------------


class TestRefreshQueueExpires:
    async def test_refresh_expires_empty(self):
        ch = _make_channel()
        ch._expires = {}
        await ch._refresh_queue_expires()  # Should be no-op

    async def test_refresh_expires_calls_pexpire(self):
        ch = _make_channel()
        ch._expires = {"q1": 30_000, "q2": 60_000}

        mock_pipe = AsyncMock()
        mock_pipe.pexpire = AsyncMock()
        mock_pipe.execute = AsyncMock()

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())
        await ch._refresh_queue_expires()

        # 2 queues x 2 keys (queue + index) = 4 pexpire calls
        assert mock_pipe.pexpire.call_count == 4


# ---------------------------------------------------------------------------
# _deliver_to_consumer
# ---------------------------------------------------------------------------


class TestDeliverToConsumer:
    async def test_deliver_no_ack_not_tracked(self):
        ch = _make_channel()
        cb = MagicMock()
        ch._consumers["tag1"] = ("q1", cb, True)  # no_ack=True

        msg = ch._create_message("q1", {"body": "hi", "properties": {}, "headers": {}}, "dtag1")
        await ch._deliver_to_consumer("q1", msg)

        cb.assert_called_once()
        assert "dtag1" not in ch._delivered

    async def test_deliver_ack_tracked(self):
        ch = _make_channel()
        cb = MagicMock()
        ch._consumers["tag1"] = ("q1", cb, False)  # no_ack=False

        msg = ch._create_message("q1", {"body": "hi", "properties": {}, "headers": {}}, "dtag1")
        await ch._deliver_to_consumer("q1", msg)

        cb.assert_called_once()
        assert "dtag1" in ch._delivered

    async def test_deliver_async_callback(self):
        ch = _make_channel()
        cb = AsyncMock()
        ch._consumers["tag1"] = ("q1", cb, True)

        msg = ch._create_message("q1", {"body": "hi", "properties": {}, "headers": {}}, "dtag1")
        await ch._deliver_to_consumer("q1", msg)

        cb.assert_called_once()

    async def test_deliver_no_matching_consumer(self):
        ch = _make_channel()
        cb = MagicMock()
        ch._consumers["tag1"] = ("q2", cb, True)  # consumer for q2, not q1

        msg = ch._create_message("q1", {"body": "hi", "properties": {}, "headers": {}}, "dtag1")
        await ch._deliver_to_consumer("q1", msg)

        cb.assert_not_called()


# ---------------------------------------------------------------------------
# _create_message edge cases
# ---------------------------------------------------------------------------


class TestCreateMessage:
    async def test_base64_body(self):
        ch = _make_channel()
        import base64

        encoded = base64.b64encode(b"binary data").decode()
        payload = {
            "body": encoded,
            "properties": {},
            "headers": {"body_encoding": "base64"},
            "content-type": "application/octet-stream",
            "content-encoding": "utf-8",
        }
        msg = ch._create_message("q1", payload, "tag1")
        assert msg.body == b"binary data"

    async def test_dict_body(self):
        ch = _make_channel()
        payload = {
            "body": {"key": "value"},
            "properties": {},
            "headers": {},
            "content-type": "application/json",
            "content-encoding": "utf-8",
        }
        msg = ch._create_message("q1", payload, "tag1")
        assert isinstance(msg.body, bytes)

    async def test_binary_content_encoding(self):
        ch = _make_channel()
        payload = {
            "body": "raw string",
            "properties": {},
            "headers": {},
            "content-type": "application/octet-stream",
            "content-encoding": "binary",
        }
        msg = ch._create_message("q1", payload, "tag1")
        assert msg.body == b"raw string"


# ---------------------------------------------------------------------------
# fast_consume error path
# ---------------------------------------------------------------------------


class TestFastConsumeErrors:
    async def test_fast_consume_script_error(self):
        ch = _make_channel()
        consume_script = AsyncMock(side_effect=RuntimeError("script error"))
        ch._consume_script = consume_script
        result = await ch._fast_consume(["q1"])
        assert result is False

    async def test_fast_consume_passes_correct_args(self):
        ch = _make_channel(global_keyprefix="p:")
        consume_script = AsyncMock(return_value=None)
        ch._consume_script = consume_script

        await ch._fast_consume(["q1", "q2"])

        call_kw = consume_script.call_args[1]
        keys = call_kw["keys"]
        args = call_kw["args"]
        assert keys == ["p:queue:q1", "p:queue:q2"]
        assert args[0] == "p:"  # global_keyprefix
        assert args[1] == MESSAGE_KEY_PREFIX
        # args[2] = new_queue_at (float string)
        assert args[3] == MESSAGES_INDEX_PREFIX
        assert args[4] == "q1"
        assert args[5] == "q2"


# ---------------------------------------------------------------------------
# Transport connect/close edge cases
# ---------------------------------------------------------------------------


class TestTransportEdgeCases:
    async def test_connect_already_connected(self):
        t = _make_transport()
        t._connected = True
        # Should be a no-op
        await t.connect()

    async def test_create_channel_auto_connects(self):
        t = Transport(url="redis://localhost:6379")
        mock_aiolib = MagicMock()
        mock_client = AsyncMock()
        mock_subclient = AsyncMock()
        mock_aiolib.from_url.side_effect = [mock_client, mock_subclient]
        mock_client.ping = AsyncMock()
        mock_subclient.ping = AsyncMock()
        t._aiolib = mock_aiolib

        ch = await t.create_channel()
        assert t._connected
        assert isinstance(ch, Channel)

    async def test_transport_close_empty_channels(self):
        t = _make_transport()
        t._channels = []
        t._client.aclose = AsyncMock()
        t._subclient.aclose = AsyncMock()

        await t.close()
        assert not t._connected


# ---------------------------------------------------------------------------
# Enqueue due messages — batch limit warning
# ---------------------------------------------------------------------------


class TestEnqueueBatchLimit:
    async def test_enqueue_batch_limit_warning(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), False)

        from kombu.transport.valkey_redis import DEFAULT_REQUEUE_BATCH_LIMIT

        enqueue_script = AsyncMock(return_value=[DEFAULT_REQUEUE_BATCH_LIMIT, 0])
        ch._enqueue_script = enqueue_script

        with patch("kombu.transport.valkey_redis.logger") as mock_logger:
            enqueued, dropped = await ch._enqueue_due_messages()
            assert enqueued == DEFAULT_REQUEUE_BATCH_LIMIT
            mock_logger.warning.assert_called()

    async def test_enqueue_dropped_warning(self):
        ch = _make_channel(max_restore_count=3)
        ch._consumers["tag1"] = ("q1", MagicMock(), False)

        enqueue_script = AsyncMock(return_value=[2, 5])
        ch._enqueue_script = enqueue_script

        with patch("kombu.transport.valkey_redis.logger") as mock_logger:
            enqueued, dropped = await ch._enqueue_due_messages()
            assert dropped == 5
            mock_logger.warning.assert_called()

    async def test_enqueue_multiple_queues(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), False)
        ch._consumers["tag2"] = ("q2", MagicMock(), False)

        enqueue_script = AsyncMock(side_effect=[[3, 0], [2, 1]])
        ch._enqueue_script = enqueue_script

        enqueued, dropped = await ch._enqueue_due_messages()
        assert enqueued == 5
        assert dropped == 1
        assert enqueue_script.call_count == 2

    async def test_enqueue_skips_fanout_queues(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), False)
        ch._consumers["tag2"] = ("fq1", MagicMock(), False)
        ch.active_fanout_queues.add("fq1")

        enqueue_script = AsyncMock(return_value=[1, 0])
        ch._enqueue_script = enqueue_script

        await ch._enqueue_due_messages()
        # Should only process q1, not fq1
        assert enqueue_script.call_count == 1


# ---------------------------------------------------------------------------
# _put_message invalid JSON fallback
# ---------------------------------------------------------------------------


class TestPutMessageEdgeCases:
    async def test_put_message_invalid_json(self):
        ch = _make_channel()

        mock_pipe = AsyncMock()
        mock_pipe.hset = AsyncMock()
        mock_pipe.zadd = AsyncMock()
        mock_pipe.execute = AsyncMock()

        class PipeCtx:
            async def __aenter__(self):
                return mock_pipe

            async def __aexit__(self, *a):
                pass

        ch.client.pipeline = MagicMock(return_value=PipeCtx())
        # Send invalid JSON
        await ch._put_message("q1", b"not valid json at all")

        # Should still store something (fallback path)
        mock_pipe.hset.assert_called_once()
        mapping = mock_pipe.hset.call_args[1]["mapping"]
        assert "not valid json at all" in mapping["payload"]


# ---------------------------------------------------------------------------
# Reject fanout tag
# ---------------------------------------------------------------------------


class TestRejectFanout:
    async def test_reject_fanout_no_redis_ops(self):
        ch = _make_channel()
        ack_script = AsyncMock()
        ch._ack_script = ack_script
        ch._requeue_by_tag = AsyncMock()

        ch._fanout_tags.add("ftag1")
        ch._delivered["ftag1"] = ("fq1", MagicMock())

        await ch.basic_reject("ftag1", requeue=True)
        ack_script.assert_not_called()
        ch._requeue_by_tag.assert_not_called()
        assert "ftag1" not in ch._fanout_tags

    async def test_reject_fanout_no_requeue(self):
        ch = _make_channel()
        ack_script = AsyncMock()
        ch._ack_script = ack_script

        ch._fanout_tags.add("ftag2")
        ch._delivered["ftag2"] = ("fq1", MagicMock())

        await ch.basic_reject("ftag2", requeue=False)
        ack_script.assert_not_called()


# ---------------------------------------------------------------------------
# Recover edge cases
# ---------------------------------------------------------------------------


class TestRecoverEdgeCases:
    async def test_recover_no_requeue(self):
        ch = _make_channel()
        ch._requeue_by_tag = AsyncMock()
        ch._delivered["tag1"] = ("q1", MagicMock())
        ch._fanout_tags.add("ftag1")
        ch._delivered["ftag1"] = ("fq1", MagicMock())

        await ch.basic_recover(requeue=False)
        ch._requeue_by_tag.assert_not_called()
        assert len(ch._delivered) == 0
        assert len(ch._fanout_tags) == 0

    async def test_recover_skips_fanout(self):
        ch = _make_channel()
        ch._requeue_by_tag = AsyncMock(return_value=True)
        ch._delivered["tag1"] = ("q1", MagicMock())
        ch._fanout_tags.add("ftag1")
        ch._delivered["ftag1"] = ("fq1", MagicMock())

        await ch.basic_recover(requeue=True)
        # Only tag1 should be requeued, not ftag1
        ch._requeue_by_tag.assert_called_once_with("tag1", leftmost=True)
