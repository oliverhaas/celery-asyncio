"""Django 6.0 Tasks backend powered by celery-asyncio.

This module provides a :class:`CeleryBackend` that implements Django's
:class:`~django.tasks.backends.base.BaseTaskBackend` interface, bridging
Django's ``@task`` / ``enqueue()`` API with Celery's message-passing
infrastructure.

Configuration in Django settings::

    TASKS = {
        "default": {
            "BACKEND": "celery.contrib.django.CeleryBackend",
            "QUEUES": ["default"],
            "OPTIONS": {
                # Optional: explicit Celery app import path.
                # If omitted, uses the current default Celery app.
                # "celery_app": "myproject.celery.app",
            },
        }
    }
"""

from datetime import datetime, timedelta
from inspect import iscoroutinefunction

from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import TaskContext, TaskError, TaskResult, TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist
from django.tasks.signals import task_enqueued, task_finished, task_started
from django.utils import timezone
from django.utils.json import normalize_json
from django.utils.module_loading import import_string

# Module-level registry: celery task name -> Django Task object.
# Populated by validate_task() on both sender and worker sides.
_django_task_registry: dict[str, object] = {}

# Celery state -> Django TaskResultStatus
_STATE_MAP = {
    "PENDING": TaskResultStatus.READY,
    "RECEIVED": TaskResultStatus.READY,
    "STARTED": TaskResultStatus.RUNNING,
    "SUCCESS": TaskResultStatus.SUCCESSFUL,
    "FAILURE": TaskResultStatus.FAILED,
    "REVOKED": TaskResultStatus.FAILED,
    "REJECTED": TaskResultStatus.FAILED,
    "RETRY": TaskResultStatus.READY,
    "IGNORED": TaskResultStatus.FAILED,
}


