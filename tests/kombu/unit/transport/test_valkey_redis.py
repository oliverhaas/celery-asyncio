"""Unit tests for the pure asyncio Redis transport.

All Redis operations are mocked — no Redis server required.
"""

import asyncio
import json
import logging
import re
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from kombu.entity import Exchange, Queue
from kombu.exceptions import InconsistencyError
from kombu.transport import valkey_redis
from kombu.transport.valkey_redis import (
    BINDING_SEP,
    DEFAULT_DELIVERY_LIMIT,
    DEFAULT_REQUEUE_CHECK_INTERVAL,
    DEFAULT_VISIBILITY_TIMEOUT,
    DROPPED_REPORT_LIMIT,
    MAX_CONSUME_BATCH,
    MESSAGE_KEY_PREFIX,
    MESSAGES_INDEX_PREFIX,
    MIN_BINDING_LIFETIME,
    MIN_QUEUE_EXPIRES,
    QUEUE_KEY_PREFIX,
    Channel,
    SweepStats,
    Transport,
    _parse_db_from_url,
    _queue_score,
    _topic_match,
)
from kombu.utils.json import dumps as json_dumps

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
    transport._connected = True
    transport._db = "0"
    transport._lock = asyncio.Lock()
    return transport


def _make_channel(**opts) -> Channel:
    """Create a Channel with a mocked transport."""
    transport = _make_transport(**opts)
    return Channel(transport)


def _stub_binding_writes(ch: Channel) -> Channel:
    """Stub the commands a bind or unbind sends to the binding table."""
    ch.client.zadd = AsyncMock()
    ch.client.zrem = AsyncMock()
    ch.client.pexpire = AsyncMock(return_value=1)
    ch.client.pttl = AsyncMock(return_value=-1)
    return ch


def _stub_binding_reads(ch: Channel, live=(), stale=()) -> Channel:
    """Stub the prune-on-read pipeline `_read_bindings` runs."""
    pipe = AsyncMock()
    pipe.execute = AsyncMock(return_value=[list(stale), len(stale), list(live)])
    ch.client.pipeline = MagicMock(return_value=_AsyncContext(pipe))
    return ch


class _AsyncContext:
    """Yield a fixed object from `async with`."""

    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *args):
        return False


class _MockPipeline:
    """Async context manager that collects pipeline calls."""

    def __init__(self):
        self.calls = []
        self._mock = AsyncMock()

    async def __aenter__(self):
        return self._mock

    async def __aexit__(self, *args):
        pass


def _stub_pipeline(channel, execute_results: list) -> AsyncMock:
    """Hand `channel.client.pipeline()` one execute() result per call."""
    pipe = AsyncMock()
    pipe.execute = AsyncMock(side_effect=execute_results)

    class PipeCtx:
        async def __aenter__(self):
            return pipe

        async def __aexit__(self, *args):
            return False

    channel.client.pipeline = MagicMock(side_effect=lambda *a, **kw: PipeCtx())
    return pipe


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

    def test_hash_alone_matches_every_key(self):
        assert _topic_match("user.created", "#") is True
        assert _topic_match("", "#") is True

    def test_leading_hash_matches_zero_or_more_words(self):
        assert _topic_match("created", "#.created") is True
        assert _topic_match("user.created", "#.created") is True
        assert _topic_match("a.b.created", "#.created") is True
        assert _topic_match("a.b.deleted", "#.created") is False

    def test_hash_between_words_matches_zero_or_more_words(self):
        assert _topic_match("user.created", "user.#.created") is True
        assert _topic_match("user.profile.created", "user.#.created") is True
        assert _topic_match("user.profile.created.late", "user.#.created") is False

    @pytest.mark.parametrize("key", ["a(b", "a+b", "a[b", "a|b", "a$b", "a.b?"])
    def test_a_metacharacter_in_the_pattern_is_literal(self, key):
        assert _topic_match(key, key) is True

    def test_a_metacharacter_does_not_widen_the_pattern(self):
        # `+` used to reach the regex engine, so `a+b` matched `aab`.
        assert _topic_match("aab", "a+b") is False
        assert _topic_match("ab", "a?b") is False

    def test_a_metacharacter_in_the_routing_key_still_matches_a_wildcard(self):
        assert _topic_match("user.a(b", "user.*") is True
        assert _topic_match("user.a(b.c", "user.#") is True


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
        assert ch._delivery_limit == DEFAULT_DELIVERY_LIMIT
        assert ch._consume_fast_mode is True
        assert ch._global_keyprefix == ""

    def test_custom_options(self):
        ch = _make_channel(
            visibility_timeout=120,
            message_ttl=3600,
            delivery_limit=5,
            global_keyprefix="test:",
        )
        assert ch._visibility_timeout == 120
        assert ch._message_ttl == 3600
        assert ch._delivery_limit == 5
        assert ch._global_keyprefix == "test:"

    def test_requeue_check_interval_defaults_and_is_configurable(self):
        assert _make_channel()._requeue_check_interval == DEFAULT_REQUEUE_CHECK_INTERVAL
        assert _make_channel(requeue_check_interval=5)._requeue_check_interval == 5

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_non_positive_requeue_check_interval_falls_back(self, bad):
        # Zero would turn the sweep into a busy loop and a negative value would
        # put every visibility deadline in the past.
        assert _make_channel(requeue_check_interval=bad)._requeue_check_interval == DEFAULT_REQUEUE_CHECK_INTERVAL

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
        await ch.exchange_delete("test_ex")
        assert "test_ex" not in ch._exchanges
        ch.client.delete.assert_awaited_once_with("_kombu.binding.test_ex")


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------


