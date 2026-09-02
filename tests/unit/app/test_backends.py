import importlib.util
import logging
import threading
from unittest.mock import patch

import pytest

from celery.app import backends
from celery.backends.cache import CacheBackend
from celery.contrib.testing.worker import start_worker
from celery.exceptions import ImproperlyConfigured


class CachedBackendWithTreadTrucking(CacheBackend):
    test_instance_count = 0
    test_call_stats = {}

    def _track_attribute_access(self, method_name):
        cls = type(self)

        instance_no = getattr(self, "_instance_no", None)
        if instance_no is None:
            instance_no = self._instance_no = cls.test_instance_count
            cls.test_instance_count += 1
            cls.test_call_stats[instance_no] = []

        cls.test_call_stats[instance_no].append({"thread_id": threading.get_ident(), "method_name": method_name})

    def __getattribute__(self, name):
        if name == "_instance_no" or name == "_track_attribute_access":
            return super().__getattribute__(name)

        if name.startswith("__") and name != "__init__":
            return super().__getattribute__(name)

        self._track_attribute_access(name)
        return super().__getattribute__(name)


class test_backends:
    @pytest.mark.parametrize(
        "url,expect_cls",
        [
            ("cache+memory://", CacheBackend),
        ],
    )
    def test_get_backend_aliases(self, url, expect_cls, app):
        backend, url = backends.by_url(url, app.loader)
        assert isinstance(backend(app=app, url=url), expect_cls)

    def test_unknown_backend(self, app):
        with pytest.raises(ImportError):
            backends.by_name("fasodaopjeqijwqe", app.loader)

    @pytest.mark.skipif(
        not importlib.util.find_spec("redis"),
        reason="redis not installed",
    )
    def test_backend_by_url(self, app, url="redis://localhost/1"):
        from celery.backends.valkey_redis import RedisBackend

        backend, url_ = backends.by_url(url, app.loader)
        assert backend is RedisBackend
        assert url_ == url

    def test_sym_raises_ValuError(self, app):
        with patch("celery.app.backends.symbol_by_name") as sbn:
            sbn.side_effect = ValueError()
            with pytest.raises(ImproperlyConfigured):
                backends.by_name("xxx.xxx:foo", app.loader)

    def test_backend_can_not_be_module(self, app):
        with pytest.raises(ImproperlyConfigured):
            backends.by_name(pytest, app.loader)

    @pytest.mark.celery(
        result_backend=f"{CachedBackendWithTreadTrucking.__module__}."
        f"{CachedBackendWithTreadTrucking.__qualname__}"
        f"+memory://"
    )
    def test_backend_thread_safety(self):
        @self.app.task
        def dummy_add_task(x, y):
            return x + y

        # The embedded worker sets logging up for the whole process, so it is
        # handed the level the session already runs at.
        with start_worker(self.app, perform_ping_check=False, loglevel=logging.getLogger().level):
            result = dummy_add_task.delay(6, 9)
            assert result.get(timeout=10) == 15

        call_stats = CachedBackendWithTreadTrucking.test_call_stats
        assert call_stats, "the tracking backend was never used"
        for backend_call_stats in call_stats.values():
            thread_ids = {call_stat["thread_id"] for call_stat in backend_call_stats}
            assert len(thread_ids) <= 1, "The same celery backend instance is used by multiple threads"
