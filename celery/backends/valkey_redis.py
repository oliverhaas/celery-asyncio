"""Valkey/Redis result store backend.

Supports both valkey-py and redis-py client libraries. The URL scheme selects
the preferred library (with automatic fallback if only one is installed):
- ``valkey://`` / ``valkeys://`` → prefer valkey-py, fallback redis-py
- ``redis://`` / ``rediss://`` → prefer redis-py, fallback valkey-py
"""

import asyncio
from functools import partial
from ssl import CERT_NONE, CERT_OPTIONAL, CERT_REQUIRED
from urllib.parse import unquote

from kombu.transport._valkey_redis_compat import (
    get_all_channel_errors,
    get_all_connection_errors,
    resolve_async_lib,
    resolve_lib,
)
from kombu.utils import symbol_by_name

try:
    from redis import CredentialProvider
except ImportError:
    try:
        from valkey import CredentialProvider
    except ImportError:
        CredentialProvider = None

_SSL_CONNECTION_CLASSES: list[type] = []
_URL_QUERY_ARGUMENT_PARSERS: dict = {}

try:
    from redis.connection import URL_QUERY_ARGUMENT_PARSERS as _URL_QUERY_ARGUMENT_PARSERS
    from redis.connection import SSLConnection as _RedisSSLConnection

    _SSL_CONNECTION_CLASSES.append(_RedisSSLConnection)
except ImportError:
    pass

try:
    from valkey.connection import URL_QUERY_ARGUMENT_PARSERS as _vk_URL_QUERY_ARGUMENT_PARSERS
    from valkey.connection import SSLConnection as _ValkeySSLConnection

    _SSL_CONNECTION_CLASSES.append(_ValkeySSLConnection)
    if not _URL_QUERY_ARGUMENT_PARSERS:
        _URL_QUERY_ARGUMENT_PARSERS = _vk_URL_QUERY_ARGUMENT_PARSERS
except ImportError:
    pass

_SSLConnection = tuple(_SSL_CONNECTION_CLASSES) or None
from asgiref.sync import sync_to_async
from kombu.utils.encoding import bytes_to_str
from kombu.utils.functional import retry_over_time
from kombu.utils.objects import cached_property
from kombu.utils.url import _parse_url, maybe_sanitize_url

from celery import states
from celery.backends.base import _create_chord_error_with_cause
from celery.canvas import maybe_signature
from celery.exceptions import BackendStoreError, ChordError, ImproperlyConfigured
from celery.result import GroupResult, allow_join_result
from celery.utils.functional import _regen, dictfilter
from celery.utils.log import get_logger
from celery.utils.time import humanize_seconds

from .asynchronous import AsyncBackendMixin, BaseResultConsumer
from .base import BaseKeyValueStoreBackend

__all__ = ("RedisBackend", "SentinelBackend")

E_REDIS_MISSING = """
You need to install a Valkey/Redis client library in order to use \
the Valkey/Redis result store backend. Install one with: \
pip install valkey  OR  pip install redis
"""

E_REDIS_SENTINEL_MISSING = """
You need to install a Valkey/Redis client library with sentinel support \
in order to use the Sentinel result store backend. Install one with: \
pip install valkey  OR  pip install redis
"""

W_REDIS_SSL_CERT_OPTIONAL = """
Setting ssl_cert_reqs=CERT_OPTIONAL when connecting to Valkey/Redis means \
that celery might not validate the identity of the broker when connecting. \
This leaves you vulnerable to man in the middle attacks.
"""

W_REDIS_SSL_CERT_NONE = """
Setting ssl_cert_reqs=CERT_NONE when connecting to Valkey/Redis means that \
celery will not validate the identity of the broker when connecting. This \
leaves you vulnerable to man in the middle attacks.
"""

E_REDIS_SSL_PARAMS_AND_SCHEME_MISMATCH = """
SSL connection parameters have been provided but the specified URL scheme \
is redis:// or valkey://. An SSL connection URL should use the scheme \
rediss:// or valkeys://.
"""

E_REDIS_SSL_CERT_REQS_MISSING_INVALID = """
A rediss:// or valkeys:// URL must have parameter ssl_cert_reqs and this \
must be set to CERT_REQUIRED, CERT_OPTIONAL, or CERT_NONE
"""