class TestQueueOps:
    async def test_declare_queue_auto_name(self):
        ch = _make_channel()
        _stub_binding_writes(ch)
        q = Queue("")
        name = await ch.declare_queue(q)
        assert name.startswith("amq.gen-")

    async def test_declare_queue_with_expires(self):
        ch = _make_channel()
        _stub_binding_writes(ch)
        q = Queue("test_q")
        q.queue_arguments = {"x-expires": 20_000}
        q.exchange = Exchange("ex", type="direct")
        q.routing_key = "rk"
        await ch.declare_queue(q)
        assert ch._expires["test_q"] == 20_000

    async def test_declare_queue_expires_clamped(self):
        ch = _make_channel()
        _stub_binding_writes(ch)
        q = Queue("test_q")
        q.queue_arguments = {"x-expires": 5_000}  # Below MIN_QUEUE_EXPIRES
        await ch.declare_queue(q)
        assert ch._expires["test_q"] == MIN_QUEUE_EXPIRES

    async def test_redeclaring_a_queue_updates_its_expires(self):
        """A redeclare is how a caller changes a TTL; first-declare-wins kept it stale."""
        ch = _make_channel()
        _stub_binding_writes(ch)
        q = Queue("test_q")
        q.queue_arguments = {"x-expires": 20_000, "x-message-ttl": 30_000}
        await ch.declare_queue(q)

        q.queue_arguments = {"x-expires": 60_000, "x-message-ttl": 90_000}
        await ch.declare_queue(q)
        assert ch._expires["test_q"] == 60_000
        assert ch._message_ttls["test_q"] == 90_000

    async def test_redeclaring_a_queue_without_expires_drops_the_ttl(self):
        ch = _make_channel()
        _stub_binding_writes(ch)
        q = Queue("test_q")
        q.queue_arguments = {"x-expires": 20_000, "x-message-ttl": 30_000}
        await ch.declare_queue(q)

        q.queue_arguments = {}
        await ch.declare_queue(q)
        assert "test_q" not in ch._expires
        assert "test_q" not in ch._message_ttls

    async def test_queue_expires_is_the_fallback_for_a_queue_without_one(self):
        ch = _make_channel(queue_expires=45)
        _stub_binding_writes(ch)
        q = Queue("test_q")
        await ch.declare_queue(q)
        assert ch._expires["test_q"] == 45_000

    async def test_an_explicit_expires_wins_over_the_fallback(self):
        ch = _make_channel(queue_expires=45)
        _stub_binding_writes(ch)
        q = Queue("test_q")
        q.queue_arguments = {"x-expires": 20_000}
        await ch.declare_queue(q)
        assert ch._expires["test_q"] == 20_000

    async def test_queue_expires_is_clamped_to_the_minimum(self):
        ch = _make_channel(queue_expires=1)
        assert ch._global_expires_ms() == MIN_QUEUE_EXPIRES

    async def test_no_queue_expires_leaves_queues_alone(self):
        ch = _make_channel()
        _stub_binding_writes(ch)
        await ch.declare_queue(Queue("test_q"))
        assert ch._global_expires_ms() is None
        assert "test_q" not in ch._expires

    async def test_queue_bind(self):
        ch = _make_channel()
        _stub_binding_writes(ch)
        await ch.queue_bind("q1", "ex1", "rk1")
        assert ("ex1", BINDING_SEP.join(["rk1", "rk1", "q1"])) in ch._binding_members["q1"]
        ch.client.zadd.assert_called_once()

    async def test_queue_unbind(self):
        ch = _make_channel()
        _stub_binding_writes(ch)
        await ch.queue_bind("q1", "ex1", "rk1")
        await ch.queue_unbind("q1", "ex1", "rk1")
        assert "q1" not in ch._binding_members
        ch.client.zrem.assert_awaited_once_with(
            "_kombu.binding.ex1",
            BINDING_SEP.join(["rk1", "rk1", "q1"]),
        )

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
        _stub_binding_reads(ch, live=[BINDING_SEP.join(["user.*", "user.*", "q1"]).encode()])
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

    async def test_put_message_stores_delivery_count(self):
        """New messages should have delivery_count=0."""
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

        # Check hset mapping includes delivery_count=0
        hset_call = mock_pipe.hset.call_args
        mapping = hset_call[1]["mapping"]
        assert mapping["delivery_count"] == 0


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

    async def test_fast_consume_with_delivery_count(self):
        """FAST consume should inject x-delivery-count header."""
        ch = _make_channel()
        ch._consume_fast_mode = True

        cb = MagicMock()
        ch._consumers["tag1"] = ("q1", cb, True)

        consume_script = AsyncMock(
            return_value=[
                b"q1",
                b"tag-1",
                b'{"body": "hello", "properties": {}, "headers": {}}',
                b"3",  # delivery_count = 3
            ],
        )
        ch._consume_script = consume_script

        result = await ch._fast_consume(["q1"])
        assert result is True
        # Check the message passed to callback has x-delivery-count
        call_args = cb.call_args
        msg = call_args[0][1]
        assert msg.headers.get("x-delivery-count") == 3


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
        # index key, message key, queue key. The queue key cancels a copy the
        # sweep restored while this consumer was still working (PORT-PLAN fix 1).
        assert keys == ["messages_index:q1", "message:tag1", "queue:q1"]
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
    async def test_enqueue_returns_the_four_counters(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), False)

        enqueue_script = AsyncMock(return_value=[5, 2, 3, 1, []])
        ch._enqueue_script = enqueue_script

        stats = await ch._enqueue_due_messages()
        assert stats == SweepStats(enqueued=5, dropped=2, redelivered=3, orphaned=1)

    async def test_enqueue_passes_delivery_limit(self):
        ch = _make_channel(delivery_limit=10)
        ch._consumers["tag1"] = ("q1", MagicMock(), False)

        enqueue_script = AsyncMock(return_value=[1, 0, 0, 0, []])
        ch._enqueue_script = enqueue_script

        await ch._enqueue_due_messages()
        call_args = enqueue_script.call_args
        args = call_args[1]["args"]
        assert args[7] == 10  # delivery_limit

    async def test_enqueue_passes_dropped_report_limit(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), False)

        enqueue_script = AsyncMock(return_value=[1, 0, 0, 0, []])
        ch._enqueue_script = enqueue_script

        await ch._enqueue_due_messages()
        args = enqueue_script.call_args[1]["args"]
        assert args[8] == DROPPED_REPORT_LIMIT

    async def test_enqueue_no_limit(self):
        ch = _make_channel(delivery_limit=None)
        ch._consumers["tag1"] = ("q1", MagicMock(), False)

        enqueue_script = AsyncMock(return_value=[1, 0, 0, 0, []])
        ch._enqueue_script = enqueue_script

        await ch._enqueue_due_messages()
        call_args = enqueue_script.call_args
        args = call_args[1]["args"]
        assert args[7] == -1  # -1 = no limit

    async def test_enqueue_no_active_queues(self):
        ch = _make_channel()
        # No consumers
        assert await ch._enqueue_due_messages() == SweepStats()

    async def test_one_failing_queue_does_not_stop_the_others(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), False)
        ch._consumers["tag2"] = ("q2", MagicMock(), False)

        ch._enqueue_script = AsyncMock(side_effect=[RuntimeError("boom"), [4, 0, 0, 0, []]])

        stats = await ch._enqueue_due_messages()
        assert stats.enqueued == 4


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

    async def test_get_lets_a_broker_failure_out(self):
        """An outage is not an empty queue, or get_nowait() reports Empty for it."""
        ch = _make_channel()
        script = AsyncMock(side_effect=RedisConnectionError("connection lost"))
        ch._get_consume_script = AsyncMock(return_value=script)

        with pytest.raises(RedisConnectionError):
            await ch.get("q1")

    async def test_get_empty(self):
        ch = _make_channel()

        consume_script = AsyncMock(return_value=None)
        ch._consume_script = consume_script

        msg = await ch.get("q1")
        assert msg is None

    async def test_get_with_delivery_count(self):
        ch = _make_channel()

        consume_script = AsyncMock(
            return_value=[
                b"q1",
                b"tag-1",
                b'{"body": "hello", "properties": {}, "headers": {}}',
                b"7",  # delivery_count=7
            ],
        )
        ch._consume_script = consume_script

        msg = await ch.get("q1", no_ack=True)
        assert msg.headers["x-delivery-count"] == 7


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
            delivery_limit=5,
            credential_provider="some.module.Provider",
            socket_timeout=10,
        )
        kw = t._client_kwargs()
        assert "global_keyprefix" not in kw
        assert "visibility_timeout" not in kw
        assert "delivery_limit" not in kw
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
        ch = Channel(t)
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
# Transport connect / close lifecycle
# ---------------------------------------------------------------------------


