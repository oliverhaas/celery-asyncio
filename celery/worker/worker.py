"""WorkController - async worker instance using native asyncio.

The worker consists of several components, all managed by async bootsteps
(mod:`celery.bootsteps`). All lifecycle methods are async.
"""

import asyncio
import os
import sys
from datetime import UTC, datetime

from celery import bootsteps, signals
from celery import concurrency as _concurrency
from celery.bootsteps import RUN, TERMINATE
from celery.exceptions import ImproperlyConfigured, TaskRevokedError, WorkerTerminate
from celery.platforms import EX_FAILURE, create_pidlock
from celery.utils.imports import reload_from_cwd
from celery.utils.log import mlevel
from celery.utils.log import worker_logger as logger
from celery.utils.nodenames import default_nodename, worker_direct
from celery.utils.text import str_to_list

from . import state

try:
    import resource
except ImportError:
    resource = None


def cpu_count() -> int:
    """Return the number of CPUs."""
    return os.cpu_count() or 1


__all__ = ("WorkController",)

#: Default socket timeout at shutdown.
SHUTDOWN_SOCKET_TIMEOUT = 5.0

SELECT_UNKNOWN_QUEUE = """
Trying to select queue subset of {0!r}, but queue {1} isn't
defined in the `task_queues` setting.

If you want to automatically declare unknown queues you can
enable the `task_create_missing_queues` setting.
"""

DESELECT_UNKNOWN_QUEUE = """
Trying to deselect queue subset of {0!r}, but queue {1} isn't
defined in the `task_queues` setting.
"""