E_LOST = "Connection to Valkey/Redis lost: Retry (%s/%s) %s."

logger = get_logger(__name__)


class ResultConsumer(BaseResultConsumer):
    """Minimal result consumer stub.

    Polling is done directly in AsyncBackendMixin.wait_for_pending()
    and iter_native() — no PUBSUB subscriptions needed.
    """

    def start(self, initial_task_id, **kwargs):
        pass

    def stop(self):
        pass

    def drain_events(self, timeout=None):
        pass

    def consume_from(self, task_id):
        pass

    def cancel_for(self, task_id):
        pass


class RedisBackend(BaseKeyValueStoreBackend, AsyncBackendMixin):
    """Valkey/Redis task result store.

    Supports both valkey-py and redis-py client libraries.

    It makes use of the following commands:
    GET, MGET, DEL, INCRBY, EXPIRE, SET, SETEX
    """

    ResultConsumer = ResultConsumer

    #: Valkey/Redis client module (set per-instance from URL).
    redis = None
    connection_class_ssl = None

    #: Maximum number of connections in the pool.
    max_connections = None

    supports_autoexpire = True
    supports_native_join = True

    #: Maximal length of string value.
    #: 512 MB - https://redis.io/topics/data-types
    _MAX_STR_VALUE_SIZE = 536870912

    def __init__(
        self,
        host=None,
        port=None,
        db=None,
        password=None,
        max_connections=None,
        url=None,
        connection_pool=None,
        **kwargs,
    ):
        super().__init__(expires_type=int, **kwargs)
        _get = self.app.conf.get

        # Resolve client library from URL scheme (unless subclass pre-set it)
        _resolve_url = url or ""
        if self.redis is None:
            try:
                self.redis = resolve_lib(_resolve_url)
            except ImportError:
                raise ImproperlyConfigured(E_REDIS_MISSING.strip())

        if self.connection_class_ssl is None:
            self.connection_class_ssl = getattr(self.redis, "SSLConnection", None)
            if self.connection_class_ssl is None and _SSL_CONNECTION_CLASSES:
                self.connection_class_ssl = _SSL_CONNECTION_CLASSES[0]
        try:
            self._aiolib = resolve_async_lib(_resolve_url)
        except ImportError:
            self._aiolib = None

        if host and "://" in host:
            url, host = host, None

        self.max_connections = max_connections or _get("redis_max_connections") or self.max_connections
        self._ConnectionPool = connection_pool

        socket_timeout = _get("redis_socket_timeout")
        socket_connect_timeout = _get("redis_socket_connect_timeout")
        retry_on_timeout = _get("redis_retry_on_timeout")
        socket_keepalive = _get("redis_socket_keepalive")
        health_check_interval = _get("redis_backend_health_check_interval")
        credential_provider = _get("redis_backend_credential_provider")

        self.connparams = {
            "host": _get("redis_host") or "localhost",
            "port": _get("redis_port") or 6379,
            "db": _get("redis_db") or 0,
            "password": _get("redis_password"),
            "max_connections": self.max_connections,
            "socket_timeout": socket_timeout and float(socket_timeout),
            "retry_on_timeout": retry_on_timeout or False,
            "socket_connect_timeout": socket_connect_timeout and float(socket_connect_timeout),
            "client_name": _get("redis_client_name"),
        }

        username = _get("redis_username")
        if username:
            # We're extra careful to avoid including this configuration value
            # if it wasn't specified since older versions of py-redis
            # don't support specifying a username.
            # Only Redis>6.0 supports username/password authentication.

            # TODO: Include this in connparams' definition once we drop
            #       support for py-redis<3.4.0.
            self.connparams["username"] = username

        if credential_provider:
            # if credential provider passed as string or query param
            if isinstance(credential_provider, str):
                credential_provider_cls = symbol_by_name(credential_provider)
                credential_provider = credential_provider_cls()

            if CredentialProvider is not None and not isinstance(credential_provider, CredentialProvider):
                raise ValueError("Credential provider is not an instance of a CredentialProvider or a subclass")

            self.connparams["credential_provider"] = credential_provider

            # drop username and password if credential provider is configured
            self.connparams.pop("username", None)
            self.connparams.pop("password", None)

        if health_check_interval:
            self.connparams["health_check_interval"] = health_check_interval

        # absent in redis.connection.UnixDomainSocketConnection
        if socket_keepalive:
            self.connparams["socket_keepalive"] = socket_keepalive

        # "redis_backend_use_ssl" must be a dict with the keys:
        # 'ssl_cert_reqs', 'ssl_ca_certs', 'ssl_certfile', 'ssl_keyfile'
        # (the same as "broker_use_ssl")
        ssl = _get("redis_backend_use_ssl")
        if ssl:
            self.connparams.update(ssl)
            self.connparams["connection_class"] = self.connection_class_ssl

        if url:
            self.connparams = self._params_from_url(url, self.connparams)

        # If we've received SSL parameters via query string or the
        # redis_backend_use_ssl dict, check ssl_cert_reqs is valid. If set
        # via query string ssl_cert_reqs will be a string so convert it here
        conn_class = self.connparams.get("connection_class")
        if conn_class is not None and _SSLConnection and issubclass(conn_class, _SSLConnection):
            ssl_cert_reqs_missing = "MISSING"
            ssl_string_to_constant = {
                "CERT_REQUIRED": CERT_REQUIRED,
                "CERT_OPTIONAL": CERT_OPTIONAL,
                "CERT_NONE": CERT_NONE,
                "required": CERT_REQUIRED,
                "optional": CERT_OPTIONAL,
                "none": CERT_NONE,
            }
            ssl_cert_reqs = self.connparams.get("ssl_cert_reqs", ssl_cert_reqs_missing)
            ssl_cert_reqs = ssl_string_to_constant.get(ssl_cert_reqs, ssl_cert_reqs)
            if ssl_cert_reqs not in ssl_string_to_constant.values():
                raise ValueError(E_REDIS_SSL_CERT_REQS_MISSING_INVALID)

            if ssl_cert_reqs == CERT_OPTIONAL:
                logger.warning(W_REDIS_SSL_CERT_OPTIONAL)
            elif ssl_cert_reqs == CERT_NONE:
                logger.warning(W_REDIS_SSL_CERT_NONE)
            self.connparams["ssl_cert_reqs"] = ssl_cert_reqs

        self.url = url

        self.connection_errors = get_all_connection_errors()
        self.channel_errors = get_all_channel_errors()
        self.result_consumer = self.ResultConsumer(
            self,
            self.app,
            self.accept,
            self._pending_results,
            self._pending_messages,
        )

    def _params_from_url(self, url, defaults):
        scheme, host, port, username, password, path, query = _parse_url(url)
        connparams = dict(
            defaults,
            **dictfilter(
                {
                    "host": host,
                    "port": port,
                    "username": username,
                    "password": password,
                    "db": query.pop("virtual_host", None),
                }
            ),
        )

        if scheme == "socket":
            # use 'path' as path to the socket… in this case
            # the database number should be given in 'query'
            connparams.update(
                {
                    "connection_class": self.redis.UnixDomainSocketConnection,
                    "path": "/" + path,
                }
            )
            # host+port are invalid options when using this connection type.
            connparams.pop("host", None)
            connparams.pop("port", None)
            connparams.pop("socket_connect_timeout")
        else:
            connparams["db"] = path

        ssl_param_keys = ["ssl_ca_certs", "ssl_certfile", "ssl_keyfile", "ssl_cert_reqs"]

        if scheme in ("redis", "valkey"):
            # If connparams or query string contain ssl params, raise error
            if any(key in connparams for key in ssl_param_keys) or any(key in query for key in ssl_param_keys):
                raise ValueError(E_REDIS_SSL_PARAMS_AND_SCHEME_MISMATCH)

        if scheme in ("rediss", "valkeys"):
            connparams["connection_class"] = self.connection_class_ssl
            # The following parameters, if present in the URL, are encoded. We
            # must add the decoded values to connparams.
            for ssl_setting in ssl_param_keys:
                ssl_val = query.pop(ssl_setting, None)
                if ssl_val:
                    connparams[ssl_setting] = unquote(ssl_val)

        # db may be string and start with / like in kombu.
        db = connparams.get("db") or 0
        db = db.strip("/") if isinstance(db, str) else db
        connparams["db"] = int(db)

        # credential provider as query string
        credential_provider = query.pop("credential_provider", None)
        if credential_provider:
            if isinstance(credential_provider, str):
                credential_provider_cls = symbol_by_name(credential_provider)
                credential_provider = credential_provider_cls()

            if CredentialProvider is not None and not isinstance(credential_provider, CredentialProvider):
                raise ValueError("Credential provider is not an instance of a CredentialProvider or a subclass")

            connparams["credential_provider"] = credential_provider
            # drop username and password if credential provider is configured
            connparams.pop("username", None)
            connparams.pop("password", None)

        for key, value in query.items():
            if key in _URL_QUERY_ARGUMENT_PARSERS:
                query[key] = _URL_QUERY_ARGUMENT_PARSERS[key](value)

        # Query parameters override other parameters
        connparams.update(query)
        return connparams

    def exception_safe_to_retry(self, exc):
        if isinstance(exc, self.connection_errors):
            return True
        return False

    @cached_property
    def retry_policy(self):
        retry_policy = super().retry_policy
        if "retry_policy" in self._transport_options:
            retry_policy = retry_policy.copy()
            retry_policy.update(self._transport_options["retry_policy"])

        return retry_policy

    def on_task_call(self, producer, task_id):
        pass  # Polling-based — no early subscription needed

    def get(self, key):
        return self.client.get(key)

    def mget(self, keys):
        return self.client.mget(keys)

    def ensure(self, fun, args, **policy):
        retry_policy = dict(self.retry_policy, **policy)
        max_retries = retry_policy.get("max_retries")
        return retry_over_time(
            fun, self.connection_errors, args, {}, partial(self.on_connection_error, max_retries), **retry_policy
        )

    def on_connection_error(self, max_retries, exc, intervals, retries):
        tts = next(intervals)
        logger.error(E_LOST.strip(), retries, max_retries or "Inf", humanize_seconds(tts, "in "))
        return tts

    def set(self, key, value, **retry_policy):
        if isinstance(value, str) and len(value) > self._MAX_STR_VALUE_SIZE:
            raise BackendStoreError("value too large for Redis backend")

        return self.ensure(self._set, (key, value), **retry_policy)

    def _set(self, key, value):
        if self.expires:
            self.client.setex(key, self.expires, value)
        else:
            self.client.set(key, value)

    def forget(self, task_id):
        super().forget(task_id)

    def delete(self, key):
        self.client.delete(key)

    def incr(self, key):
        return self.client.incr(key)

    def expire(self, key, value):
        return self.client.expire(key, value)

    def add_to_chord(self, group_id, result):
        self.client.incr(self.get_key_for_group(group_id, ".t"), 1)

    def _unpack_chord_result(
        self, tup, decode, EXCEPTION_STATES=states.EXCEPTION_STATES, PROPAGATE_STATES=states.PROPAGATE_STATES
    ):
        _, tid, state, retval = decode(tup)
        if state in EXCEPTION_STATES:
            retval = self.exception_to_python(retval)
        if state in PROPAGATE_STATES:
            chord_error = _create_chord_error_with_cause(
                message=f"Dependency {tid} raised {retval!r}", original_exc=retval
            )
            raise chord_error
        return retval

    def set_chord_size(self, group_id, chord_size):
        self.set(self.get_key_for_group(group_id, ".s"), chord_size)

    def apply_chord(self, header_result_args, body, **kwargs):
        # If any of the child results of this chord are complex (ie. group
        # results themselves), we need to save `header_result` to ensure that
        # the expected structure is retained when we finish the chord and pass
        # the results onward to the body in `on_chord_part_return()`. We don't
        # do this is all cases to retain an optimisation in the common case
        # where a chord header is comprised of simple result objects.
        if not isinstance(header_result_args[1], _regen):
            header_result = self.app.GroupResult(*header_result_args)
            if any(isinstance(nr, GroupResult) for nr in header_result.results):
                header_result.save(backend=self)

    @cached_property
    def _chord_zset(self):
        return self._transport_options.get("result_chord_ordered", True)

    @cached_property
    def _transport_options(self):
        return self.app.conf.get("result_backend_transport_options", {})

    def on_chord_part_return(self, request, state, result, propagate=None, **kwargs):
        app = self.app
        tid, gid, group_index = request.id, request.group, request.group_index
        if not gid or not tid:
            return None
        if group_index is None:
            group_index = "+inf"

        client = self.client
        jkey = self.get_key_for_group(gid, ".j")
        tkey = self.get_key_for_group(gid, ".t")
        skey = self.get_key_for_group(gid, ".s")
        result = self.encode_result(result, state)
        encoded = self.encode([1, tid, state, result])
        with client.pipeline() as pipe:
            pipeline = (
                (
                    pipe.zadd(jkey, {encoded: group_index}).zcount(jkey, "-inf", "+inf")
                    if self._chord_zset
                    else pipe.rpush(jkey, encoded).llen(jkey)
                )
                .get(tkey)
                .get(skey)
            )
            if self.expires:
                pipeline = pipeline.expire(jkey, self.expires).expire(tkey, self.expires).expire(skey, self.expires)

            _, readycount, totaldiff, chord_size_bytes = pipeline.execute()[:4]

        totaldiff = int(totaldiff or 0)

        if chord_size_bytes:
            try:
                callback = maybe_signature(request.chord, app=app)
                total = int(chord_size_bytes) + totaldiff
                if readycount == total:
                    header_result = GroupResult.restore(gid, app=app)
                    if header_result is not None:
                        # If we manage to restore a `GroupResult`, then it must
                        # have been complex and saved by `apply_chord()` earlier.
                        #
                        # Before we can join the `GroupResult`, it needs to be
                        # manually marked as ready to avoid blocking
                        header_result.on_ready()
                        # We'll `join()` it to get the results and ensure they are
                        # structured as intended rather than the flattened version
                        # we'd construct without any other information.
                        join_func = (
                            header_result.join_native if header_result.supports_native_join else header_result.join
                        )
                        with allow_join_result():
                            resl = join_func(timeout=app.conf.result_chord_join_timeout, propagate=True)
                    else:
                        # Otherwise simply extract and decode the results we
                        # stashed along the way, which should be faster for large
                        # numbers of simple results in the chord header.
                        decode, unpack = self.decode, self._unpack_chord_result
                        with client.pipeline() as pipe:
                            if self._chord_zset:
                                pipeline = pipe.zrange(jkey, 0, -1)
                            else:
                                pipeline = pipe.lrange(jkey, 0, total)
                            (resl,) = pipeline.execute()
                        resl = [unpack(tup, decode) for tup in resl]
                    try:
                        callback.delay(resl)
                    except Exception as exc:
                        logger.exception("Chord callback for %r raised: %r", request.group, exc)
                        return self.chord_error_from_stack(
                            callback,
                            ChordError(f"Callback error: {exc!r}"),
                        )
                    finally:
                        with client.pipeline() as pipe:
                            pipe.delete(jkey).delete(tkey).delete(skey).execute()
            except ChordError as exc:
                logger.exception("Chord %r raised: %r", request.group, exc)
                return self.chord_error_from_stack(callback, exc)
            except Exception as exc:
                logger.exception("Chord %r raised: %r", request.group, exc)
                return self.chord_error_from_stack(
                    callback,
                    ChordError(f"Join error: {exc!r}"),
                )

    async def aon_chord_part_return(self, request, state, result, propagate=None, **kwargs):
        """Async version of on_chord_part_return using async Redis client."""
        app = self.app
        tid, gid, group_index = request.id, request.group, request.group_index
        if not gid or not tid:
            return None
        if group_index is None:
            group_index = "+inf"

        client = self.async_client
        jkey = self.get_key_for_group(gid, ".j")
        tkey = self.get_key_for_group(gid, ".t")
        skey = self.get_key_for_group(gid, ".s")
        result = self.encode_result(result, state)
        encoded = self.encode([1, tid, state, result])

        # Atomic pipeline: store result + get counts
        async with client.pipeline() as pipe:
            if self._chord_zset:
                pipe.zadd(jkey, {encoded: group_index})
                pipe.zcount(jkey, "-inf", "+inf")
            else:
                pipe.rpush(jkey, encoded)
                pipe.llen(jkey)
            pipe.get(tkey)
            pipe.get(skey)
            if self.expires:
                pipe.expire(jkey, self.expires)
                pipe.expire(tkey, self.expires)
                pipe.expire(skey, self.expires)
            pipe_results = await pipe.execute()

        _, readycount, totaldiff, chord_size_bytes = pipe_results[:4]
        totaldiff = int(totaldiff or 0)

        if chord_size_bytes:
            try:
                callback = maybe_signature(request.chord, app=app)
                total = int(chord_size_bytes) + totaldiff
                if readycount == total:
                    # Try to restore a saved GroupResult (only exists for
                    # complex nested headers saved by apply_chord()).
                    header_result = await self.arestore_group(gid)
                    if header_result is not None:
                        header_result.on_ready()
                        join_func = (
                            header_result.join_native if header_result.supports_native_join else header_result.join
                        )

                        # join() is sync — offload to thread to avoid blocking the loop.
                        def _join_in_thread():
                            with allow_join_result():
                                return join_func(timeout=app.conf.result_chord_join_timeout, propagate=True)

                        resl = await sync_to_async(_join_in_thread, thread_sensitive=False)()
                    else:
                        # Common fast path: extract results directly from Redis.
                        decode, unpack = self.decode, self._unpack_chord_result
                        async with client.pipeline() as pipe:
                            if self._chord_zset:
                                pipe.zrange(jkey, 0, -1)
                            else:
                                pipe.lrange(jkey, 0, total)
                            (resl,) = await pipe.execute()
                        resl = [unpack(tup, decode) for tup in resl]
                    try:
                        await callback.adelay(resl)
                    except Exception as exc:
                        logger.exception("Chord callback for %r raised: %r", request.group, exc)
                        return self.chord_error_from_stack(
                            callback,
                            ChordError(f"Callback error: {exc!r}"),
                        )
                    finally:
                        async with client.pipeline() as pipe:
                            pipe.delete(jkey)
                            pipe.delete(tkey)
                            pipe.delete(skey)
                            await pipe.execute()
            except ChordError as exc:
                logger.exception("Chord %r raised: %r", request.group, exc)
                return self.chord_error_from_stack(callback, exc)
            except Exception as exc:
                logger.exception("Chord %r raised: %r", request.group, exc)
                return self.chord_error_from_stack(
                    callback,
                    ChordError(f"Join error: {exc!r}"),
                )

    async def aset_chord_size(self, group_id, chord_size):
        """Async version of set_chord_size."""
        await self.aset(self.get_key_for_group(group_id, ".s"), chord_size)

    def _create_client(self, **params):
        return self._get_client()(
            connection_pool=self._get_pool(**params),
        )

    def _get_client(self):
        return self.redis.StrictRedis

    def _get_pool(self, **params):
        return self.ConnectionPool(**params)

    @property
    def ConnectionPool(self):
        if self._ConnectionPool is None:
            self._ConnectionPool = self.redis.ConnectionPool
        return self._ConnectionPool

    @cached_property
    def client(self):
        return self._create_client(**self.connparams)

    # --- Async client and methods ---

    def _get_async_client_class(self):
        return self._aiolib.Redis

    def _get_async_connection_pool_class(self):
        return self._aiolib.ConnectionPool

    def _create_async_client(self, **params):
        # Filter out params not supported by async client
        async_params = params.copy()
        # The async client uses the same connection pool parameters
        pool = self._get_async_connection_pool_class()(**async_params)
        return self._get_async_client_class()(connection_pool=pool)

    @cached_property
    def async_client(self):
        """Return an async Redis client for native asyncio operations."""
        return self._create_async_client(**self.connparams)

    async def aget(self, key):
        """Async version of get."""
        return await self.async_client.get(key)

    async def amget(self, keys):
        """Async version of mget."""
        return await self.async_client.mget(keys)

    async def aset(self, key, value, **retry_policy):
        """Async version of set."""
        if isinstance(value, str) and len(value) > self._MAX_STR_VALUE_SIZE:
            raise BackendStoreError("value too large for Redis backend")
        # For async, we don't use retry_over_time - caller should handle retries
        await self._aset(key, value)

    async def _aset(self, key, value):
        """Async version of _set."""
        if self.expires:
            await self.async_client.setex(key, self.expires, value)
        else:
            await self.async_client.set(key, value)

    async def adelete(self, key):
        """Async version of delete."""
        await self.async_client.delete(key)

    async def aincr(self, key):
        """Async version of incr."""
        return await self.async_client.incr(key)

    async def aexpire(self, key, value):
        """Async version of expire."""
        return await self.async_client.expire(key, value)

    async def aget_task_meta(self, task_id, cache=True):
        """Async version of get_task_meta."""
        self._ensure_not_eager()
        if cache:
            try:
                return self._cache[task_id]
            except KeyError:
                pass
        meta = await self._aget_task_meta_for(task_id)
        if cache and meta.get("status") == states.SUCCESS:
            self._cache[task_id] = meta
        return meta

    async def _aget_task_meta_for(self, task_id):
        """Async version of _get_task_meta_for."""
        meta = await self.aget(self.get_key_for_task(task_id))
        if not meta:
            return {"status": states.PENDING, "result": None}
        return self.decode_result(meta)

    async def await_for_pending(
        self,
        result,
        timeout=None,
        interval=0.5,
        no_ack=True,
        on_message=None,
        on_interval=None,
        callback=None,
        propagate=True,
    ):
        """Async version of wait_for_pending - wait for task result."""
        self._ensure_not_eager()
        meta = await self.await_for(
            result.id,
            timeout=timeout,
            interval=interval,
            on_interval=on_interval,
            no_ack=no_ack,
        )
        if meta:
            result._maybe_set_cache(meta)
            return result.maybe_throw(propagate=propagate, callback=callback)

    async def await_for(self, task_id, timeout=None, interval=0.5, no_ack=True, on_interval=None):
        """Async version of wait_for - poll for task result."""
        self._ensure_not_eager()
        time_elapsed = 0.0

        while True:
            meta = await self.aget_task_meta(task_id)
            if meta["status"] in states.READY_STATES:
                return meta
            if on_interval:
                on_interval()
            await asyncio.sleep(interval)
            time_elapsed += interval
            if timeout and time_elapsed >= timeout:
                from celery.exceptions import TimeoutError

                raise TimeoutError("The operation timed out.")

    async def aforget(self, task_id):
        """Async version of forget."""
        self._cache.pop(task_id, None)
        key = self.get_key_for_task(task_id)
        await self.adelete(key)

    async def asave_group(self, group_id, result):
        """Async version of save_group."""
        return await self.aset(
            self.get_key_for_group(group_id),
            self.encode_group(result),
        )

    async def adelete_group(self, group_id):
        """Async version of delete_group."""
        await self.adelete(self.get_key_for_group(group_id))

    async def arestore_group(self, group_id, cache=True):
        """Async version of restore_group."""
        if cache:
            try:
                return self._cache[group_id]
            except KeyError:
                pass
        meta = await self.aget(self.get_key_for_group(group_id))
        if meta:
            meta = self.decode_group(meta)
            if cache:
                self._cache[group_id] = meta
            return meta

    async def aget_many(
        self,
        task_ids,
        timeout=None,
        interval=0.5,
        no_ack=True,
        on_message=None,
        on_interval=None,
        max_iterations=None,
        READY_STATES=states.READY_STATES,
    ):
        """Async version of get_many - fetch multiple task results."""
        interval = 0.5 if interval is None else interval
        ids = task_ids if isinstance(task_ids, set) else set(task_ids)
        cached_ids = set()
        cache = self._cache
        results = []

        # First yield cached results
        for task_id in ids:
            try:
                cached = cache[task_id]
            except KeyError:
                pass
            else:
                if cached["status"] in READY_STATES:
                    results.append((bytes_to_str(task_id), cached))
                    cached_ids.add(task_id)

        ids.difference_update(cached_ids)
        iterations = 0

        while ids:
            keys = list(ids)
            # Use async mget
            values = await self.amget([self.get_key_for_task(k) for k in keys])
            r = self._mget_to_results(values, keys, READY_STATES)
            cache.update(r)
            ids.difference_update({bytes_to_str(v) for v in r})
            for key, value in r.items():
                if on_message is not None:
                    on_message(value)
                results.append((bytes_to_str(key), value))
            if timeout and iterations * interval >= timeout:
                from celery.exceptions import TimeoutError

                raise TimeoutError(f"Operation timed out ({timeout})")
            if on_interval:
                on_interval()
            await asyncio.sleep(interval)
            iterations += 1
            if max_iterations and iterations >= max_iterations:
                break

        return results

    async def aiter_native(self, result, timeout=None, interval=0.5, no_ack=True, on_message=None, on_interval=None):
        """Async version of iter_native - iterate over task results."""
        self._ensure_not_eager()
        results = result.results
        if not results:
            return []

        all_results = []
        task_ids = set()
        for res in results:
            # Check if it's a ResultSet (has .results attribute)
            if hasattr(res, "results") and hasattr(res, "id"):
                all_results.append((res.id, res.results))
            else:
                task_ids.add(res.id)

        if task_ids:
            many_results = await self.aget_many(
                task_ids,
                timeout=timeout,
                interval=interval,
                no_ack=no_ack,
                on_message=on_message,
                on_interval=on_interval,
            )
            all_results.extend(many_results)

        return all_results

    async def _aset_with_state(self, key, value, state):
        """Async version of _set_with_state using native async Redis client."""
        return await self._aset(key, value)

    def __reduce__(self, args=(), kwargs=None):
        kwargs = {} if not kwargs else kwargs
        return super().__reduce__(args, dict(kwargs, expires=self.expires, url=self.url))