class TestTransportLifecycle:
    @staticmethod
    def _transport_with_clients(pings):
        """Build an unconnected transport whose from_url hands out `pings`."""
        t = Transport(url="redis://localhost:6379")
        clients = []
        for ping in pings:
            client = AsyncMock()
            client.ping = AsyncMock(side_effect=ping)
            client.aclose = AsyncMock()
            clients.append(client)
        t._aiolib = MagicMock()
        t._aiolib.from_url = MagicMock(side_effect=list(clients))
        return t, clients

    async def test_a_failed_second_ping_leaves_no_client_connected(self):
        t, clients = self._transport_with_clients([None, RedisConnectionError("nope")])

        with pytest.raises(RedisConnectionError):
            await t.connect()

        # Both pools were built, so both have to be closed.
        clients[0].aclose.assert_awaited_once()
        clients[1].aclose.assert_awaited_once()
        assert t._client is None
        assert t._subclient is None
        assert t._connected is False

    async def test_a_failed_connect_can_be_retried(self):
        t, clients = self._transport_with_clients([None, RedisConnectionError("nope")])
        with pytest.raises(RedisConnectionError):
            await t.connect()

        retry = [AsyncMock(), AsyncMock()]
        for client in retry:
            client.ping = AsyncMock()
        t._aiolib.from_url = MagicMock(side_effect=retry)

        await t.connect()
        assert t._connected is True
        assert t._client is retry[0]
        assert t._subclient is retry[1]

    async def test_racing_connects_build_one_pair_of_clients(self):
        async def slow_ping():
            await asyncio.sleep(0.01)

        t, clients = self._transport_with_clients([slow_ping, slow_ping, None, None])

        await asyncio.gather(t.connect(), t.connect(), t.connect())

        assert t._aiolib.from_url.call_count == 2
        assert t._client is clients[0]
        assert t._subclient is clients[1]

    async def test_a_cancelled_channel_drain_still_closes_the_sockets(self):
        t = _make_transport()
        t._client.aclose = AsyncMock()
        t._subclient.aclose = AsyncMock()
        client, subclient = t._client, t._subclient
        channel = MagicMock()
        channel.close = AsyncMock(side_effect=asyncio.CancelledError)
        t._channels = [channel]

        with pytest.raises(asyncio.CancelledError):
            await t.close()

        client.aclose.assert_awaited_once()
        subclient.aclose.assert_awaited_once()
        assert t._client is None
        assert t._subclient is None
        assert t._connected is False

    async def test_a_client_that_will_not_close_does_not_hide_the_other(self, caplog):
        t = _make_transport()
        t._subclient.aclose = AsyncMock(side_effect=RedisConnectionError("gone"))
        t._client.aclose = AsyncMock()
        client = t._client

        with caplog.at_level(logging.WARNING, logger="kombu.transport.valkey_redis"):
            await t.close()

        client.aclose.assert_awaited_once()
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert t._connected is False


# ---------------------------------------------------------------------------
# Documented transport options
# ---------------------------------------------------------------------------

_DOCSTRING_OPTION = re.compile(r"^\* ``(?P<name>[a-z_]+)``:", re.MULTILINE)

#: Documented options that name a keyword argument of the client library, so
#: the transport is supposed to hand them to from_url untouched.
_CLIENT_OPTIONS = frozenset(
    {
        "health_check_interval",
        "max_connections",
        "socket_connect_timeout",
        "socket_timeout",
    },
)

#: A usable value for every documented option, so a transport can be built
#: with all of them at once.
_OPTION_VALUES = {
    "block_timeout": 1.0,
    "credential_provider": None,
    "delivery_limit": 3,
    "fanout_prefix": True,
    "global_keyprefix": "prefix:",
    "health_check_interval": 5,
    "max_connections": 4,
    "message_ttl": -1,
    "queue_expires": 30,
    "requeue_check_interval": 5.0,
    "socket_connect_timeout": 2.0,
    "socket_timeout": 2.0,
    "stream_maxlen": 100,
    "visibility_timeout": 60.0,
}


class TestDocumentedTransportOptions:
    def test_the_docstring_lists_exactly_the_options_we_classify(self):
        documented = set(_DOCSTRING_OPTION.findall(valkey_redis.__doc__))
        assert documented == set(Transport._TRANSPORT_ONLY_OPTIONS) | _CLIENT_OPTIONS
        assert documented == set(_OPTION_VALUES)

    def test_only_client_options_are_forwarded(self):
        t = Transport(url="redis://localhost:6379", **_OPTION_VALUES)
        assert set(t._client_kwargs()) == set(_CLIENT_OPTIONS)

    async def test_the_client_accepts_every_forwarded_option(self):
        # from_url builds the connection pool eagerly, so an option this
        # transport should have consumed raises TypeError right here.
        t = Transport(url="redis://localhost:6379/0", **_OPTION_VALUES)
        client = t._aiolib.from_url(t._url, decode_responses=False, **t._client_kwargs())
        try:
            kwargs = client.connection_pool.connection_kwargs
            assert kwargs["socket_timeout"] == 2.0
            assert kwargs["socket_connect_timeout"] == 2.0
            assert kwargs["health_check_interval"] == 5
            assert client.connection_pool.max_connections == 4
        finally:
            await client.aclose()

    def test_every_transport_only_option_is_read_by_a_channel(self):
        ch = _make_channel(**{name: _OPTION_VALUES[name] for name in Transport._TRANSPORT_ONLY_OPTIONS})
        assert ch._global_keyprefix == "prefix:"
        assert ch._visibility_timeout == 60.0
        assert ch._requeue_check_interval == 5.0
        assert ch._queue_expires == 30
        assert ch._message_ttl == -1
        assert ch._stream_maxlen == 100
        assert ch._delivery_limit == 3
        assert ch._block_timeout == 1.0
        assert ch._fanout_prefix == "/0."


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

    def test_default_delivery_limit(self):
        # RabbitMQ has applied this to quorum queues since 4.0.
        assert DEFAULT_DELIVERY_LIMIT == 20


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

    async def test_none_delivery_count(self):
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
        _stub_binding_reads(ch, live=[binding.encode()])

        bindings = await ch._load_bindings("ex1")
        assert bindings == [("q1", "rk1")]

    async def test_load_json_format(self):
        ch = _make_channel()
        _stub_binding_reads(ch, live=[b'{"queue": "q1", "routing_key": "rk1"}'])

        bindings = await ch._load_bindings("ex1")
        assert bindings == [("q1", "rk1")]