class WorkController:
    """Unmanaged worker instance.

    Uses native asyncio for all I/O operations.
    """

    app = None

    pidlock = None
    blueprint = None
    pool = None

    #: contains the exit code if a :exc:`SystemExit` event is handled.
    exitcode = None

    class Blueprint(bootsteps.Blueprint):
        """Worker bootstep blueprint."""

        name = "Worker"
        default_steps = {
            "celery.worker.components:Pool",
            "celery.worker.components:Beat",
            "celery.worker.components:Timer",
            "celery.worker.components:StateDB",
            "celery.worker.components:Consumer",
        }

    def __init__(self, app=None, hostname=None, **kwargs):
        self.app = app or self.app
        self.hostname = default_nodename(hostname)
        self.startup_time = datetime.now(UTC)
        self.app.loader.init_worker()
        self.on_before_init(**kwargs)
        self.setup_defaults(**kwargs)
        self.on_after_init(**kwargs)

        self.setup_instance(**self.prepare_args(**kwargs))

    def setup_instance(
        self, queues=None, ready_callback=None, pidfile=None, include=None, exclude_queues=None, **kwargs
    ):
        self.pidfile = pidfile
        self.setup_queues(queues, exclude_queues)
        self.setup_includes(str_to_list(include))

        # Set default concurrency
        if not self.concurrency:
            try:
                self.concurrency = cpu_count()
            except NotImplementedError:
                self.concurrency = 2

        # Options
        self.loglevel = mlevel(self.loglevel)
        self.ready_callback = ready_callback or self.on_consumer_ready

        # this connection won't establish, only used for params
        self._conninfo = self.app.connection_for_read()
        self.options = kwargs

        signals.worker_init.send(sender=self)

        # Initialize bootsteps
        self.pool_cls = _concurrency.get_implementation(self.pool_cls)
        self.steps = []
        self.on_init_blueprint()
        self.blueprint = self.Blueprint(
            steps=self.app.steps["worker"],
            on_start=self.on_start,
            on_close=self.on_close,
            on_stopped=self.on_stopped,
        )
        self.blueprint.apply(self, **kwargs)

    def on_init_blueprint(self):
        pass

    def on_before_init(self, **kwargs):
        pass

    def on_after_init(self, **kwargs):
        pass

    def on_start(self):
        if self.pidfile:
            self.pidlock = create_pidlock(self.pidfile)

    def on_consumer_ready(self, consumer):
        pass

    def on_close(self):
        self.app.loader.shutdown_worker()

    def on_stopped(self):
        self.timer.clear()
        # Consumer shutdown is handled by the blueprint stop mechanism.
        # Perform any remaining pending operations synchronously.
        if hasattr(self, "consumer") and self.consumer:
            self.consumer.perform_pending_operations()

        if self.pidlock:
            self.pidlock.release()

    def setup_queues(self, include, exclude=None):
        include = str_to_list(include)
        exclude = str_to_list(exclude)
        try:
            self.app.amqp.queues.select(include)
        except KeyError as exc:
            raise ImproperlyConfigured(SELECT_UNKNOWN_QUEUE.strip().format(include, exc))
        try:
            self.app.amqp.queues.deselect(exclude)
        except KeyError as exc:
            raise ImproperlyConfigured(DESELECT_UNKNOWN_QUEUE.strip().format(exclude, exc))
        if self.app.conf.worker_direct:
            self.app.amqp.queues.select_add(worker_direct(self.hostname))

    def setup_includes(self, includes):
        # Update celery_include to have all known task modules
        prev = tuple(self.app.conf.include)
        if includes:
            prev += tuple(includes)
            [self.app.loader.import_task_module(m) for m in includes]
        self.include = includes
        task_modules = {task.__class__.__module__ for task in self.app.tasks.values()}
        self.app.conf.include = tuple(set(prev) | task_modules)

    def prepare_args(self, **kwargs):
        return kwargs

    def _send_worker_shutdown(self):
        signals.worker_shutdown.send(sender=self)

    async def start(self):
        """Start the worker using native asyncio."""
        try:
            await self.blueprint.start(self)
        except WorkerTerminate:
            await self.terminate()
        except Exception as exc:
            logger.critical("Unrecoverable error: %r", exc, exc_info=True)
            await self.stop(exitcode=EX_FAILURE)
        except SystemExit as exc:
            await self.stop(exitcode=exc.code)
        except KeyboardInterrupt:
            await self.stop(exitcode=EX_FAILURE)

    def _process_task(self, req):
        """Process task by sending it to the pool of workers."""
        try:
            req.execute_using_pool(self.pool)
        except TaskRevokedError:
            pass

    async def signal_consumer_close(self):
        try:
            await self.consumer.close()
        except AttributeError:
            pass

    async def stop(self, in_sighandler=False, exitcode=None):
        """Graceful shutdown of the worker server."""
        if exitcode is not None:
            self.exitcode = exitcode
        if self.blueprint.state == RUN:
            await self.signal_consumer_close()
            await self.wait_for_soft_shutdown()
            await self._shutdown(warm=True)
        self._send_worker_shutdown()

    async def terminate(self, in_sighandler=False):
        """Not so graceful shutdown of the worker server."""
        if self.blueprint.state != TERMINATE:
            await self.signal_consumer_close()
            await self._shutdown(warm=False)

    async def _shutdown(self, warm=True):
        if self.blueprint is not None:
            await self.blueprint.stop(self, terminate=not warm)
            await self.blueprint.join()

    def reload(self, modules=None, reload=False, reloader=None):
        list(self._reload_modules(modules, force_reload=reload, reloader=reloader))

        if self.consumer:
            self.consumer.update_strategies()
            self.consumer.reset_rate_limits()
        try:
            self.pool.restart()
        except NotImplementedError:
            pass

    def _reload_modules(self, modules=None, **kwargs):
        return (
            self._maybe_reload_module(m, **kwargs)
            for m in set(self.app.loader.task_modules if modules is None else (modules or ()))
        )

    def _maybe_reload_module(self, module, force_reload=False, reloader=None):
        if module not in sys.modules:
            logger.debug("importing module %s", module)
            return self.app.loader.import_from_cwd(module)
        if force_reload:
            logger.debug("reloading module %s", module)
            return reload_from_cwd(sys.modules[module], reloader)

    def info(self):
        uptime = datetime.now(UTC) - self.startup_time
        return {
            "total": self.state.total_count,
            "pid": os.getpid(),
            "clock": str(self.app.clock),
            "uptime": round(uptime.total_seconds()),
        }

    def rusage(self):
        if resource is None:
            raise NotImplementedError("rusage not supported by this platform")
        s = resource.getrusage(resource.RUSAGE_SELF)
        return {
            "utime": s.ru_utime,
            "stime": s.ru_stime,
            "maxrss": s.ru_maxrss,
            "ixrss": s.ru_ixrss,
            "idrss": s.ru_idrss,
            "isrss": s.ru_isrss,
            "minflt": s.ru_minflt,
            "majflt": s.ru_majflt,
            "nswap": s.ru_nswap,
            "inblock": s.ru_inblock,
            "oublock": s.ru_oublock,
            "msgsnd": s.ru_msgsnd,
            "msgrcv": s.ru_msgrcv,
            "nsignals": s.ru_nsignals,
            "nvcsw": s.ru_nvcsw,
            "nivcsw": s.ru_nivcsw,
        }

    def stats(self):
        info = self.info()
        info.update(self.blueprint.info(self))
        info.update(self.consumer.blueprint.info(self.consumer))
        try:
            info["rusage"] = self.rusage()
        except NotImplementedError:
            info["rusage"] = "N/A"
        return info

    def __repr__(self):
        """``repr(worker)``."""
        return "<Worker: {self.hostname} ({state})>".format(
            self=self,
            state=self.blueprint.human_state() if self.blueprint else "INIT",
        )

    def __str__(self):
        """``str(worker) == worker.hostname``."""
        return self.hostname

    @property
    def state(self):
        return state

    def setup_defaults(
        self,
        concurrency=None,
        loglevel="WARN",
        logfile=None,
        task_events=None,
        pool=None,
        consumer_cls=None,
        timer_cls=None,
        timer_precision=None,
        autoscaler_cls=None,
        pool_putlocks=None,
        pool_restarts=None,
        optimization=None,
        O=None,
        statedb=None,
        time_limit=None,
        soft_time_limit=None,
        scheduler=None,
        pool_cls=None,
        state_db=None,
        task_time_limit=None,
        task_soft_time_limit=None,
        scheduler_cls=None,
        schedule_filename=None,
        max_tasks_per_child=None,
        prefetch_multiplier=None,
        disable_rate_limits=None,
        worker_lost_wait=None,
        max_memory_per_child=None,
        loop_workers=None,
        loop_concurrency=None,
        sync_workers=None,
        **_kw,
    ):
        either = self.app.either
        self.loglevel = loglevel
        self.logfile = logfile

        self.concurrency = either("worker_concurrency", concurrency)
        self.task_events = either("worker_send_task_events", task_events)
        self.pool_cls = either("worker_pool", pool, pool_cls)
        self.consumer_cls = either("worker_consumer", consumer_cls)
        self.timer_cls = either("worker_timer", timer_cls)
        self.timer_precision = either(
            "worker_timer_precision",
            timer_precision,
        )
        self.optimization = optimization or O
        self.autoscaler_cls = either("worker_autoscaler", autoscaler_cls)
        self.pool_putlocks = either("worker_pool_putlocks", pool_putlocks)
        self.pool_restarts = either("worker_pool_restarts", pool_restarts)
        self.statedb = either("worker_state_db", statedb, state_db)
        self.schedule_filename = either(
            "beat_schedule_filename",
            schedule_filename,
        )
        self.scheduler = either("beat_scheduler", scheduler, scheduler_cls)
        self.time_limit = either("task_time_limit", time_limit, task_time_limit)
        self.soft_time_limit = either(
            "task_soft_time_limit",
            soft_time_limit,
            task_soft_time_limit,
        )
        self.max_tasks_per_child = either(
            "worker_max_tasks_per_child",
            max_tasks_per_child,
        )
        self.max_memory_per_child = either(
            "worker_max_memory_per_child",
            max_memory_per_child,
        )
        self.prefetch_multiplier = int(
            either(
                "worker_prefetch_multiplier",
                prefetch_multiplier,
            )
        )
        self.disable_rate_limits = either(
            "worker_disable_rate_limits",
            disable_rate_limits,
        )
        self.worker_lost_wait = either("worker_lost_wait", worker_lost_wait)
        self.loop_workers = either("worker_loop_workers", loop_workers)
        self.loop_concurrency = either("worker_loop_concurrency", loop_concurrency)
        self.sync_workers = either("worker_sync_workers", sync_workers)

    async def wait_for_soft_shutdown(self):
        """Wait for active tasks to finish, up to worker_soft_shutdown_timeout.

        Polls active_requests every 0.5s. If tasks finish early, proceeds
        immediately. After timeout, force-cancels remaining tasks.
        """
        app = self.app
        active = tuple(state.active_requests)
        timeout = app.conf.worker_soft_shutdown_timeout

        if app.conf.worker_enable_soft_shutdown_on_idle:
            active = True

        if timeout > 0 and active:
            active_count = len(tuple(state.active_requests))
            logger.warning(
                "Soft shutdown: waiting up to %s seconds for %d active task(s)",
                timeout,
                active_count,
            )

            import time

            deadline = time.monotonic() + timeout
            while tuple(state.active_requests) and time.monotonic() < deadline:
                await asyncio.sleep(0.5)

            # Force-cancel any remaining tasks
            remaining = tuple(state.active_requests)
            if remaining:
                logger.warning(
                    "Force-cancelling %d remaining task(s) after %ss timeout",
                    len(remaining),
                    timeout,
                )
                for req in remaining:
                    try:
                        req.cancel(self.pool)
                    except Exception as exc:
                        logger.debug("Error cancelling task %s: %r", req.id, exc)
