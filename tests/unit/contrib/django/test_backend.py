"""Tests for celery.contrib.django.backend (Django 6.0 Tasks CeleryBackend)."""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure Django is configured before importing anything Django-related.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "django.conf.global_settings")

import django

django.setup()

from django.tasks.base import Task as DjangoTask
from django.tasks.base import TaskContext, TaskResult, TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist

from celery.contrib.django.backend import CeleryBackend, _build_task_context, _django_task_registry, _map_priority

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_django_task(func, *, priority=0, queue_name="default", backend="default", run_after=None, takes_context=False):
    """Build a Django Task without triggering __post_init__ validation."""
    task = object.__new__(DjangoTask)
    object.__setattr__(task, "priority", priority)
    object.__setattr__(task, "func", func)
    object.__setattr__(task, "backend", backend)
    object.__setattr__(task, "queue_name", queue_name)
    object.__setattr__(task, "run_after", run_after)
    object.__setattr__(task, "takes_context", takes_context)
    return task


def _sample_func(x, y):
    return x + y


async def _async_sample_func(x, y):
    return x + y


def _context_func(context, x):
    return context.attempt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear the module-level Django task registry between tests."""
    _django_task_registry.clear()
    yield
    _django_task_registry.clear()


@pytest.fixture
def celery_app():
    app = MagicMock()
    app.finalized = True
    app._finalize_mutex = MagicMock()
    app._finalize_mutex.__enter__ = MagicMock(return_value=None)
    app._finalize_mutex.__exit__ = MagicMock(return_value=False)
    app.tasks = {}
    return app


@pytest.fixture
def backend(celery_app):
    b = CeleryBackend.__new__(CeleryBackend)
    b.alias = "default"
    b.queues = {"default"}
    b.options = {}
    b._celery_app_path = None
    b._celery_app = celery_app
    return b


@pytest.fixture
def task():
    return _make_django_task(_sample_func)


@pytest.fixture
def async_task():
    return _make_django_task(_async_sample_func)


# ---------------------------------------------------------------------------
# Priority mapping
# ---------------------------------------------------------------------------


class test_map_priority:
    def test_max_django_maps_max_celery(self):
        # Django 100 → Celery 255 (highest priority)
        assert _map_priority(100) == 255

    def test_zero_maps_middle(self):
        # Django 0 → Celery 128 (middle)
        assert _map_priority(0) == 128

    def test_min_django_maps_zero_celery(self):
        # Django -100 → Celery 0 (lowest priority)
        assert _map_priority(-100) == 0

    def test_negative_fifty(self):
        # (−50 + 100) * 255 / 200 = 50 * 1.275 = 63.75 → 64
        assert _map_priority(-50) == 64

    def test_fifty(self):
        # (50 + 100) * 255 / 200 = 150 * 1.275 = 191.25 → 191
        assert _map_priority(50) == 191


# ---------------------------------------------------------------------------
# validate_task / task registration
# ---------------------------------------------------------------------------


class test_validate_task:
    def test_registers_in_django_registry(self, backend, task):
        with patch("celery.contrib.django.backend.CeleryBackend._register_shared"):
            backend.validate_task(task)

        assert task.module_path in _django_task_registry
        assert _django_task_registry[task.module_path] is task

    def test_idempotent(self, backend, task):
        with patch("celery.contrib.django.backend.CeleryBackend._register_shared") as mock_reg:
            backend.validate_task(task)
            backend.validate_task(task)

        mock_reg.assert_called_once()

    def test_calls_register_shared(self, backend, task):
        with patch("celery.contrib.django.backend.CeleryBackend._register_shared") as mock_reg:
            backend.validate_task(task)

        mock_reg.assert_called_once()
        name, run_fn = mock_reg.call_args.args
        assert name == task.module_path

    def test_async_task_registered(self, backend, async_task):
        with patch("celery.contrib.django.backend.CeleryBackend._register_shared"):
            backend.validate_task(async_task)

        assert async_task.module_path in _django_task_registry

    def test_takes_context_wraps_function(self, backend):
        ctx_task = _make_django_task(_context_func, takes_context=True)
        with patch("celery.contrib.django.backend.CeleryBackend._register_shared") as mock_reg:
            backend.validate_task(ctx_task)

        _, run_fn = mock_reg.call_args.args
        # The wrapper should NOT be the original function.
        assert run_fn is not _context_func
        # But it preserves identity metadata.
        assert run_fn.__module__ == _context_func.__module__
        assert run_fn.__qualname__ == _context_func.__qualname__

    def test_no_context_passes_func_directly(self, backend, task):
        with patch("celery.contrib.django.backend.CeleryBackend._register_shared") as mock_reg:
            backend.validate_task(task)

        _, run_fn = mock_reg.call_args.args
        assert run_fn is _sample_func


# ---------------------------------------------------------------------------
# _register_shared
# ---------------------------------------------------------------------------


class test_register_shared:
    def test_connects_on_app_finalize(self):
        with patch("celery._state.connect_on_app_finalize") as mock_connect, patch(
            "celery._state._get_active_apps", return_value=[]
        ):
            CeleryBackend._register_shared("test.task", _sample_func)

        mock_connect.assert_called_once()

    def test_registers_in_finalized_apps(self):
        mock_app = MagicMock()
        mock_app.finalized = True
        mock_app.tasks = {}
        mock_app._finalize_mutex.__enter__ = MagicMock(return_value=None)
        mock_app._finalize_mutex.__exit__ = MagicMock(return_value=False)

        with patch("celery._state.connect_on_app_finalize"), patch(
            "celery._state._get_active_apps", return_value=[mock_app]
        ):
            CeleryBackend._register_shared("test.task", _sample_func)

        mock_app._task_from_fun.assert_called_once_with(
            _sample_func, name="test.task", serializer="json"
        )

    def test_skips_if_already_registered(self):
        mock_app = MagicMock()
        mock_app.finalized = True
        mock_app.tasks = {"test.task": MagicMock()}
        mock_app._finalize_mutex.__enter__ = MagicMock(return_value=None)
        mock_app._finalize_mutex.__exit__ = MagicMock(return_value=False)

        with patch("celery._state.connect_on_app_finalize"), patch(
            "celery._state._get_active_apps", return_value=[mock_app]
        ):
            CeleryBackend._register_shared("test.task", _sample_func)

        mock_app._task_from_fun.assert_not_called()


# ---------------------------------------------------------------------------
# _build_send_options
# ---------------------------------------------------------------------------


class test_build_send_options:
    def test_default_options(self, backend, task):
        opts = backend._build_send_options(task)
        assert opts == {"serializer": "json"}

    def test_custom_queue(self, backend):
        t = _make_django_task(_sample_func, queue_name="high")
        opts = backend._build_send_options(t)
        assert opts["queue"] == "high"

    def test_priority(self, backend):
        t = _make_django_task(_sample_func, priority=50)
        opts = backend._build_send_options(t)
        assert opts["priority"] == _map_priority(50)

    def test_run_after_timedelta(self, backend):
        t = _make_django_task(_sample_func, run_after=timedelta(seconds=30))
        opts = backend._build_send_options(t)
        assert opts["countdown"] == 30.0
        assert "eta" not in opts

    def test_run_after_datetime(self, backend):
        dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        t = _make_django_task(_sample_func, run_after=dt)
        opts = backend._build_send_options(t)
        assert opts["eta"] == dt
        assert "countdown" not in opts

    def test_serializer_always_json(self, backend, task):
        opts = backend._build_send_options(task)
        assert opts["serializer"] == "json"


# ---------------------------------------------------------------------------
# enqueue / aenqueue
# ---------------------------------------------------------------------------


class test_enqueue:
    def test_calls_send_task(self, backend, celery_app, task):
        celery_result = MagicMock()
        celery_result.id = "abc-123"
        celery_app.send_task.return_value = celery_result

        with patch.object(backend, "validate_task"):
            result = backend.enqueue(task, (1, 2), {"z": 3})

        celery_app.send_task.assert_called_once_with(
            task.module_path,
            args=[1, 2],
            kwargs={"z": 3},
            serializer="json",
        )
        assert isinstance(result, TaskResult)
        assert result.id == "abc-123"
        assert result.status == TaskResultStatus.READY
        assert result.task is task

    def test_enqueue_with_options(self, backend, celery_app):
        t = _make_django_task(_sample_func, queue_name="priority", priority=80)
        celery_result = MagicMock()
        celery_result.id = "def-456"
        celery_app.send_task.return_value = celery_result

        with patch.object(backend, "validate_task"):
            backend.enqueue(t, (), {})

        call_kwargs = celery_app.send_task.call_args.kwargs
        assert call_kwargs["queue"] == "priority"
        assert call_kwargs["priority"] == _map_priority(80)

    @pytest.mark.asyncio
    async def test_aenqueue_calls_asend_task(self, backend, celery_app, task):
        celery_result = MagicMock()
        celery_result.id = "ghi-789"
        celery_app.asend_task = AsyncMock(return_value=celery_result)

        with patch.object(backend, "validate_task"):
            result = await backend.aenqueue(task, (10,), {})

        celery_app.asend_task.assert_called_once_with(
            task.module_path,
            args=[10],
            kwargs={},
            serializer="json",
        )
        assert result.id == "ghi-789"
        assert result.status == TaskResultStatus.READY


# ---------------------------------------------------------------------------
# State mapping
# ---------------------------------------------------------------------------


class test_state_mapping:
    @pytest.mark.parametrize(
        "celery_state,expected",
        [
            ("PENDING", TaskResultStatus.READY),
            ("RECEIVED", TaskResultStatus.READY),
            ("STARTED", TaskResultStatus.RUNNING),
            ("SUCCESS", TaskResultStatus.SUCCESSFUL),
            ("FAILURE", TaskResultStatus.FAILED),
            ("REVOKED", TaskResultStatus.FAILED),
            ("REJECTED", TaskResultStatus.FAILED),
            ("RETRY", TaskResultStatus.READY),
            ("IGNORED", TaskResultStatus.FAILED),
            ("UNKNOWN_STATE", TaskResultStatus.READY),
        ],
    )
    def test_state_mapping(self, backend, task, celery_state, expected):
        _django_task_registry[task.module_path] = task
        meta = {"status": celery_state, "name": task.module_path}

        result = backend._meta_to_task_result("task-id-1", meta)
        assert result.status == expected


# ---------------------------------------------------------------------------
# get_result / aget_result
# ---------------------------------------------------------------------------


class test_get_result:
    def test_success(self, backend, celery_app, task):
        _django_task_registry[task.module_path] = task
        celery_app.backend.get_task_meta.return_value = {
            "status": "SUCCESS",
            "result": 42,
            "traceback": None,
            "date_done": datetime(2026, 3, 13, 12, 0, 0, tzinfo=UTC),
            "name": task.module_path,
            "args": [1, 2],
            "kwargs": {},
            "worker": "worker@host",
        }

        result = backend.get_result("task-id-success")
        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.return_value == 42
        assert result.finished_at == datetime(2026, 3, 13, 12, 0, 0, tzinfo=UTC)
        assert result.worker_ids == ["worker@host"]
        assert result.task.func is _sample_func

    def test_failure(self, backend, celery_app, task):
        _django_task_registry[task.module_path] = task
        exc = ValueError("bad value")
        celery_app.backend.get_task_meta.return_value = {
            "status": "FAILURE",
            "result": exc,
            "traceback": "Traceback ...\nValueError: bad value",
            "date_done": None,
            "name": task.module_path,
        }

        result = backend.get_result("task-id-fail")
        assert result.status == TaskResultStatus.FAILED
        assert len(result.errors) == 1
        assert result.errors[0].exception_class_path == "builtins.ValueError"
        assert "bad value" in result.errors[0].traceback

    def test_pending(self, backend, celery_app, task):
        _django_task_registry[task.module_path] = task
        celery_app.backend.get_task_meta.return_value = {
            "status": "PENDING",
            "result": None,
            "name": task.module_path,
        }

        result = backend.get_result("task-id-pending")
        assert result.status == TaskResultStatus.READY

    def test_disabled_backend_raises(self, backend, celery_app):
        from celery.backends.base import DisabledBackend

        celery_app.backend = MagicMock(spec=DisabledBackend)

        with pytest.raises(NotImplementedError, match="result backend is disabled"):
            backend.get_result("any-id")

    def test_unknown_task_raises(self, backend, celery_app):
        celery_app.backend.get_task_meta.return_value = {
            "status": "SUCCESS",
            "result": 1,
        }

        with pytest.raises(TaskResultDoesNotExist, match="Cannot resolve task"):
            backend.get_result("unknown-id")

    @pytest.mark.asyncio
    async def test_aget_result_success(self, backend, celery_app, task):
        _django_task_registry[task.module_path] = task
        celery_app.backend.aget_task_meta = AsyncMock(
            return_value={
                "status": "SUCCESS",
                "result": 99,
                "name": task.module_path,
            }
        )

        result = await backend.aget_result("task-id-async")
        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.return_value == 99


# ---------------------------------------------------------------------------
# supports_get_result property
# ---------------------------------------------------------------------------


class test_supports_get_result:
    def test_true_with_real_backend(self, backend, celery_app):
        celery_app.backend = MagicMock()
        assert backend.supports_get_result is True

    def test_false_with_disabled_backend(self, backend, celery_app):
        from celery.backends.base import DisabledBackend

        celery_app.backend = MagicMock(spec=DisabledBackend)
        assert backend.supports_get_result is False


# ---------------------------------------------------------------------------
# _build_task_context
# ---------------------------------------------------------------------------


class test_build_task_context:
    def test_context_has_correct_attempt(self, task):
        _django_task_registry[task.module_path] = task

        mock_celery_task = MagicMock()
        mock_celery_task.request.id = "ctx-task-id"
        mock_celery_task.request.task = task.module_path
        mock_celery_task.request.retries = 2
        mock_celery_task.request.hostname = "worker@host"
        mock_celery_task.request.args = [1, 2]
        mock_celery_task.request.kwargs = {"z": 3}

        with patch("celery._state.get_current_task", return_value=mock_celery_task):
            ctx = _build_task_context("default")

        assert isinstance(ctx, TaskContext)
        assert ctx.task_result.id == "ctx-task-id"
        assert ctx.attempt == 3  # retries=2 -> attempt = retries + 1

    def test_context_first_attempt(self, task):
        _django_task_registry[task.module_path] = task

        mock_celery_task = MagicMock()
        mock_celery_task.request.id = "first-run"
        mock_celery_task.request.task = task.module_path
        mock_celery_task.request.retries = 0
        mock_celery_task.request.hostname = "w1"
        mock_celery_task.request.args = []
        mock_celery_task.request.kwargs = {}

        with patch("celery._state.get_current_task", return_value=mock_celery_task):
            ctx = _build_task_context("default")

        assert ctx.attempt == 1


# ---------------------------------------------------------------------------
# _build_initial_result
# ---------------------------------------------------------------------------


class test_build_initial_result:
    def test_fields(self, backend, task):
        result = backend._build_initial_result(task, "init-id", (1,), {"k": "v"})
        assert result.id == "init-id"
        assert result.status == TaskResultStatus.READY
        assert result.task is task
        assert result.args == [1]
        assert result.kwargs == {"k": "v"}
        assert result.backend == "default"
        assert result.errors == []
        assert result.worker_ids == []
        assert result.enqueued_at is not None
        assert result.started_at is None
        assert result.finished_at is None