# ---------------------------------------------------------------------------
# Binding lifetime
# ---------------------------------------------------------------------------


def _wrongtype() -> Exception:
    return ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value")


class TestBindingLifetime:
    async def test_a_bind_scores_the_member_with_its_staleness_deadline(self):
        ch = _make_channel()
        _stub_binding_writes(ch)
        ch._expires["q1"] = 900_000  # 900s, comfortably above the floor

        with patch("kombu.transport.valkey_redis.time", return_value=1000.0):
            await ch.queue_bind("q1", "ex1", "rk1")

        member = BINDING_SEP.join(["rk1", "rk1", "q1"])
        ch.client.zadd.assert_called_once_with("_kombu.binding.ex1", {member: 1900.0})

    async def test_a_queue_without_expires_binds_forever(self):
        """Nothing can refresh it, but nothing needs to: it never goes away on its own."""
        ch = _make_channel()
        _stub_binding_writes(ch)

        await ch.queue_bind("q1", "ex1", "rk1")

        (_key, mapping), _ = ch.client.zadd.call_args
        assert next(iter(mapping.values())) == float("inf")

    async def test_the_deadline_never_falls_below_the_minimum(self):
        """A control client's 10s reply queue must outlive the control call itself."""
        ch = _make_channel()
        ch._expires["q1"] = MIN_QUEUE_EXPIRES

        with patch("kombu.transport.valkey_redis.time", return_value=1000.0):
            assert ch._binding_stale_at("q1") == 1000.0 + MIN_BINDING_LIFETIME

    async def test_a_legacy_set_is_converted_on_bind(self):
        ch = _make_channel()
        _stub_binding_writes(ch)
        ch.client.zadd = AsyncMock(side_effect=[_wrongtype(), 1])
        script = AsyncMock(return_value=3)
        ch.client.register_script = MagicMock(return_value=script)

        await ch.queue_bind("q1", "ex1", "rk1")

        script.assert_awaited_once_with(keys=["_kombu.binding.ex1"])
        assert ch.client.zadd.await_count == 2

    async def test_a_bind_error_that_is_not_wrongtype_propagates(self):
        ch = _make_channel()
        _stub_binding_writes(ch)
        ch.client.zadd = AsyncMock(side_effect=ResponseError("NOSCRIPT"))

        with pytest.raises(ResponseError, match="NOSCRIPT"):
            await ch.queue_bind("q1", "ex1", "rk1")

    async def test_unbind_removes_the_member_in_place_from_a_legacy_set(self):
        """Unbinding is no reason to convert a table another deployment still writes."""
        ch = _make_channel()
        _stub_binding_writes(ch)
        ch.client.zrem = AsyncMock(side_effect=_wrongtype())
        ch.client.srem = AsyncMock()

        await ch.queue_unbind("q1", "ex1", "rk1")

        ch.client.srem.assert_awaited_once_with(
            "_kombu.binding.ex1",
            BINDING_SEP.join(["rk1", "rk1", "q1"]),
        )

    async def test_the_binding_key_gets_no_ttl_without_queue_expires(self):
        """A per-queue x-expires must not expire a table shared with queues that never do."""
        ch = _make_channel()
        _stub_binding_writes(ch)
        ch._expires["q1"] = 900_000

        await ch.queue_bind("q1", "ex1", "rk1")

        ch.client.pexpire.assert_not_called()

    async def test_the_binding_key_gets_a_ttl_with_queue_expires(self):
        ch = _make_channel(queue_expires=900)
        _stub_binding_writes(ch)

        await ch.queue_bind("q1", "ex1", "rk1")

        ch.client.pexpire.assert_awaited_once_with("_kombu.binding.ex1", 900_000, gt=True)

    async def test_a_key_that_has_no_ttl_yet_gets_one(self):
        """PEXPIRE GT reads a missing TTL as infinite and declines, so bootstrap it."""
        ch = _make_channel(queue_expires=900)
        _stub_binding_writes(ch)
        ch.client.pexpire = AsyncMock(side_effect=[0, 1])
        ch.client.pttl = AsyncMock(return_value=-1)

        await ch.queue_bind("q1", "ex1", "rk1")

        assert ch.client.pexpire.await_args_list[-1].args == ("_kombu.binding.ex1", 900_000)

    async def test_a_key_whose_ttl_is_already_longer_is_left_alone(self):
        ch = _make_channel(queue_expires=900)
        _stub_binding_writes(ch)
        ch.client.pexpire = AsyncMock(return_value=0)
        ch.client.pttl = AsyncMock(return_value=5_000_000)

        await ch.queue_bind("q1", "ex1", "rk1")

        assert ch.client.pexpire.await_count == 1

    async def test_reading_drops_the_bindings_that_aged_out(self):
        ch = _make_channel()
        stale = BINDING_SEP.join(["rk-gone", "rk-gone", "q-gone"]).encode()
        live = BINDING_SEP.join(["rk1", "rk1", "q1"]).encode()
        _stub_binding_reads(ch, live=[live], stale=[stale])

        with patch("kombu.transport.valkey_redis.logger") as mock_logger:
            assert await ch._load_bindings("ex1") == [("q1", "rk1")]

        fmt, *rest = mock_logger.info.call_args[0]
        assert "q-gone" in fmt % tuple(rest)

    async def test_reading_a_legacy_set_falls_back_to_smembers(self):
        """Kombu's own Redis transport writes a plain set; stay readable against it."""
        ch = _make_channel()
        member = BINDING_SEP.join(["rk1", "rk1", "q1"]).encode()
        pipe = AsyncMock()
        pipe.execute = AsyncMock(side_effect=_wrongtype())
        ch.client.pipeline = MagicMock(return_value=_AsyncContext(pipe))
        ch.client.smembers = AsyncMock(return_value={member})

        assert await ch._load_bindings("ex1") == [("q1", "rk1")]

    async def test_a_read_error_that_is_not_wrongtype_propagates(self):
        ch = _make_channel()
        pipe = AsyncMock()
        pipe.execute = AsyncMock(side_effect=ResponseError("LOADING"))
        ch.client.pipeline = MagicMock(return_value=_AsyncContext(pipe))

        with pytest.raises(ResponseError, match="LOADING"):
            await ch._load_bindings("ex1")

    async def test_the_refresh_rescores_the_bindings_this_channel_declared(self):
        ch = _make_channel()
        _stub_binding_writes(ch)
        ch._expires["q1"] = 900_000
        await ch.queue_bind("q1", "ex1", "rk1")

        pipe = AsyncMock()
        ch.client.pipeline = MagicMock(return_value=_AsyncContext(pipe))
        with patch("kombu.transport.valkey_redis.time", return_value=1000.0):
            await ch._refresh_queue_expires()

        member = BINDING_SEP.join(["rk1", "rk1", "q1"])
        pipe.zadd.assert_awaited_once_with("_kombu.binding.ex1", {member: 1900.0}, gt=True)

    async def test_the_refresh_leaves_another_channels_longer_deadline_alone(self):
        """GT, so a short-lived declarer cannot pull a route out from under a long one."""
        ch = _make_channel()
        _stub_binding_writes(ch)
        ch._expires["q1"] = 900_000
        await ch.queue_bind("q1", "ex1", "rk1")

        pipe = AsyncMock()
        ch.client.pipeline = MagicMock(return_value=_AsyncContext(pipe))
        await ch._refresh_queue_expires()

        assert pipe.zadd.await_args.kwargs == {"gt": True}

    async def test_publishing_rescores_the_binding(self):
        """Producers run no refresh timer, so the publish has to keep the route alive."""
        ch = _make_channel()
        _stub_binding_writes(ch)
        ch._expires["q1"] = 900_000
        await ch.queue_bind("q1", "ex1", "rk1")

        pipe = AsyncMock()
        ch.client.pipeline = MagicMock(return_value=_AsyncContext(pipe))
        with patch("kombu.transport.valkey_redis.time", return_value=1000.0):
            await ch._put_message("q1", b'{"body": "hi", "properties": {}}')

        member = BINDING_SEP.join(["rk1", "rk1", "q1"])
        assert any(
            c.args == ("_kombu.binding.ex1", {member: 1900.0}) and c.kwargs == {"gt": True}
            for c in pipe.zadd.await_args_list
        )

    async def test_a_transient_direct_exchange_drops_instead_of_raising(self):
        """Its bindings empty by design, and a redeclare cannot recreate someone else's."""
        ch = _make_channel()
        ch._exchanges["reply.ex"] = {"type": "direct", "durable": False}
        _stub_binding_reads(ch)
        ch._put_message = AsyncMock()

        with patch("kombu.transport.valkey_redis.logger") as mock_logger:
            await ch.publish(b'{"body": "hi"}', exchange="reply.ex", routing_key="rk")

        ch._put_message.assert_not_called()
        mock_logger.info.assert_called_once()

    async def test_a_durable_direct_exchange_still_raises(self):
        ch = _make_channel()
        ch._exchanges["ex1"] = {"type": "direct", "durable": True}
        _stub_binding_reads(ch)

        with pytest.raises(InconsistencyError, match="no bindings declared"):
            await ch.publish(b'{"body": "hi"}', exchange="ex1", routing_key="rk")

    async def test_an_undeclared_exchange_counts_as_durable(self):
        """Assume the bindings were meant to outlive their consumers, and redeclare."""
        ch = _make_channel()
        _stub_binding_reads(ch)

        assert ch._exchange_is_durable("never-declared")
        with pytest.raises(InconsistencyError):
            await ch.publish(b'{"body": "hi"}', exchange="never-declared", routing_key="rk")


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

        await ch.basic_cancel(tag)
        assert "fq1" not in ch.active_fanout_queues

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
        _stub_pipeline(
            ch,
            [[None, [b'{"body": "found", "properties": {}, "headers": {}}', b"0"]]],
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
        _stub_pipeline(
            ch,
            [
                [None, [None, None]],  # expired
                [None, [b'{"body": "ok", "properties": {}, "headers": {}}', b"2"]],
            ],
        )
        ch.client.zrem = AsyncMock()

        result = await ch._drain_expired_and_deliver("q1")
        assert result is True
        # Should have cleaned up the expired tag from index
        ch.client.zrem.assert_called_once()
        # Callback should have the message with x-delivery-count
        msg = cb.call_args[0][1]
        assert msg.headers.get("x-delivery-count") == 2

    async def test_all_expired(self):
        ch = _make_channel()

        # First zpopmin: expired, second: empty
        ch.client.zpopmin = AsyncMock(
            side_effect=[
                [(b"expired-1", 500.0)],
                [],  # queue now empty
            ],
        )
        _stub_pipeline(ch, [[None, [None, None]]])
        ch.client.zrem = AsyncMock()

        result = await ch._drain_expired_and_deliver("q1")
        assert result is False
        # The expired tag is dropped from the visibility index too.
        ch.client.zrem.assert_called_once()


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


class TestDrainEventsTimeout:
    """drain_events must come back inside the window the caller asked for."""

    @staticmethod
    def _channel_with_a_hung_iteration(**opts):
        async def never_returns(*args, **kwargs):
            await asyncio.sleep(30)
            return False

        ch = _make_channel(**opts)
        ch._consumers["tag1"] = ("q1", MagicMock(), True)
        ch._consume_regular = AsyncMock(side_effect=never_returns)
        return ch

    async def test_a_positive_timeout_is_honoured(self):
        # The whole point: the celery worker loop asks for min(eta_delay, 1.0)
        # and used to be held for block_timeout instead.
        ch = self._channel_with_a_hung_iteration(block_timeout=10.0)

        loop = asyncio.get_running_loop()
        start = loop.time()
        assert await ch.drain_events(timeout=0.2) is False
        elapsed = loop.time() - start

        assert 0.2 <= elapsed < 1.0

    async def test_the_broker_block_never_outlasts_the_timeout(self):
        ch = self._channel_with_a_hung_iteration(block_timeout=10.0)
        await ch.drain_events(timeout=0.2)
        assert ch._consume_regular.await_args.args[1] == pytest.approx(0.2, abs=0.05)

    async def test_block_timeout_stays_the_ceiling(self):
        ch = self._channel_with_a_hung_iteration(block_timeout=0.05)
        await ch.drain_events(timeout=5.0)
        assert ch._consume_regular.await_args.args[1] == 0.05

    async def test_a_timeout_longer_than_the_block_starts_another_iteration(self):
        ch = _make_channel(block_timeout=0.02)
        ch._consumers["tag1"] = ("q1", MagicMock(), True)

        attempts = 0

        async def empty_then_deliver(queues, block):
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(0.01)
            return attempts >= 3

        ch._consume_regular = empty_then_deliver

        loop = asyncio.get_running_loop()
        start = loop.time()
        assert await ch.drain_events(timeout=2.0) is True
        assert attempts == 3
        assert loop.time() - start < 1.0

    async def test_no_consumers_still_returns_inside_the_timeout(self):
        ch = _make_channel()
        loop = asyncio.get_running_loop()
        start = loop.time()
        assert await ch.drain_events(timeout=0.01) is False
        assert loop.time() - start < 0.1

    async def test_timeout_zero_polls_without_blocking(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), True)
        ch._fast_consume = AsyncMock(return_value=True)
        ch._slow_consume = AsyncMock(return_value=True)

        assert await ch.drain_events(timeout=0) is True
        ch._fast_consume.assert_awaited_once_with(["q1"])
        ch._slow_consume.assert_not_awaited()
        assert ch._consume_iter_task is None

    async def test_timeout_zero_delivers_a_ready_fanout_message(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("fq1", MagicMock(), True)
        ch._fanout_queues["fq1"] = ("fan", "*")
        ch.active_fanout_queues.add("fq1")
        ch._xread_wait = AsyncMock(return_value=True)

        assert await ch.drain_events(timeout=0) is True
        ch._xread_wait.assert_awaited_once_with(0)

    async def test_timeout_zero_leaves_an_outstanding_fanout_read_alone(self):
        # Two reads from the same stream offset would deliver the same
        # message twice.
        ch = _make_channel()
        ch._consumers["tag1"] = ("fq1", MagicMock(), True)
        ch._fanout_queues["fq1"] = ("fan", "*")
        ch.active_fanout_queues.add("fq1")
        ch._xread_wait = AsyncMock(return_value=True)
        ch._xread_iter_task = asyncio.create_task(asyncio.sleep(5))

        assert await ch.drain_events(timeout=0) is False
        ch._xread_wait.assert_not_awaited()

        ch._xread_iter_task.cancel()

    async def test_timeout_none_runs_one_iteration_bounded_by_block_timeout(self):
        ch = _make_channel(block_timeout=0.05)
        ch._consumers["tag1"] = ("q1", MagicMock(), True)

        async def block_then_give_up(queues, block):
            await asyncio.sleep(block)
            return False

        ch._consume_regular = AsyncMock(side_effect=block_then_give_up)

        loop = asyncio.get_running_loop()
        start = loop.time()
        assert await ch.drain_events() is False
        assert loop.time() - start < 1.0
        # One iteration only: no deadline means no reason to start another.
        assert ch._consume_regular.await_count == 1
        assert ch._consume_regular.await_args.args[1] == 0.05


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

    async def test_a_timed_out_drain_leaves_its_iteration_pending(self):
        # A drain that runs out of time must leave the iteration pending, not
        # cancel it, so any in-flight broker state stays in sync.
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), True)

        cancelled = False
        will_finish = asyncio.Event()

        async def slow_iteration(*args, **kwargs):
            nonlocal cancelled
            try:
                await asyncio.sleep(0.2)
                will_finish.set()
                return False
            except asyncio.CancelledError:
                cancelled = True
                raise

        ch._consume_regular = slow_iteration

        assert await ch.drain_events(timeout=0.05) is False
        assert ch._consume_iter_task is not None
        assert not ch._consume_iter_task.done()
        assert cancelled is False

        # Let the iteration finish naturally so we don't leak a task.
        await will_finish.wait()

    async def test_pending_iteration_reused_by_next_drain(self):
        # The iteration a timed-out drain left behind is reused by the next
        # call: _consume_regular runs only once.
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), True)

        invocations = 0

        async def slow_then_deliver(*args, **kwargs):
            nonlocal invocations
            invocations += 1
            await asyncio.sleep(0.1)
            return True

        ch._consume_regular = slow_then_deliver

        assert await ch.drain_events(timeout=0.02) is False
        assert invocations == 1
        first_task = ch._consume_iter_task
        assert first_task is not None

        assert await ch.drain_events(timeout=1.0) is True
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
            ch._safe_consume_iter(["q1"], 0.05),
        )

        await ch.close()
        assert delivered.is_set()
        assert ch._consume_iter_task is None
        assert ch._closing is True

    async def test_warns_when_only_one_iteration_stalls(self, monkeypatch, caplog):
        # If asyncio.wait's FIRST_COMPLETED fires on a healthy iteration while
        # the sibling is hung, the hung-task warning must still fire.
        from kombu.transport import valkey_redis

        monkeypatch.setattr(valkey_redis, "CONSUMER_STALL_HEADROOM", 0.0)

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
            ch._safe_consume_iter(["q1"], 0.05),
        )

        with caplog.at_level("WARNING", logger="kombu.transport.valkey_redis"):
            await ch.close()
        assert ch._consume_iter_task is None
        assert any("did not drain" in rec.message for rec in caplog.records)

    async def test_an_iteration_cancelled_by_close_does_not_cancel_the_caller(self):
        """close() cancels a hung iteration; the drain_events waiting on it was not cancelled."""
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), True)

        running = asyncio.Event()

        async def hung_iteration(*args, **kwargs):
            running.set()
            await asyncio.sleep(60)
            return False

        ch._consume_regular = hung_iteration
        drain = asyncio.create_task(ch.drain_events(timeout=30.0))
        await running.wait()
        ch._consume_iter_task.cancel()

        assert await drain is False
        assert not drain.cancelled()
        assert ch._consume_iter_task is None

    async def test_a_failed_iteration_is_reported(self, caplog):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), True)

        async def broken_iteration(*args, **kwargs):
            raise RedisConnectionError("connection lost")

        ch._safe_consume_iter = broken_iteration
        with caplog.at_level("WARNING", logger="kombu.transport.valkey_redis"):
            assert await ch.drain_events(timeout=1.0) is False
        assert [rec.message for rec in caplog.records] == ["Consumer iteration failed."]
        assert caplog.records[0].exc_info[1].args == ("connection lost",)


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
        ch = _make_channel(requeue_check_interval=0.01)
        call_count = 0

        async def mock_enqueue():
            nonlocal call_count
            call_count += 1
            return (0, 0)

        ch._enqueue_due_messages = mock_enqueue

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
        ch = _make_channel(requeue_check_interval=0.01)
        call_count = 0

        async def mock_enqueue():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")
            return (0, 0)

        ch._enqueue_due_messages = mock_enqueue

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

    async def test_slow_consume_delivery_count_injected(self):
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
        assert msg.headers["x-delivery-count"] == 5

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

    async def test_publishing_refreshes_the_stream_ttl(self):
        """maxlen trims a stream but never removes it, so an exchange nobody
        publishes to again stays in Redis for the server's life."""
        ch = _make_channel(queue_expires=45)
        ch.client.xadd = AsyncMock()
        ch.client.pexpire = AsyncMock()

        await ch._fanout_publish("fanout_ex", b'{"body": "test"}')

        ch.client.pexpire.assert_called_once_with(ch._fanout_stream_key("fanout_ex"), 45_000)

    async def test_without_queue_expires_the_stream_gets_no_ttl(self):
        ch = _make_channel()
        ch.client.xadd = AsyncMock()
        ch.client.pexpire = AsyncMock()

        await ch._fanout_publish("fanout_ex", b'{"body": "test"}')

        ch.client.pexpire.assert_not_called()

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
# A consumer callback that raises
# ---------------------------------------------------------------------------