class CeleryBackend(BaseTaskBackend):
    """Django Tasks backend that dispatches work via Celery.

    Supports all four backend feature flags:

    - **defer**: ``run_after`` maps to Celery's ``eta`` / ``countdown``.
    - **async_task**: celery-asyncio natively runs ``async def`` tasks.
    - **get_result**: delegates to the configured Celery result backend.
    - **priority**: maps Django's -100..100 to AMQP/Redis 0..255.
    """

    supports_defer = True
    supports_async_task = True
    supports_priority = True

    def __init__(self, alias, params):
        super().__init__(alias, params)
        self._celery_app_path: str | None = self.options.get("celery_app")
        self._celery_app = None

    # -- Feature flag (dynamic) -------------------------------------------

    @property
    def supports_get_result(self):
        from celery.backends.base import DisabledBackend

        return not isinstance(self._get_celery_app().backend, DisabledBackend)

    # -- Celery app resolution --------------------------------------------

    def _get_celery_app(self):
        if self._celery_app is not None:
            return self._celery_app
        if self._celery_app_path:
            self._celery_app = import_string(self._celery_app_path)
        else:
            from celery._state import get_current_app

            self._celery_app = get_current_app()
        return self._celery_app

    # -- Task registration ------------------------------------------------

    def validate_task(self, task):
        super().validate_task(task)
        self._ensure_celery_task(task)

    def _ensure_celery_task(self, task):
        """Register the Django task function as a Celery shared task.

        Uses the same ``connect_on_app_finalize`` pattern as
        :func:`celery.shared_task` so that the task is available in
        every current and future Celery app.
        """
        celery_name = task.module_path
        if celery_name in _django_task_registry:
            return
        _django_task_registry[celery_name] = task

        run_fn = self._make_run_fn(task)
        self._register_shared(celery_name, run_fn)

    def _make_run_fn(self, task):
        """Build the callable that Celery workers will execute.

        Wraps the function to fire Django's ``task_started`` /
        ``task_finished`` signals and, when ``takes_context`` is True,
        to prepend a :class:`~django.tasks.base.TaskContext` argument.
        """
        func = task.func
        takes_context = task.takes_context
        backend_alias = self.alias

        if iscoroutinefunction(func):

            async def _run(*args, **kwargs):
                task_result = _build_worker_task_result(backend_alias)
                await task_started.asend(sender=CeleryBackend, task_result=task_result)
                try:
                    if takes_context:
                        result = await func(TaskContext(task_result=task_result), *args, **kwargs)
                    else:
                        result = await func(*args, **kwargs)
                except KeyboardInterrupt:
                    raise
                except BaseException as e:
                    _record_failure(task_result, e)
                    await task_finished.asend(sender=CeleryBackend, task_result=task_result)
                    raise
                else:
                    _record_success(task_result, result)
                    await task_finished.asend(sender=CeleryBackend, task_result=task_result)
                    return result
        else:

            def _run(*args, **kwargs):
                task_result = _build_worker_task_result(backend_alias)
                task_started.send(sender=CeleryBackend, task_result=task_result)
                try:
                    if takes_context:
                        result = func(TaskContext(task_result=task_result), *args, **kwargs)
                    else:
                        result = func(*args, **kwargs)
                except KeyboardInterrupt:
                    raise
                except BaseException as e:
                    _record_failure(task_result, e)
                    task_finished.send(sender=CeleryBackend, task_result=task_result)
                    raise
                else:
                    _record_success(task_result, result)
                    task_finished.send(sender=CeleryBackend, task_result=task_result)
                    return result

        _run.__module__ = func.__module__
        _run.__qualname__ = func.__qualname__
        _run.__name__ = func.__name__
        return _run

    @staticmethod
    def _register_shared(celery_name, run_fn):
        from celery import _state

        def _register(app):
            if celery_name in app.tasks:
                return
            app._task_from_fun(run_fn, name=celery_name, serializer="json")

        _state.connect_on_app_finalize(_register)

        for app in _state._get_active_apps():
            if app.finalized:
                with app._finalize_mutex:
                    _register(app)

    # -- Enqueue -----------------------------------------------------------

    def enqueue(self, task, args, kwargs):
        self.validate_task(task)
        app = self._get_celery_app()
        options = self._build_send_options(task)

        celery_result = app.send_task(
            task.module_path,
            args=list(args),
            kwargs=dict(kwargs),
            **options,
        )

        task_result = self._build_initial_result(task, celery_result.id, args, kwargs)
        task_enqueued.send(sender=type(self), task_result=task_result)
        return task_result

    async def aenqueue(self, task, args, kwargs):
        self.validate_task(task)
        app = self._get_celery_app()
        options = self._build_send_options(task)

        celery_result = await app.asend_task(
            task.module_path,
            args=list(args),
            kwargs=dict(kwargs),
            **options,
        )

        task_result = self._build_initial_result(task, celery_result.id, args, kwargs)
        await task_enqueued.asend(sender=type(self), task_result=task_result)
        return task_result

    # -- Result retrieval --------------------------------------------------

    def get_result(self, result_id):
        app = self._get_celery_app()

        from celery.backends.base import DisabledBackend

        if isinstance(app.backend, DisabledBackend):
            raise NotImplementedError(
                "Celery result backend is disabled. "
                "Configure a result backend to use get_result()."
            )

        meta = app.backend.get_task_meta(result_id)
        return self._meta_to_task_result(result_id, meta)

    async def aget_result(self, result_id):
        app = self._get_celery_app()

        from celery.backends.base import DisabledBackend

        if isinstance(app.backend, DisabledBackend):
            raise NotImplementedError(
                "Celery result backend is disabled. "
                "Configure a result backend to use aget_result()."
            )

        if hasattr(app.backend, "aget_task_meta"):
            meta = await app.backend.aget_task_meta(result_id)
        else:
            from asgiref.sync import sync_to_async

            meta = await sync_to_async(app.backend.get_task_meta)(result_id)

        return self._meta_to_task_result(result_id, meta)

    # -- Internal helpers --------------------------------------------------

    def _build_send_options(self, task):
        options = {"serializer": "json"}

        if task.queue_name != "default":
            options["queue"] = task.queue_name

        options["priority"] = _map_priority(task.priority)

        if task.run_after is not None:
            if isinstance(task.run_after, timedelta):
                options["countdown"] = task.run_after.total_seconds()
            elif isinstance(task.run_after, datetime):
                options["eta"] = task.run_after

        return options

    def _build_initial_result(self, task, task_id, args, kwargs):
        return TaskResult(
            task=task,
            id=task_id,
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            started_at=None,
            last_attempted_at=None,
            finished_at=None,
            args=list(args),
            kwargs=dict(kwargs),
            backend=self.alias,
            errors=[],
            worker_ids=[],
        )

    def _meta_to_task_result(self, result_id, meta):
        """Convert a Celery backend meta dict to a Django TaskResult."""
        celery_state = meta.get("status", "PENDING")
        django_status = _STATE_MAP.get(celery_state, TaskResultStatus.READY)

        # Parse finished time.
        finished_at = None
        date_done = meta.get("date_done")
        if date_done is not None:
            if isinstance(date_done, datetime):
                finished_at = date_done
            elif isinstance(date_done, str):
                from django.utils.dateparse import parse_datetime

                finished_at = parse_datetime(date_done)

        # Build errors for failure states.
        errors = []
        if celery_state in ("FAILURE", "REVOKED", "REJECTED"):
            result = meta.get("result")
            tb = meta.get("traceback") or ""
            if isinstance(result, BaseException):
                exc_type = type(result)
                exc_path = f"{exc_type.__module__}.{exc_type.__qualname__}"
            else:
                exc_path = "builtins.Exception"
            errors.append(TaskError(exception_class_path=exc_path, traceback=tb))

        # Resolve the Django Task object.
        django_task = self._resolve_django_task(result_id, meta)

        # Worker info from extended metadata.
        worker_ids = []
        worker = meta.get("worker")
        if worker:
            worker_ids.append(worker)

        task_result = TaskResult(
            task=django_task,
            id=result_id,
            status=django_status,
            enqueued_at=None,
            started_at=None,
            last_attempted_at=None,
            finished_at=finished_at,
            args=meta.get("args") or [],
            kwargs=meta.get("kwargs") or {},
            backend=self.alias,
            errors=errors,
            worker_ids=worker_ids,
        )

        if django_status == TaskResultStatus.SUCCESSFUL:
            object.__setattr__(task_result, "_return_value", meta.get("result"))

        return task_result

    def _resolve_django_task(self, result_id, meta):
        """Look up the Django Task for a Celery result.

        Uses ``result_extended`` metadata if available, otherwise falls
        back to the in-process registry.
        """
        task_name = meta.get("name")

        if task_name and task_name in _django_task_registry:
            return _django_task_registry[task_name]

        # Try to import the function by dotted path and wrap it.
        if task_name:
            try:
                func = import_string(task_name)
                return self.task_class(
                    priority=0,
                    func=func,
                    backend=self.alias,
                    queue_name="default",
                    run_after=None,
                )
            except (ImportError, AttributeError):
                pass

        raise TaskResultDoesNotExist(
            f"Cannot resolve task for result '{result_id}'. "
            "Ensure CELERY_RESULT_EXTENDED=True is set in your settings."
        )


