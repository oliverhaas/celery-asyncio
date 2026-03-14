import time
from unittest.mock import Mock, patch

import pytest

from celery.backends.asynchronous import AsyncBackendMixin, BaseResultConsumer
from celery.exceptions import TimeoutError


class test_poll_interval:
    """Tests for AsyncBackendMixin._poll_interval()."""

    def test_none_timeout_returns_default(self):
        assert AsyncBackendMixin._poll_interval(None) == 0.5

    def test_short_timeout_clamps_to_minimum(self):
        # timeout=1 → 1/20=0.05 → clamped to 0.1
        assert AsyncBackendMixin._poll_interval(1) == 0.1

    def test_medium_timeout(self):
        # timeout=10 → 10/20=0.5
        assert AsyncBackendMixin._poll_interval(10) == 0.5

    def test_long_timeout(self):
        # timeout=60 → 60/20=3.0
        assert AsyncBackendMixin._poll_interval(60) == 3.0

    def test_very_long_timeout_clamps_to_maximum(self):
        # timeout=300 → 300/20=15 → clamped to 10.0
        assert AsyncBackendMixin._poll_interval(300) == 10.0

    def test_zero_timeout_clamps_to_minimum(self):
        # timeout=0 → 0/20=0 → clamped to 0.1
        assert AsyncBackendMixin._poll_interval(0) == 0.1


class test_BaseResultConsumer:
    """Tests for the minimal BaseResultConsumer stub."""

    def test_init_stores_backend_and_app(self):
        backend = Mock()
        app = Mock()
        consumer = BaseResultConsumer(backend, app, accept=None, pending_results={}, pending_messages={})
        assert consumer.backend is backend
        assert consumer.app is app

    def test_methods_are_noop(self):
        consumer = BaseResultConsumer(Mock(), Mock(), None, {}, {})
        # These should all be no-ops (not raise)
        consumer.start("some-task-id")
        consumer.stop()
        consumer.consume_from("some-task-id")
        consumer.cancel_for("some-task-id")


class test_AsyncBackendMixin:
    """Tests for AsyncBackendMixin.wait_for_pending() and iter_native()."""

    @pytest.fixture
    def backend(self):
        """Create a mock backend with AsyncBackendMixin methods."""
        backend = Mock(spec=["get_task_meta", "_ensure_not_eager",
                             "get_key_for_task", "mget", "_mget_to_results"])
        # Bind mixin methods to the mock
        backend._poll_interval = AsyncBackendMixin._poll_interval
        backend.wait_for_pending = AsyncBackendMixin.wait_for_pending.__get__(backend)
        backend.iter_native = AsyncBackendMixin.iter_native.__get__(backend)
        backend.add_pending_result = AsyncBackendMixin.add_pending_result.__get__(backend)
        backend.remove_pending_result = AsyncBackendMixin.remove_pending_result.__get__(backend)
        return backend

    def test_wait_for_pending_immediate_result(self, backend):
        result = Mock()
        result.id = "task-1"
        backend.get_task_meta.return_value = {"status": "SUCCESS", "result": 42}
        result.maybe_throw.return_value = 42

        with patch("celery.backends.asynchronous.time.sleep") as mock_sleep:
            ret = backend.wait_for_pending(result, timeout=10)

        assert ret == 42
        result._maybe_set_cache.assert_called_once()
        mock_sleep.assert_not_called()

    def test_wait_for_pending_polls_until_ready(self, backend):
        result = Mock()
        result.id = "task-1"
        backend.get_task_meta.side_effect = [
            {"status": "PENDING", "result": None},
            {"status": "PENDING", "result": None},
            {"status": "SUCCESS", "result": 42},
        ]
        result.maybe_throw.return_value = 42

        with patch("celery.backends.asynchronous.time.sleep"):
            ret = backend.wait_for_pending(result, timeout=10)

        assert ret == 42
        assert backend.get_task_meta.call_count == 3

    def test_wait_for_pending_timeout(self, backend):
        result = Mock()
        result.id = "task-1"
        backend.get_task_meta.return_value = {"status": "PENDING", "result": None}

        # Make time.monotonic advance past the timeout
        times = iter([0.0, 0.0, 0.5, 0.5, 1.1])
        with (
            patch("celery.backends.asynchronous.time.sleep"),
            patch("celery.backends.asynchronous.time.monotonic", side_effect=times),
        ):
            with pytest.raises(TimeoutError):
                backend.wait_for_pending(result, timeout=1.0)

    def test_wait_for_pending_on_message_callback(self, backend):
        result = Mock()
        result.id = "task-1"
        meta = {"status": "SUCCESS", "result": 42}
        backend.get_task_meta.return_value = meta
        on_message = Mock()

        with patch("celery.backends.asynchronous.time.sleep"):
            backend.wait_for_pending(result, timeout=10, on_message=on_message)

        on_message.assert_called_once_with(meta)

    def test_wait_for_pending_on_interval_callback(self, backend):
        result = Mock()
        result.id = "task-1"
        backend.get_task_meta.side_effect = [
            {"status": "PENDING", "result": None},
            {"status": "SUCCESS", "result": 42},
        ]
        on_interval = Mock()

        with patch("celery.backends.asynchronous.time.sleep"):
            backend.wait_for_pending(result, timeout=10, on_interval=on_interval)

        on_interval.assert_called_once()

    def test_iter_native_empty_results(self, backend):
        result = Mock()
        result.results = []
        items = list(backend.iter_native(result, timeout=10))
        assert items == []

    def test_iter_native_cached_results(self, backend):
        r1 = Mock()
        r1.id = "task-1"
        r1._cache = {"status": "SUCCESS", "result": 1}
        r2 = Mock()
        r2.id = "task-2"
        r2._cache = {"status": "SUCCESS", "result": 2}
        result = Mock()
        result.results = [r1, r2]

        items = list(backend.iter_native(result, timeout=10))
        assert len(items) == 2
        assert ("task-1", r1._cache) in items
        assert ("task-2", r2._cache) in items
        # MGET should not have been called since everything was cached
        backend.mget.assert_not_called()

    def test_iter_native_polls_for_results(self, backend):
        r1 = Mock()
        r1.id = "task-1"
        r1._cache = None
        del r1._cache  # Make hasattr return False
        result = Mock()
        result.results = [r1]

        meta = {"status": "SUCCESS", "result": 42}
        backend.get_key_for_task.return_value = "celery-task-meta-task-1"
        backend._mget_to_results.return_value = {"task-1": meta}

        with patch("celery.backends.asynchronous.time.sleep"):
            items = list(backend.iter_native(result, timeout=10))

        assert items == [("task-1", meta)]
        r1._maybe_set_cache.assert_called_once_with(meta)

    def test_add_pending_result_returns_result(self, backend):
        result = Mock()
        assert backend.add_pending_result(result) is result

    def test_remove_pending_result_returns_result(self, backend):
        result = Mock()
        assert backend.remove_pending_result(result) is result