class TestFailingCallback:
    @staticmethod
    def _channel_with_a_broken_callback(no_ack: bool = False):
        ch = _make_channel()

        def explode(body, message):
            raise ValueError("bad task")

        ch._consumers["tag1"] = ("q1", explode, no_ack)
        ch._requeue_by_tag = AsyncMock(return_value=True)
        ch.client.zadd = AsyncMock()
        return ch

    async def test_the_message_goes_back_through_the_counting_requeue(self):
        ch = self._channel_with_a_broken_callback()
        msg = ch._create_message("q1", {"body": "hi", "properties": {}, "headers": {}}, "dtag1")

        assert await ch._deliver_to_consumer("q1", msg) is True

        # The requeue script is the only path that bumps delivery_count and so
        # the only one delivery_limit can ever act on.
        ch._requeue_by_tag.assert_awaited_once_with("dtag1")
        ch.client.zadd.assert_not_called()
        assert "dtag1" not in ch._delivered

    async def test_the_failure_is_logged_with_its_traceback(self, caplog):
        ch = self._channel_with_a_broken_callback()
        msg = ch._create_message("q1", {"body": "hi", "properties": {}, "headers": {}}, "dtag1")

        with caplog.at_level(logging.ERROR, logger="kombu.transport.valkey_redis"):
            await ch._deliver_to_consumer("q1", msg)

        record = next(r for r in caplog.records if r.levelno == logging.ERROR)
        assert "dtag1" in record.getMessage()
        assert record.exc_info is not None
        assert isinstance(record.exc_info[1], ValueError)

    async def test_a_fanout_delivery_has_nothing_to_requeue(self):
        ch = self._channel_with_a_broken_callback(no_ack=True)
        ch._fanout_tags.add("dtag1")
        msg = ch._create_message("q1", {"body": "hi", "properties": {}, "headers": {}}, "dtag1")

        assert await ch._deliver_to_consumer("q1", msg) is True

        ch._requeue_by_tag.assert_not_called()
        assert "dtag1" not in ch._fanout_tags

    async def test_the_claimed_path_does_not_restore_on_top_of_the_requeue(self):
        ch = self._channel_with_a_broken_callback()
        payload = json.dumps({"body": "hi", "properties": {}, "headers": {}})

        assert await ch._deliver_claimed("q1", "dtag1", payload, 0) is True

        ch._requeue_by_tag.assert_awaited_once_with("dtag1")
        # _restore_to_queue would put the tag back without counting the
        # redelivery, which is what let a poison message cycle forever.
        ch.client.zadd.assert_not_called()

    async def test_a_cancelled_delivery_still_restores_without_counting(self):
        ch = _make_channel()

        async def cancelled(body, message):
            raise asyncio.CancelledError

        ch._consumers["tag1"] = ("q1", cancelled, False)
        ch._requeue_by_tag = AsyncMock(return_value=True)
        ch.client.zadd = AsyncMock()
        payload = json.dumps({"body": "hi", "properties": {}, "headers": {}})

        with pytest.raises(asyncio.CancelledError):
            await ch._deliver_claimed("q1", "dtag1", payload, 0)

        ch._requeue_by_tag.assert_not_called()
        ch.client.zadd.assert_awaited_once()


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
        assert args[4] == "1"  # batch size, no prefetch set
        assert args[5] == "q1"
        assert args[6] == "q2"


