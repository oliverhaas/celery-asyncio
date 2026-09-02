"""Valkey/Redis client library compatibility layer.

Supports both valkey-py and redis-py with identical APIs.
URL scheme selects the preferred library with fallback:
  - valkey:// / valkeys:// → prefer valkey-py, fallback redis-py
  - redis:// / rediss:// → prefer redis-py, fallback valkey-py
  - No scheme / unknown → prefer valkey-py (OSI-licensed)
"""

import types
from typing import TYPE_CHECKING
from urllib.parse import urlparse

_VALKEY_AVAILABLE = False
_REDIS_AVAILABLE = False

# The _AVAILABLE flags are the runtime gate; type checkers see the unguarded
# imports so the module attributes below resolve without a None in every union.
if TYPE_CHECKING:
    import redis
    import valkey

    _VALKEY_AVAILABLE = True
    _REDIS_AVAILABLE = True
else:
    try:
        import valkey

        _VALKEY_AVAILABLE = True
    except ImportError:
        valkey = None

    try:
        import redis

        _REDIS_AVAILABLE = True
    except ImportError:
        redis = None


def resolve_lib(url: str = "") -> types.ModuleType:
    """Return the valkey or redis module based on URL scheme + availability.

    Raises ImportError if neither library is installed.
    """
    scheme = urlparse(url).scheme.lower() if url else ""

    if scheme in ("valkey", "valkeys"):
        if _VALKEY_AVAILABLE:
            return valkey
        if _REDIS_AVAILABLE:
            return redis
    elif scheme in ("redis", "rediss"):
        if _REDIS_AVAILABLE:
            return redis
        if _VALKEY_AVAILABLE:
            return valkey
    else:
        # No scheme or unknown → prefer valkey (OSI-licensed)
        if _VALKEY_AVAILABLE:
            return valkey
        if _REDIS_AVAILABLE:
            return redis

    raise ImportError(
        "A Valkey/Redis client library is required. Install one with: pip install valkey  OR  pip install redis",
    )


def resolve_async_lib(url: str = "") -> types.ModuleType:
    """Return the async sub-module (valkey.asyncio or redis.asyncio)."""
    lib = resolve_lib(url)
    return lib.asyncio


def normalize_url(url: str, lib: types.ModuleType) -> str:
    """Rewrite valkey:// → redis:// when using redis-py (and vice versa).

    Each library only accepts its own URL scheme.
    """
    lib_name = lib.__name__  # "redis" or "valkey"
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if lib_name == "redis" and scheme in ("valkey", "valkeys"):
        new_scheme = "rediss" if scheme == "valkeys" else "redis"
        return parsed._replace(scheme=new_scheme).geturl()
    if lib_name == "valkey" and scheme in ("redis", "rediss"):
        new_scheme = "valkeys" if scheme == "rediss" else "valkey"
        return parsed._replace(scheme=new_scheme).geturl()
    return url


def get_all_connection_errors() -> tuple[type[Exception], ...]:
    """Return connection error classes from ALL installed libraries."""
    errors: list[type[Exception]] = []
    if _REDIS_AVAILABLE:
        errors.extend(
            [
                redis.exceptions.ConnectionError,
                redis.exceptions.BusyLoadingError,
                redis.exceptions.TimeoutError,
            ],
        )
    if _VALKEY_AVAILABLE:
        errors.extend(
            [
                valkey.exceptions.ConnectionError,
                valkey.exceptions.BusyLoadingError,
                valkey.exceptions.TimeoutError,
            ],
        )
    return tuple(errors)


def get_all_channel_errors() -> tuple[type[Exception], ...]:
    """Return channel error classes from ALL installed libraries."""
    errors: list[type[Exception]] = []
    if _REDIS_AVAILABLE:
        errors.extend(
            [
                redis.exceptions.DataError,
                redis.exceptions.ResponseError,
            ],
        )
    if _VALKEY_AVAILABLE:
        errors.extend(
            [
                valkey.exceptions.DataError,
                valkey.exceptions.ResponseError,
            ],
        )
    return tuple(errors)