# -- Module-level helpers --------------------------------------------------


def _map_priority(django_priority: int) -> int:
    """Map Django priority (-100..100) to Celery/AMQP/Redis priority (0..255).

    Both use the same convention: higher number = executed sooner.
    Linear mapping: Django -100 → 0, Django 0 → 128, Django 100 → 255.
    """
    # (django_priority + 100) / 200 gives 0.0..1.0, then scale to 0..255
    celery_priority = round((django_priority + 100) * 255 / 200)
    return max(0, min(255, celery_priority))


def _build_worker_task_result(backend_alias: str) -> TaskResult:
    """Build a Django TaskResult on the worker side for signal dispatch."""
    from celery._state import get_current_task

    celery_task = get_current_task()
    request = celery_task.request

    celery_name = request.task or ""
    django_task = _django_task_registry.get(celery_name)

    if django_task is None:
        # Fallback: build a minimal Task from the function.
        try:
            func = import_string(celery_name)
            from django.tasks.base import Task as DjangoTask

            django_task = DjangoTask(
                priority=0,
                func=func,
                backend=backend_alias,
                queue_name="default",
                run_after=None,
            )
        except (ImportError, AttributeError):
            raise RuntimeError(
                f"Cannot resolve Django task '{celery_name}' for TaskContext."
            )

    retries = getattr(request, "retries", 0)
    hostname = getattr(request, "hostname", "unknown") or "unknown"

    now = timezone.now()
    return TaskResult(
        task=django_task,
        id=request.id,
        status=TaskResultStatus.RUNNING,
        enqueued_at=None,
        started_at=now,
        last_attempted_at=now,
        finished_at=None,
        args=list(request.args or []),
        kwargs=dict(request.kwargs or {}),
        backend=backend_alias,
        errors=[],
        worker_ids=[hostname] * (retries + 1),
    )


def _record_success(task_result: TaskResult, result) -> None:
    """Mutate a frozen TaskResult to record a successful outcome."""
    object.__setattr__(task_result, "finished_at", timezone.now())
    object.__setattr__(task_result, "status", TaskResultStatus.SUCCESSFUL)
    object.__setattr__(task_result, "_return_value", normalize_json(result))


def _record_failure(task_result: TaskResult, exc: BaseException) -> None:
    """Mutate a frozen TaskResult to record a failure."""
    object.__setattr__(task_result, "finished_at", timezone.now())
    object.__setattr__(task_result, "status", TaskResultStatus.FAILED)
    exc_type = type(exc)
    from traceback import format_exception

    task_result.errors.append(
        TaskError(
            exception_class_path=f"{exc_type.__module__}.{exc_type.__qualname__}",
            traceback="".join(format_exception(exc)),
        )
    )