# ---------------------------------------------------------------------------
# Restore-on-cancel after server-side pop
# ---------------------------------------------------------------------------


class TestRestoreOnCancel:
    """After BZMPOP/Lua already popped server-side, a cancel before delivery
    must push the tag back onto the queue ZSET so the next consume cycle
    re-picks it up. Belt-and-suspenders behind persistent consume tasks:
    persistent tasks prevent the hot-path cancel, this guards the cold paths
    (close() hard-cancel, signal during iteration, etc.).
    """

    @staticmethod
    def _pipe_ctx(pipe):
        class PipeCtx:
            async def __aenter__(self):
                return pipe

            async def __aexit__(self, *a):
                pass

        return PipeCtx()

    async def test_slow_consume_restores_on_pipeline_cancel(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), True)
        ch.client.bzmpop = AsyncMock(
            return_value=(b"queue:q1", [(b"tag-1", 1234.5)]),
        )

        mock_pipe = AsyncMock()
        mock_pipe.zadd = AsyncMock()
        mock_pipe.hmget = AsyncMock()
        mock_pipe.execute = AsyncMock(side_effect=asyncio.CancelledError())
        ch.client.pipeline = MagicMock(return_value=self._pipe_ctx(mock_pipe))
        ch.client.zadd = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await ch._slow_consume(["q1"], timeout=1.0)

        # Restored back onto the queue ZSET with the original score.
        ch.client.zadd.assert_awaited_once()
        args, _ = ch.client.zadd.call_args
        assert args[0] == ch._queue_key("q1")
        assert args[1] == {"tag-1": 1234.5}

    async def test_slow_consume_restores_on_deliver_cancel(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), True)
        ch.client.bzmpop = AsyncMock(
            return_value=(b"queue:q1", [(b"tag-2", 2222.0)]),
        )

        mock_pipe = AsyncMock()
        mock_pipe.zadd = AsyncMock()
        mock_pipe.hmget = AsyncMock()
        mock_pipe.execute = AsyncMock(
            return_value=[
                None,
                [b'{"body": "x", "properties": {}, "headers": {}}', b"0"],
            ],
        )
        ch.client.pipeline = MagicMock(return_value=self._pipe_ctx(mock_pipe))
        ch.client.zadd = AsyncMock()
        ch._deliver_to_consumer = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await ch._slow_consume(["q1"], timeout=1.0)

        ch.client.zadd.assert_awaited_once()
        args, _ = ch.client.zadd.call_args
        assert args[1] == {"tag-2": 2222.0}
        # _delivered must not retain a tag we just put back on the queue.
        assert "tag-2" not in ch._delivered

    async def test_fast_consume_restores_on_deliver_cancel(self):
        ch = _make_channel()
        ch._consume_fast_mode = True
        ch._consumers["tag1"] = ("q1", MagicMock(), True)
        ch._consume_script = AsyncMock(
            return_value=[
                b"q1",
                b"tag-fast",
                b'{"body": "x", "properties": {}, "headers": {}}',
                b"0",
            ],
        )
        ch.client.zadd = AsyncMock()
        ch._deliver_to_consumer = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await ch._fast_consume(["q1"])

        ch.client.zadd.assert_awaited_once()
        args, _ = ch.client.zadd.call_args
        assert args[0] == ch._queue_key("q1")
        assert "tag-fast" in args[1]
        assert "tag-fast" not in ch._delivered

    async def test_restore_to_queue_swallows_redis_failure(self):
        # Best-effort: if the restore itself fails, visibility-timeout handles
        # eventual recovery. The original exception must still propagate.
        ch = _make_channel()
        ch.client.zadd = AsyncMock(side_effect=ConnectionError("down"))

        # Should not raise.
        await ch._restore_to_queue("q1", "tag-x", score=42.0)
        ch.client.zadd.assert_awaited_once()


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

        enqueue_script = AsyncMock(return_value=[DEFAULT_REQUEUE_BATCH_LIMIT, 0, 0, 0, []])
        ch._enqueue_script = enqueue_script

        with patch("kombu.transport.valkey_redis.logger") as mock_logger:
            stats = await ch._enqueue_due_messages()
            assert stats.enqueued == DEFAULT_REQUEUE_BATCH_LIMIT
            mock_logger.warning.assert_called()

    async def test_enqueue_dropped_error_names_the_messages(self):
        ch = _make_channel(delivery_limit=3)
        ch._consumers["tag1"] = ("q1", MagicMock(), False)

        payload = json_dumps({"headers": {"task": "proj.add", "id": "abc"}}).encode()
        enqueue_script = AsyncMock(return_value=[2, 5, 0, 0, [payload]])
        ch._enqueue_script = enqueue_script

        with patch("kombu.transport.valkey_redis.logger") as mock_logger:
            stats = await ch._enqueue_due_messages()

        assert stats.dropped == 5
        # Five went, one payload came back: the message says so rather than
        # implying only one was lost.
        fmt, *rest = mock_logger.error.call_args[0]
        assert "proj.add (id abc), ..." in fmt % tuple(rest)

    async def test_enqueue_reports_redelivered_and_orphaned(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), False)

        ch._enqueue_script = AsyncMock(return_value=[0, 0, 3, 2, []])

        with patch("kombu.transport.valkey_redis.logger") as mock_logger:
            stats = await ch._enqueue_due_messages()

        assert (stats.redelivered, stats.orphaned) == (3, 2)
        assert mock_logger.info.call_count == 2

    async def test_enqueue_multiple_queues(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), False)
        ch._consumers["tag2"] = ("q2", MagicMock(), False)

        enqueue_script = AsyncMock(side_effect=[[3, 0, 0, 0, []], [2, 1, 0, 0, []]])
        ch._enqueue_script = enqueue_script

        stats = await ch._enqueue_due_messages()
        assert stats.enqueued == 5
        assert stats.dropped == 1
        assert enqueue_script.call_count == 2

    async def test_enqueue_skips_fanout_queues(self):
        ch = _make_channel()
        ch._consumers["tag1"] = ("q1", MagicMock(), False)
        ch._consumers["tag2"] = ("fq1", MagicMock(), False)
        ch.active_fanout_queues.add("fq1")

        enqueue_script = AsyncMock(return_value=[1, 0, 0, 0, []])
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


