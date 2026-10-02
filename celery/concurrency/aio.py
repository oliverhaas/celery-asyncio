"""Multi-loop asyncio + thread pool for celery-asyncio.

Architecture:
  - N "loop worker" threads, each running its own asyncio event loop
    with a Semaphore(P) limiting concurrent async tasks per loop.
  - M "sync worker" threads via ThreadPoolExecutor for sync tasks.
  - Main thread owns the broker connection and dispatches tasks.

With Python 3.14t free-threading, all threads run with true parallelism.

This is the default pool for celery-asyncio workers.
"""

import asyncio
import ctypes
import inspect
import os
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from threading import Lock
from typing import Any

from celery import signals
from celery.utils.log import get_logger

from .base import BasePool, apply_target

__all__ = ("TaskPool",)

logger = get_logger("celery.pool")


class ApplyResult:
    """Handle for a sync task running in the thread pool."""

    def __init__(self, future: Future) -> None:
        self.f = future

    def get(self, timeout: float | None = None) -> Any:
        return self.f.result(timeout)

    def wait(self, timeout: float | None = None) -> None:
        wait_for_futures([self.f], timeout)

    def cancel(self) -> None:
        self.f.cancel()

    def terminate(self, signal: Any = None) -> None:
        self.cancel()


class AsyncApplyResult:
    """Handle for an async task dispatched to a loop worker.

    ``terminate()`` may be called from any thread and cancels the asyncio
    task on the loop that runs it, including before that task exists: a job
    terminated between dispatch and scheduling is cancelled as soon as it is
    attached. ``discard()`` does the same until the job starts, and nothing
    after. Neither touches a job whose body is done.
    """

    def __init__(self, worker: LoopWorker, job_id: str, on_done: Callable[[AsyncApplyResult], None]) -> None:
        self.id = job_id
        self._worker = worker
        self._on_done = on_done
        self._mutex = Lock()
        self._task: asyncio.Task | None = None
        self._terminated = False
        self._started = False
        self._discarded = False
        #: Set on the loop thread after the task body has returned or raised.
        self.past_body = False

    def attach(self, task: asyncio.Task) -> None:
        """Bind the asyncio task running this job (called on the loop thread).

        The done callback is what keeps the handle alive: the request holds
        only a weak reference to it, and it has to stay resolvable for as
        long as the job can still be cancelled.
        """
        with self._mutex:
            self._task = task
            cancelled = self._terminated or self._discarded
        task.add_done_callback(self._release)
        if cancelled:
            task.cancel()

    def _release(self, task: asyncio.Task) -> None:
        with self._mutex:
            self._task = None
        self._on_done(self)

    def start(self) -> bool:
        """Claim the job for running, unless it was discarded first.

        Called on the loop thread once the job has a slot. Under the mutex, so
        a discard either wins and the job never runs, or finds it started.
        """
        with self._mutex:
            if self._discarded:
                return False
            self._started = True
            return True

    def discard(self) -> None:
        """Drop the job, from any thread, unless it has started."""
        with self._mutex:
            if self._started or self._discarded:
                return
            self._discarded = True
            task = self._task
        if task is not None:
            self._worker.cancel_task(task)

    def body_done(self) -> None:
        # Under the mutex, so a terminate either lands before this or sees it.
        with self._mutex:
            self.past_body = True

    def cancel(self) -> None:
        self.terminate()

    def terminate(self, signal: Any = None) -> bool:
        """Cancel the job, returning False if its body is already done.

        Such a job is only reporting an outcome that is already decided, so
        it is left to report it, as at shutdown; see LoopWorker.cancel_all().
        """
        with self._mutex:
            if self.past_body:
                return False
            if self._terminated:
                return True
            self._terminated = True
            task = self._task
        if task is not None:
            self._worker.cancel_task(task)
        return True