try:
    import redis.sentinel

    class SentinelManagedSSLConnection(redis.sentinel.SentinelManagedConnection, redis.SSLConnection):
        """Connect to a Valkey/Redis server using Sentinel + TLS."""

except ImportError, AttributeError:
    try:
        import valkey.sentinel

        class SentinelManagedSSLConnection(valkey.sentinel.SentinelManagedConnection, valkey.SSLConnection):
            """Connect to a Valkey/Redis server using Sentinel + TLS."""

    except ImportError, AttributeError:
        SentinelManagedSSLConnection = None


class SentinelBackend(RedisBackend):
    """Valkey/Redis sentinel task result store."""

    # URL looks like `sentinel://0.0.0.0:26347/3;sentinel://0.0.0.0:26348/3`
    _SERVER_URI_SEPARATOR = ";"

    sentinel = None
    connection_class_ssl = SentinelManagedSSLConnection

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Resolve sentinel module — check class attribute first (tests), then library
        if self.sentinel is None:
            self.sentinel = getattr(self.redis, "sentinel", None)
        if self.sentinel is None:
            raise ImproperlyConfigured(E_REDIS_SENTINEL_MISSING.strip())

    def as_uri(self, include_password=False):
        """Return the server addresses as URIs, sanitizing the password or not."""
        # Allow superclass to do work if we don't need to force sanitization
        if include_password:
            return super().as_uri(
                include_password=include_password,
            )
        # Otherwise we need to ensure that all components get sanitized rather
        # by passing them one by one to the `kombu` helper
        uri_chunks = (maybe_sanitize_url(chunk) for chunk in (self.url or "").split(self._SERVER_URI_SEPARATOR))
        # Similar to the superclass, strip the trailing slash from URIs with
        # all components empty other than the scheme
        return self._SERVER_URI_SEPARATOR.join(uri[:-1] if uri.endswith(":///") else uri for uri in uri_chunks)

    def _params_from_url(self, url, defaults):
        chunks = url.split(self._SERVER_URI_SEPARATOR)
        connparams = dict(defaults, hosts=[])
        for chunk in chunks:
            data = super()._params_from_url(url=chunk, defaults=defaults)
            connparams["hosts"].append(data)
        for param in ("host", "port", "db", "password"):
            connparams.pop(param)

        # Adding db/password in connparams to connect to the correct instance
        for param in ("db", "password"):
            if connparams["hosts"] and param in connparams["hosts"][0]:
                connparams[param] = connparams["hosts"][0].get(param)
        return connparams

    def _get_sentinel_instance(self, **params):
        connparams = params.copy()

        hosts = connparams.pop("hosts")
        min_other_sentinels = self._transport_options.get("min_other_sentinels", 0)
        sentinel_kwargs = self._transport_options.get("sentinel_kwargs", {})

        sentinel_instance = self.sentinel.Sentinel(
            [(cp["host"], cp["port"]) for cp in hosts],
            min_other_sentinels=min_other_sentinels,
            sentinel_kwargs=sentinel_kwargs,
            **connparams,
        )

        return sentinel_instance

    def _get_pool(self, **params):
        sentinel_instance = self._get_sentinel_instance(**params)

        master_name = self._transport_options.get("master_name", None)

        return sentinel_instance.master_for(
            service_name=master_name,
            redis_class=self._get_client(),
        ).connection_pool
