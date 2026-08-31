"""Tests for kombu.transport._valkey_redis_compat."""

from unittest.mock import patch

import pytest

from kombu.transport._valkey_redis_compat import (
    get_all_channel_errors,
    get_all_connection_errors,
    normalize_url,
    resolve_async_lib,
    resolve_exceptions,
    resolve_lib,
)


class TestResolveLib:
    """Tests for resolve_lib()."""

    def test_redis_url_prefers_redis(self):
        lib = resolve_lib("redis://localhost:6379")
        assert lib.__name__ == "redis"

    def test_rediss_url_prefers_redis(self):
        lib = resolve_lib("rediss://localhost:6379")
        assert lib.__name__ == "redis"

    def test_no_url_returns_installed_lib(self):
        lib = resolve_lib("")
        assert lib.__name__ in ("redis", "valkey")

    def test_neither_installed_raises(self):
        with (
            patch("kombu.transport._valkey_redis_compat._VALKEY_AVAILABLE", False),
            patch("kombu.transport._valkey_redis_compat._REDIS_AVAILABLE", False),
            pytest.raises(ImportError, match="Valkey/Redis client library"),
        ):
            resolve_lib("redis://localhost")

    def test_redis_url_falls_back_to_valkey(self):
        with patch("kombu.transport._valkey_redis_compat._REDIS_AVAILABLE", False):
            # If only valkey is installed, redis:// should fall back
            try:
                lib = resolve_lib("redis://localhost")
                assert lib.__name__ == "valkey"
            except ImportError:
                pytest.skip("valkey not installed")

    def test_valkey_url_falls_back_to_redis(self):
        with patch("kombu.transport._valkey_redis_compat._VALKEY_AVAILABLE", False):
            lib = resolve_lib("valkey://localhost")
            assert lib.__name__ == "redis"


class TestResolveAsyncLib:
    """Tests for resolve_async_lib()."""

    def test_returns_asyncio_module(self):
        aiolib = resolve_async_lib("redis://localhost")
        assert hasattr(aiolib, "from_url")


class TestResolveExceptions:
    """Tests for resolve_exceptions()."""

    def test_returns_exceptions_module(self):
        exc = resolve_exceptions("redis://localhost")
        assert hasattr(exc, "ConnectionError")
        assert hasattr(exc, "TimeoutError")


class TestNormalizeUrl:
    """Tests for normalize_url()."""

    def test_redis_url_unchanged_for_redis_lib(self):
        import redis

        url = normalize_url("redis://localhost:6379/0", redis)
        assert url == "redis://localhost:6379/0"

    def test_rediss_url_unchanged_for_redis_lib(self):
        import redis

        url = normalize_url("rediss://localhost:6379/0", redis)
        assert url == "rediss://localhost:6379/0"

    def test_valkey_url_rewritten_for_redis_lib(self):
        import redis

        url = normalize_url("valkey://localhost:6379/0", redis)
        assert url.startswith("redis://")

    def test_valkeys_url_rewritten_for_redis_lib(self):
        import redis

        url = normalize_url("valkeys://localhost:6379/0", redis)
        assert url.startswith("rediss://")


class TestErrorTuples:
    """Tests for get_all_connection_errors() / get_all_channel_errors()."""

    def test_connection_errors_not_empty(self):
        errors = get_all_connection_errors()
        assert len(errors) > 0
        # All should be exception classes
        for exc_cls in errors:
            assert issubclass(exc_cls, Exception)

    def test_channel_errors_not_empty(self):
        errors = get_all_channel_errors()
        assert len(errors) > 0
        for exc_cls in errors:
            assert issubclass(exc_cls, Exception)

    def test_connection_errors_include_redis(self):
        import redis.exceptions

        errors = get_all_connection_errors()
        assert redis.exceptions.ConnectionError in errors

    def test_channel_errors_include_redis(self):
        import redis.exceptions

        errors = get_all_channel_errors()
        assert redis.exceptions.DataError in errors