class LoopWorker:
    """A worker thread running its own asyncio event loop.

    Each LoopWorker runs an independent event loop on a daemon thread.
    A semaphore limits the number of concurrent async tasks to
    ``concurrency``.
    """

    def __init__(self, concurrency: int, app: Any, index: int) -> None:
        self._concurrency = concurrency
        self._app = app
        self._index = index
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._semaphore = asyncio.Semaphore(concurrency)
        self._active_count = 0
        self._active_count_lock = Lock()
        self._ready = threading.Event()
        self._tasks: dict[asyncio.Task, AsyncApplyResult | None] = {}

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"celery-loop-worker-{self._index}",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._app.set_current()
        # Signalled from inside the loop so that start() returns once
        # run_forever() is spinning, not merely once the loop object exists.
        self._loop.call_soon(self._ready.set)
        exitcode = 1
        try:
            signals.worker_process_init.send(sender=None)
            self._loop.run_forever()
            exitcode = 0
        finally:
            # Only the owning thread can close the loop, and an unclosed one
            # leaks its self-pipe until __del__ raises from the GC.
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            finally:
                self._loop.close()
                signals.worker_process_shutdown.send(sender=None, pid=os.getpid(), exitcode=exitcode)

    def submit(self, coro_factory: Callable, *args: Any, job: AsyncApplyResult | None = None) -> None:
        """Submit a coroutine to this loop worker (thread-safe).

        The coroutine will be wrapped with the semaphore to limit
        concurrent execution.
        """
        loop = self._loop
        if loop is None:
            raise RuntimeError(f"loop worker {self._index} has not been started")
        with self._active_count_lock:
            self._active_count += 1
        # We need to create the coroutine from inside the target loop.
        # call_soon_threadsafe schedules a regular callback, so we
        # use it to create_task the semaphore-wrapped coroutine.
        loop.call_soon_threadsafe(self._schedule_task, coro_factory, args, job)

    def _schedule_task(self, coro_factory: Callable, args: tuple, job: AsyncApplyResult | None) -> None:
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._run_with_semaphore(coro_factory, args, job))
        self._tasks[task] = job
        task.add_done_callback(self._task_done)
        if job is not None:
            job.attach(task)

    def _task_done(self, task: asyncio.Task) -> None:
        # Not a finally in the coroutine: a task cancelled before its first
        # step never runs it, and its slot would be counted as busy forever.
        del self._tasks[task]
        with self._active_count_lock:
            self._active_count -= 1

    async def _run_with_semaphore(self, coro_factory: Callable, args: tuple, job: AsyncApplyResult | None) -> None:
        from celery.app.trace import async_body_done

        async with self._semaphore:
            if job is not None:
                if not job.start():
                    # Discarded by flush() while it waited for a slot.
                    return
                # The tracer's task copies this task's context, so the tracer
                # can tell the job when its body is done; see cancel_all().
                async_body_done.set(job.body_done)
            await coro_factory(*args)

    def cancel_task(self, task: asyncio.Task) -> None:
        """Cancel a task running on this loop from any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            # The loop closed between the check and the call, which takes
            # every task on it down anyway.
            logger.debug("Loop worker %s stopped before a job could be cancelled", self._index)

    def cancel_all(self) -> None:
        """Cancel every job that is still in its task body.

        A job past its body is only reporting an outcome that is already
        decided. Cancelling it there reports it revoked instead, over a result
        it may already have stored.
        """
        for task, job in list(self._tasks.items()):
            if job is None or not job.past_body:
                task.cancel()

    def stop(self) -> None:
        """Cancel the jobs still in their body, stop the event loop, and join the thread."""
        if self._loop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self.cancel_all)
                # Give tasks a brief interval to process their CancelledError
                # and run cleanup (callbacks, result reporting) before stopping.
                self._loop.call_soon_threadsafe(
                    self._loop.call_later,
                    0.5,
                    self._loop.stop,
                )
            except RuntimeError:
                # Loop closed underneath us; the thread is already on its way out.
                pass
        if self._thread:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                logger.warning(
                    "Loop worker %s did not stop within 10s, thread may be leaked (%d tasks were active)",
                    self._index,
                    self._active_count,
                )


class _TaskExited(Exception):
    """Carries a SystemExit or KeyboardInterrupt out of the task that raised it.

    asyncio re-raises both out of the task step and into ``run_forever()``,
    which closes the loop and takes every other task on that loop worker with
    it. Wrapping keeps the exception inside the task it came from.
    """

    def __init__(self, exc: BaseException) -> None:
        super().__init__(repr(exc))
        self.exc = exc


def _worker_lost(exc: BaseException) -> Any:
    """Build the WorkerLostError info a task that exits its worker reports."""
    from celery.exceptions import ExceptionInfo, WorkerLostError, reraise

    try:
        reraise(WorkerLostError, WorkerLostError(repr(exc)), exc.__traceback__)
    except WorkerLostError:
        return ExceptionInfo()


def _raise_in_thread(thread_id: int, exc: type[BaseException] | None) -> int:
    """Set or clear the asynchronous exception of a thread.

    Returns the number of thread states modified, so 0 means the thread was
    already gone. The exception is delivered at the thread's next bytecode
    boundary, which for a task blocked in C code (``time.sleep``, a socket
    read) is when that call returns.
    """
    return ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread_id),
        ctypes.py_object(exc) if exc is not None else None,
    )


class _SyncInterrupt(BaseException):
    """Raised in a sync task's thread to stop the task.

    A BaseException, so that neither the task's ``except Exception`` nor the
    tracer's failure handling stops it on the way out to the pool.
    """


class _TaskTerminated(_SyncInterrupt):
    """The task was terminated or cancelled, which its request announces."""


class _TaskTimedOut(_SyncInterrupt):
    """The task ran into its hard time limit, which reports it."""


# Where a sync job is. Only a job in its task body is raised in.
_STARTING, _IN_BODY, _PAST_BODY, _FINISHED = range(4)


class SyncJob:
    """A sync task in the thread pool: its time limits, and what stops it.

    Another thread stops the task by raising in its thread, and only while
    the task body runs, which the tracer brackets with open() and close().
    Anywhere else the exception would land in the result save or in the
    pool's own code. An interrupt that comes before the body is raised as it
    starts. One still pending as the body ends is dropped, except that a
    termination or timeout is raised again there, as it decides the outcome.

    The task's thread never takes the mutex while an exception can be pending
    for it: one landing just after the lock is acquired leaves it held.
    """

    def __init__(
        self,
        task_id: str | None,
        soft_timeout: float | None,
        timeout: float | None,
        timeout_callback: Callable | None,
    ) -> None:
        self.id = task_id
        self.soft_timeout = soft_timeout
        self.timeout = timeout
        self.timeout_callback = timeout_callback
        self._mutex = Lock()
        self._thread_id: int | None = None
        self._phase = _STARTING
        #: What the task is stopped with: a termination, a timeout or the soft limit.
        self._interrupt: type[BaseException] | None = None
        #: Whether an exception raised in the thread can still be pending.
        self._injected = False
        #: Whether the hard limit decided the outcome, which it then reports.
        self.timed_out = False
        #: Whether the hard limit gave up on the thread before the task was done.
        self.stuck = False
        #: Set when the thread is done with the task.
        self.finished = threading.Event()
        #: Set when the task's outcome is reported, by the task or by its hard limit.
        self.settled = threading.Event()
        self._timers: list[threading.Timer] = []

    @property
    def terminated(self) -> bool:
        return self._interrupt is _TaskTerminated

    def start(self, target: Callable, on_hard_limit: Callable[[SyncJob], None]) -> Callable:
        """Start the limits for the calling thread and return ``target`` guarded."""
        from celery.exceptions import SoftTimeLimitExceeded

        self._thread_id = threading.get_ident()
        if self.soft_timeout:
            self._timers.append(threading.Timer(self.soft_timeout, self.interrupt, (SoftTimeLimitExceeded,)))
        if self.timeout:
            self._timers.append(threading.Timer(self.timeout, on_hard_limit, (self,)))
        for timer in self._timers:
            timer.daemon = True
            timer.start()

        def _guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                stop = self._interrupt
                if stop is not None and issubclass(stop, _SyncInterrupt):
                    # Stopped as it was accepted: the tracer would store STARTED over it.
                    raise stop
                return target(*args, **kwargs)
            finally:
                self.finish()

        return _guarded

    def interrupt(self, exc: type[BaseException]) -> bool:
        """Raise ``exc`` in the task body, returning False if the body is done.

        A termination is raised one time only, and nothing replaces it or a timeout.
        """
        with self._mutex:
            if self._phase >= _PAST_BODY:
                return False
            if self._interrupt is _TaskTerminated:
                return exc is _TaskTerminated
            if self._interrupt is _TaskTimedOut:
                return False
            self._interrupt = exc
            self._inject(exc)
            return True

    def expire(self) -> bool:
        """Stop the task at its hard limit, returning False if it is done.

        The outcome is a timeout, unless a termination or the end of the body
        decided it first.
        """
        with self._mutex:
            if self._phase == _FINISHED:
                return False
            if self._phase == _PAST_BODY or self._interrupt is _TaskTerminated:
                return True
            self.timed_out = True
            self._interrupt = _TaskTimedOut
            self._inject(_TaskTimedOut)
            return True

    def _inject(self, exc: type[BaseException]) -> None:
        # Raised from the task's own thread, it would land in the pool's code.
        if self._phase != _IN_BODY or self._thread_id is None or self._thread_id == threading.get_ident():
            return
        if _raise_in_thread(self._thread_id, exc):
            self._injected = True

    def open(self) -> None:
        """Mark the start of the task body, raising an interrupt that came first."""
        with self._mutex:
            if self._interrupt is not None:
                self._phase = _PAST_BODY
                raise self._interrupt
            self._phase = _IN_BODY

    def close(self) -> None:
        """Mark the end of the task body, raising a termination or timeout it got past."""
        if self._phase != _IN_BODY:
            return
        self._phase = _PAST_BODY
        self._drop_pending()
        stop = self._interrupt
        if stop is not None and issubclass(stop, _SyncInterrupt) and not isinstance(sys.exception(), _SyncInterrupt):
            raise stop

    def finish(self) -> None:
        """Record that the thread is done with the task, so that its limits leave it alone."""
        self._phase = _FINISHED
        try:
            self._drop_pending()
        finally:
            for timer in self._timers:
                timer.cancel()
            self.finished.set()

    def _drop_pending(self) -> None:
        # An interrupter that saw the earlier phase is done when the mutex is free.
        while self._mutex.locked():
            time.sleep(0)
        if self._injected and self._thread_id is not None:
            self._injected = False
            _raise_in_thread(self._thread_id, None)


class TaskPool(BasePool):
    """Multi-loop asyncio + thread pool.

    - Async tasks: dispatched round-robin to N loop worker threads,
      each with its own event loop and Semaphore(P) concurrency limit.
    - Sync tasks: dispatched to a ThreadPoolExecutor with M workers.

    With Python 3.14t free-threading, all threads run truly parallel.
    """

    body_can_be_buffer = True
    signal_safe = False
    task_join_will_block = False

    #: How long a sync task gets to stop after its hard limit has raised in its
    #: thread. A thread still running after that is stuck and restarts the worker.
    stuck_thread_grace = 2.0

    def __init__(
        self,
        *args: Any,
        loop_workers: int = 1,
        loop_concurrency: int = 10,
        sync_workers: int = 1,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._loop_worker_count = loop_workers
        self._loop_concurrency = loop_concurrency
        self._sync_worker_count = sync_workers
        self._loop_workers: list[LoopWorker] = []
        self._executor: ThreadPoolExecutor | None = None
        self._active_futures: dict[Future, SyncJob] = {}
        self._async_jobs: dict[str, AsyncApplyResult] = {}
        self._sync_jobs: dict[str, SyncJob] = {}
        self._jobs_lock = Lock()
        self._stuck_thread_count = 0
        self._stuck_lock = Lock()
        self._accept_content: set | None = None

    def on_start(self) -> None:
        # Start N loop worker threads
        for i in range(self._loop_worker_count):
            w = LoopWorker(self._loop_concurrency, self.app, i)
            w.start()
            self._loop_workers.append(w)
        # Start M sync worker threads
        self._executor = ThreadPoolExecutor(max_workers=self._sync_worker_count)
        logger.info(
            "Pool started: %d loop workers (concurrency=%d each), %d sync workers",
            self._loop_worker_count,
            self._loop_concurrency,
            self._sync_worker_count,
        )

    def on_stop(self) -> None:
        sync_jobs = list(self._active_futures.items())
        for f, _ in sync_jobs:
            f.cancel()
        for w in self._loop_workers:
            w.stop()
        self._loop_workers.clear()
        with self._jobs_lock:
            self._async_jobs.clear()
        # The running sync tasks get to report, including those a cold shutdown
        # stopped. A stuck one is settled by its hard limit and left to the exit.
        running = [job for _, job in sync_jobs if not job.settled.is_set()]
        if running:
            logger.info("Waiting for %d running sync task(s) to finish", len(running))
        for job in running:
            job.settled.wait()
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    @property
    def stuck_threads(self) -> int:
        """How many threads that a hard time limit gave up on are still running."""
        return sum(job.stuck for job in list(self._active_futures.values()))

    @property
    def sync_threads_all_stuck(self) -> bool:
        """Whether a hard time limit gave up on every sync thread, so no queued sync task can start."""
        return self.stuck_threads >= self._sync_worker_count

    def restart(self) -> None:
        self.on_stop()
        self.on_start()

    def flush(self) -> None:
        """Drop the jobs that have not started.

        The consumer calls this when it loses the broker connection. Closing
        the connection returns their messages to the queue, so starting them
        here as well would run them twice.
        """
        for f in list(self._active_futures):
            f.cancel()
        with self._jobs_lock:
            jobs = list(self._async_jobs.values())
        for job in jobs:
            job.discard()

    def terminate_job(self, job_id: str, signal: Any = None, interrupt_thread: bool = True) -> bool:
        """Stop a running task, returning whether it is stopped.

        An async task is cancelled. A sync task is stopped on a best-effort
        basis, by raising in its thread: Python code stops immediately, while a
        call blocked in C code, such as a socket read, sees it only when it
        returns. ``interrupt_thread=False`` leaves sync tasks to finish, as a
        warm shutdown does. A task whose body is done is left to report its
        outcome, and so is one that has already finished.
        """
        with self._jobs_lock:
            job = self._async_jobs.get(job_id)
            sync_job = self._sync_jobs.get(job_id)
        if job is not None:
            return job.terminate(signal)
        return sync_job is not None and interrupt_thread and sync_job.interrupt(_TaskTerminated)

    def _forget_async_job(self, job: AsyncApplyResult) -> None:
        with self._jobs_lock:
            if self._async_jobs.get(job.id) is job:
                del self._async_jobs[job.id]

    def _is_async_task(self, args: tuple) -> bool:
        if self.app and args:
            task_name = args[0]
            try:
                task = self.app.tasks[task_name]
                return inspect.iscoroutinefunction(task.run)
            except KeyError, AttributeError:
                pass
        return False

    def _pick_loop_worker(self) -> LoopWorker:
        return min(self._loop_workers, key=lambda w: w._active_count)

    def on_apply(
        self,
        target: Callable,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        callback: Callable | None = None,
        accept_callback: Callable | None = None,
        timeout_callback: Callable | None = None,
        error_callback: Callable | None = None,
        soft_timeout: float | None = None,
        timeout: float | None = None,
        **options: Any,
    ) -> ApplyResult | AsyncApplyResult:
        args = tuple(args or ())
        kwargs = kwargs or {}

        if self._is_async_task(args) and self._loop_workers:
            worker = self._pick_loop_worker()
            job = AsyncApplyResult(worker, args[1], self._forget_async_job)
            with self._jobs_lock:
                self._async_jobs[job.id] = job
            worker.submit(
                self._run_async_task,
                args,
                callback,
                accept_callback,
                timeout_callback,
                error_callback,
                soft_timeout,
                timeout,
                job=job,
            )
            return job
        else:
            return self._apply_sync_task(
                target,
                args,
                kwargs,
                callback,
                accept_callback,
                error_callback=error_callback,
                timeout_callback=timeout_callback,
                soft_timeout=soft_timeout,
                timeout=timeout,
                **options,
            )

    async def _run_async_task(
        self,
        args: tuple,
        callback: Callable | None,
        accept_callback: Callable | None,
        timeout_callback: Callable | None = None,
        error_callback: Callable | None = None,
        soft_timeout: float | None = None,
        timeout: float | None = None,
    ) -> None:
        """Execute an async task using the async tracer."""
        from kombu.serialization import loads as loads_message
        from kombu.serialization import prepare_accept_content

        from celery.app.trace import build_async_tracer

        # Unpack args: (task_name, uuid, request, body, content_type, content_encoding)
        task_name, uuid, request, body, content_type, content_encoding = args[:6]

        if accept_callback:
            accept_callback(os.getpid(), time.monotonic())

        try:
            app = self.app
            embed = None
            if content_type:
                accept = self._accept_content
                if accept is None:
                    accept = self._accept_content = prepare_accept_content(app.conf.accept_content)
                task_args, task_kwargs, embed = loads_message(
                    body,
                    content_type,
                    content_encoding,
                    accept=accept,
                )
            else:
                task_args, task_kwargs, embed = body

            request.update(
                {
                    "args": task_args,
                    "kwargs": task_kwargs,
                    "hostname": request.get("hostname", ""),
                    "is_eager": False,
                },
                **(embed or {}),
            )

            task_obj = app.tasks[task_name]

            # The async tracer is built once per task type at consumer startup
            # (see update_strategies). Fall back to building it lazily for paths
            # that bypass the consumer (tests, eager use).
            tracer = task_obj.__async_trace__
            if tracer is None:
                tracer = task_obj.__async_trace__ = build_async_tracer(
                    task_name,
                    task_obj,
                    app=app,
                )

            tracer_result = await self._run_tracer_with_timeouts(
                tracer,
                uuid,
                task_args,
                task_kwargs,
                request,
                soft_timeout=soft_timeout,
                timeout=timeout,
                timeout_callback=timeout_callback,
            )

            # The tracer always returns a 4-tuple, so None means the hard
            # timeout fired and on_timeout has already reported it.
            if tracer_result is None:
                return

            R, I, T, Rstr = tracer_result

            result = (1, R, T) if I else (0, Rstr, T)
            if callback:
                callback(result)
        except asyncio.CancelledError:
            self._report_terminated(uuid, error_callback)
            # Report, then let the cancellation carry on outwards: swallowing it
            # here leaves the task looking like it completed and stalls shutdown.
            raise
        except _TaskExited as exited:
            self._handle_task_exit(uuid, error_callback, exited.exc)
        except (SystemExit, KeyboardInterrupt) as exc:
            self._handle_task_exit(uuid, error_callback, exc)
        except Exception:
            from celery.exceptions import ExceptionInfo

            self._report_failure(uuid, error_callback, ExceptionInfo())

    def _handle_task_exit(self, uuid: str | None, error_callback: Callable | None, exc: BaseException) -> None:
        """Report a task that tried to exit the worker.

        Upstream reports a task that takes its worker down as WorkerLostError,
        and a shutdown request is handed to the thread that can carry it out.
        """
        self._request_worker_exit(exc)
        self._report_failure(uuid, error_callback, _worker_lost(exc))

    @staticmethod
    def _request_worker_exit(exc: BaseException) -> None:
        """Pass a task's shutdown request to the thread that can honour it."""
        from celery.exceptions import WorkerShutdown, WorkerTerminate
        from celery.platforms import EX_OK
        from celery.worker import state

        if not isinstance(exc, (WorkerTerminate, WorkerShutdown)):
            return
        # SystemExit accepts any object as its code, and the worker exits
        # with whatever it is handed here.
        code = exc.code if isinstance(exc.code, int) else EX_OK
        if isinstance(exc, WorkerTerminate):
            state.should_terminate = code
        else:
            state.should_stop = code

    def _report_terminated(self, uuid: str | None, error_callback: Callable | None) -> None:
        """Report a task that terminate_job() stopped, which the request then tells apart."""
        from celery.exceptions import ExceptionInfo, Terminated

        exc = Terminated("cancelled")
        self._report_failure(uuid, error_callback, ExceptionInfo((type(exc), exc, None)))

    def _report_failure(self, uuid: str | None, error_callback: Callable | None, exc_info: Any) -> None:
        """Report a failure the tracer did not report itself.

        The tracer stores, logs and signals everything the task body raises,
        so whatever arrives here escaped it and is an internal error. The
        error callback is the request's ``on_failure``, which records
        FAILURE, sends ``task_failure`` and logs the traceback.
        """
        if error_callback is None:
            logger.error(
                "Task %s raised %r outside the tracer",
                uuid,
                exc_info.exception,
                exc_info=exc_info.exc_info,
            )
            return
        try:
            error_callback(exc_info)
        except Exception:
            # Last chance to report the task; nobody reads this future's result.
            logger.exception("Failed to report the failure of task %s", uuid)

    async def _run_tracer_with_timeouts(
        self,
        tracer: Callable,
        uuid: str,
        task_args: tuple,
        task_kwargs: dict,
        request: dict,
        soft_timeout: float | None = None,
        timeout: float | None = None,
        timeout_callback: Callable | None = None,
    ) -> tuple[Any, ...] | None:
        """Run the async tracer under the task's time limits.

        Cancellation is the only way asyncio can interrupt a running
        coroutine, so the soft limit cancels the task the tracer runs in and
        the tracer turns that back into SoftTimeLimitExceeded, failing the
        task the way any other exception would. The task body sees
        CancelledError at its await point; a task that catches it keeps
        running until the hard limit.

        The hard limit runs on the job's own task, one level above the
        tracer, so that a soft limit which has already fired cannot mask it.
        """
        from celery.app.trace import async_cancellation_reason
        from celery.exceptions import SoftTimeLimitExceeded

        job_task = asyncio.current_task()
        soft_expired = False

        def cancellation_reason() -> BaseException | None:
            # Only a cancellation that leaves the job's own task alone is the soft limit.
            if not soft_expired or (job_task is not None and job_task.cancelling()):
                return None
            return SoftTimeLimitExceeded(soft_timeout)

        def fire_soft_timeout() -> None:
            nonlocal soft_expired
            soft_expired = True
            traced.cancel()

        # Set before the tracer's task is created so that its copy of the
        # context carries the reason the tracer asks for.
        token = async_cancellation_reason.set(cancellation_reason)
        try:
            traced = asyncio.get_running_loop().create_task(
                self._trace(tracer, uuid, task_args, task_kwargs, request),
                name=f"celery-trace-{uuid}",
            )
            soft_handle = None
            if soft_timeout:
                soft_handle = asyncio.get_running_loop().call_later(soft_timeout, fire_soft_timeout)
            try:
                if timeout:
                    return await asyncio.wait_for(traced, timeout)
                return await traced
            except asyncio.TimeoutError:
                if timeout_callback:
                    timeout_callback(False, timeout)
                # Don't raise, on_timeout already handled task_ready + mark_as_failure.
                return None
            except asyncio.CancelledError:
                exc = cancellation_reason()
                if exc is None:
                    raise
                # The soft limit landed outside the task body, where the
                # tracer reports nothing itself.
                raise exc from None
            finally:
                if soft_handle is not None:
                    soft_handle.cancel()
        finally:
            async_cancellation_reason.reset(token)

    @staticmethod
    async def _trace(
        tracer: Callable,
        uuid: str,
        task_args: tuple,
        task_kwargs: dict,
        request: dict,
    ) -> Any:
        """Run the tracer with the exceptions asyncio would escalate wrapped."""
        try:
            return await tracer(uuid, task_args, task_kwargs, request)
        except (SystemExit, KeyboardInterrupt) as exc:
            raise _TaskExited(exc) from None

    def _apply_sync_task(
        self,
        target: Callable,
        args: tuple,
        kwargs: dict,
        callback: Callable | None,
        accept_callback: Callable | None,
        error_callback: Callable | None = None,
        timeout_callback: Callable | None = None,
        soft_timeout: float | None = None,
        timeout: float | None = None,
        **options: Any,
    ) -> ApplyResult:
        if self._executor is None:
            raise RuntimeError("pool has not been started")
        job = SyncJob(args[1] if len(args) > 1 else None, soft_timeout, timeout, timeout_callback)
        if job.id is not None:
            with self._jobs_lock:
                self._sync_jobs[job.id] = job
        f = self._executor.submit(
            self._run_in_thread,
            target,
            args,
            kwargs,
            callback,
            accept_callback,
            error_callback,
            job,
        )
        self._active_futures[f] = job
        f.add_done_callback(self._sync_job_done)
        return ApplyResult(f)

    def _sync_job_done(self, future: Future) -> None:
        job = self._active_futures.pop(future, None)
        if job is None:
            return
        if job.id is not None:
            with self._jobs_lock:
                if self._sync_jobs.get(job.id) is job:
                    del self._sync_jobs[job.id]
        job.settled.set()

    def _on_sync_hard_limit(self, job: SyncJob) -> None:
        """Stop a sync task at its hard limit, and give up on a thread that does not stop.

        The timeout is reported immediately, unless a termination or the end of
        the body decided the outcome first. A thread still running after
        ``stuck_thread_grace`` counts as stuck, which restarts the worker.
        """
        if not job.expire():
            return
        if job.timed_out:
            self._report_timeout(job)
        if job.finished.wait(self.stuck_thread_grace):
            return
        job.stuck = True
        try:
            logger.error(
                "Sync task %s is still running %ss after its hard time limit (%ss). "
                "Its thread is stuck; will trigger process restart.",
                job.id,
                self.stuck_thread_grace,
                job.timeout,
            )
            with self._stuck_lock:
                self._stuck_thread_count += 1
            if not job.timed_out and not job.terminated:
                # Its body is done, but nothing reports the outcome while the thread is stuck.
                self._report_timeout(job)
        finally:
            # A stuck thread possibly never reports.
            job.settled.set()

    @staticmethod
    def _report_timeout(job: SyncJob) -> None:
        if job.timeout_callback is None:
            return
        try:
            job.timeout_callback(False, job.timeout)
        except Exception:
            logger.exception("Failed to report the hard time limit of task %s", job.id)

    def _run_in_thread(
        self,
        target: Callable,
        args: tuple,
        kwargs: dict,
        callback: Callable | None,
        accept_callback: Callable | None,
        error_callback: Callable | None,
        job: SyncJob,
    ) -> Any:
        from celery.app.trace import sync_body_window
        from celery.exceptions import ExceptionInfo

        self.app.set_current()
        uuid = args[1] if len(args) > 1 else None
        # The executor runs every task on a thread in the same context.
        token = sync_body_window.set(job)
        try:
            target = job.start(target, self._on_sync_hard_limit)
            return apply_target(
                target,
                args,
                kwargs,
                callback,
                accept_callback,
                error_callback=error_callback,
                propagate=(_SyncInterrupt,),
            )
        except _TaskTerminated:
            self._report_terminated(uuid, error_callback)
            return None
        except _TaskTimedOut:
            # Already reported by the hard limit.
            return None
        except (SystemExit, KeyboardInterrupt) as exc:
            # apply_target re-raises the two the worker acts on. Nothing reads
            # the future they would end up in, so they are handed on here.
            self._handle_task_exit(uuid, error_callback, exc)
            return None
        except Exception:
            # Nothing reads the future, so an exception that escapes the
            # tracer is only reported if it is reported from here.
            self._report_failure(uuid, error_callback, ExceptionInfo())
            return None
        finally:
            sync_body_window.reset(token)
            job.finish()

    def _get_info(self) -> dict[str, Any]:
        info = super()._get_info()
        info.update(
            {
                "implementation": "asyncio+threads",
                "loop-workers": self._loop_worker_count,
                "loop-concurrency": self._loop_concurrency,
                "sync-workers": self._sync_worker_count,
                "loop-active": [w._active_count for w in self._loop_workers],
            },
        )
        return info