class TestPrefetchBatching:
    """basic_qos turns one consume round-trip into a batch that is handed out
    from a local buffer, so N messages cost one script call instead of N.
    """

    def _channel(self):
        ch = Channel.__new__(Channel)
        ch._global_keyprefix = ""
        ch._visibility_timeout = 100
        ch._requeue_check_interval = 10
        ch._no_ack_queues = set()
        ch._delivered = {}
        ch._prefetch_count = 0
        ch._prefetch_buffer = deque()
        ch._get_consume_script = AsyncMock()
        ch._deliver_claimed = AsyncMock(return_value=True)
        ch._queue_key = lambda q: f"queue:{q}"
        return ch

    @pytest.mark.asyncio
    async def test_batch_size_follows_prefetch_count(self):
        ch = self._channel()
        script = AsyncMock(return_value=None)
        ch._get_consume_script.return_value = script

        await ch.basic_qos(prefetch_count=8)
        await ch._fast_consume(["q1"])

        assert script.call_args[1]["args"][4] == "8"

    @pytest.mark.asyncio
    async def test_unacked_messages_do_not_shrink_the_batch(self):
        """The count bounds the buffer, not the unacked set: a batch is fetched
        only once the previous one has been handed out, so subtracting what the
        consumer still owes would throttle batching to nothing under load.
        """
        ch = self._channel()
        ch._delivered = {f"t{i}": ("q1", None) for i in range(9)}
        script = AsyncMock(return_value=None)
        ch._get_consume_script.return_value = script

        await ch.basic_qos(prefetch_count=5)
        await ch._fast_consume(["q1"])

        assert script.call_args[1]["args"][4] == "5"

    @pytest.mark.asyncio
    async def test_batch_size_is_capped(self):
        ch = self._channel()
        script = AsyncMock(return_value=None)
        ch._get_consume_script.return_value = script

        await ch.basic_qos(prefetch_count=10_000)
        await ch._fast_consume(["q1"])

        assert script.call_args[1]["args"][4] == str(MAX_CONSUME_BATCH)

    @pytest.mark.asyncio
    async def test_extra_messages_are_buffered_then_drained(self):
        ch = self._channel()
        script = AsyncMock(
            return_value=["q1", "t1", '{"a": 1}', "1", "q1", "t2", '{"a": 2}', "1"],
        )
        ch._get_consume_script.return_value = script

        await ch.basic_qos(prefetch_count=4)
        assert await ch._fast_consume(["q1"])

        assert ch._deliver_claimed.await_args[0][1] == "t1"
        assert len(ch._prefetch_buffer) == 1

        assert await ch._fast_consume(["q1"])
        assert ch._deliver_claimed.await_args[0][1] == "t2"
        assert not ch._prefetch_buffer
        assert script.await_count == 1

    @pytest.mark.asyncio
    async def test_buffer_is_restored_for_the_cancelled_queue_only(self):
        ch = self._channel()
        ch._prefetch_buffer.extend(
            [("q1", "t1", "{}", 1), ("q2", "t2", "{}", 1)],
        )
        ch._restore_to_queue = AsyncMock()

        await ch._restore_prefetch_buffer("q1")

        ch._restore_to_queue.assert_awaited_once_with("q1", "t1")
        assert list(ch._prefetch_buffer) == [("q2", "t2", "{}", 1)]
