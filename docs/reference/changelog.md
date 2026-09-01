# Changelog

## Unreleased

### Fixed

- `Mailbox._collect` waited for replies until `drain_events` timed out, which
  on a channel shared with a busy consumer it never does -- it returns for as
  long as messages keep arriving. With no reply limit nothing ended the loop,
  so `inspect` and any other `broadcast(reply=True)` blocked on a set of
  replies that was already complete. The `timeout` is now the window it says
  it is
- The shared loop was stopped and closed with whatever was still running on
  it, so a transport's background tasks -- consumer iterations, heartbeats,
  expiry refreshes -- were reported as `Task was destroyed but it is pending!`
  at interpreter exit, sometimes trailed by a `no running event loop`
  traceback from the coroutine's next `await`. It now performs the same
  shutdown `asyncio.run` does
- A chord whose body was itself a chord never reported the header's failure. A
  chord's `task_id` option is its *header group's* id -- `freeze` assigns
  `self.id = self.tasks.id` -- so `chord_error_from_stack` stored the error
  under a key nobody reads, while the result the caller was handed, the
  innermost body's, stayed `PENDING` and `get()` blocked until it timed out.
  The error now walks down to the body, and the inner header's members, which
  will never run either, are failed alongside it
- `Connection.connection_errors`, `channel_errors` and `resource_locked_errors`
  answered with a generic default until something had been connected -- which
  is exactly when they are asked, since the caller is usually about to connect
  or has just been disconnected. Against an unreachable AMQP broker the
  worker's recoverable-error handler therefore never saw aiormq's
  `AMQPConnectionError` and died with an unhandled traceback instead of the
  shutdown it was supposed to report. The tuples now come from the transport
  class, which does not require an instance
- A chord header built from a generator was unrolled completely before any of
  its tasks were published, so the header could not be produced incrementally
  (upstream #3021). `_apply_tasks` had materialised the header to write
  `set_chord_size` before the first dispatch; it now looks one task ahead and
  writes the size before the last dispatch instead, which closes the same race
  -- the final part return always sees the size -- without draining the
  generator
- `self.request` inside a sync task body was blank -- no `id`, no `retries`,
  and `called_directly` still true. The worker runs sync task bodies in a
  thread through `sync_to_async`, and the request stack was a
  `threading.local`, so nothing the trace pushed was visible there. Most
  visibly this made `self.retry()` take its "called directly" branch and
  re-raise without ever publishing the retry, so a retried task hung in the
  `RETRY` state forever. The stack is now backed by a `ContextVar`, which
  `sync_to_async` carries into the thread
- `with Connection(...)` opened the connection on a throwaway event loop and
  closed it on another, so leaving the block raised `RuntimeError: Event loop
  is closed`. The sync context manager, `Control.purge()`, `Control.broadcast()`
  and `app.events.default_dispatcher()` now share one long-lived loop, which
  also unbreaks Flower and `celery -A app worker --purge`

- `with Connection(...)` raised from inside a running event loop, which is
  where Flower's tornado request handlers call it, so Flower's pages 500'd
- `start_worker()` ran the embedded test worker under its own `asyncio.run()`
  while the test published through the process-wide loop, so the two shared
  transports across two loops. It now runs on the shared loop, which un-skips
  the `celery.contrib.testing` tests
- `start_worker(logfile=None)` passed `""` on to the logging setup, which took
  it for a filename and opened the working directory

### Changed

- Moved the shared loop runner to `kombu.utils.eventloop`, so kombu and celery
  drive a connection from the same loop; `celery.utils.eventloop` re-exports it
- The `flower` extra no longer installs Flower itself, only Flower's other
  dependencies. Flower requires upstream `celery` from PyPI, which installed
  over this package for anyone who was not resolving inside this workspace.
  Install it with `pip install "celery-asyncio[flower]" && pip install flower
  --no-deps`
- Dropped the `[tool.uv] override-dependencies` entry that neutralised
  upstream `celery`; nothing in the project pulls it in any more
- Raised the `amqp` extra's floor to `aio-pika>=9.5.0`, the line CI runs against
- Restored the per-rule reasons on the ruff `ignore` list, lost in the merge

### Added

- A Broker API (kombu) section in the docs nav: Connection, producers and
  consumers, exchanges and queues, and the simple interface
- CI runs the celery integration suite, which nothing had ever run
- Each pytest-xdist worker now runs the integration suite against a Redis
  database of its own, so parallel workers no longer share broker queues,
  fanout channels or the keys the test tasks write to. Without it `inspect`
  saw every worker's embedded worker
- A `global_pubsub` marker for the four tests that assert on the set of active
  Redis PUBSUB channels. Redis reports those per server rather than per
  database, so CI runs them in a step of their own

### Removed

- The `zstd` extra. PEP 784 put zstd in the stdlib as of 3.14, this package's
  floor, so `zstandard` was already unused
- The unused `unit` and `integration` pytest markers, and the vestigial
  `UV_NO_SOURCES` from the workflows: there is no `[tool.uv.sources]` table
- Merged the `kombu-asyncio` package into this repository; `kombu` now ships as
  a top-level package of `celery-asyncio` instead of a separate install
- Requirements no longer list `kombu-asyncio` as a dependency
- Raised the minimum `asgiref` version to 3.8.0

## v6.0.0a5

### Fixed

- Receiving an AMQP message with a TTL raised `AttributeError` (kombu-asyncio 6.0.0a5)

### Changed

- Every re-raise now names its cause, so a traceback points at the original error
  instead of stopping at the exception celery raised in its place

## v6.0.0a4

Ported every applicable upstream fix from the sweep recorded in `UPSTREAM-PLAN.md`,
and fixed the fork-only defects the sweep turned up along the way.

### Added

- `AsyncResult.exists()` and its async twin
- `time_limit` and `soft_time_limit` on `task.request`
- Separate `acks_on_failure` and `acks_on_timeout` task options
- `additional_connection_errors` transport option for the Valkey/Redis backend
- Warning when routing is declared as a task attribute

### Fixed

- Publishing opened a broker connection per task
- Chord counter double-counted on redelivery
- A failing step in a chain never reached the chord body
- Chains of chords were nested instead of flat; empty groups broke `as_tuple`
- Eager chains kept running past `Ignore` and `Reject`; `EagerResult` had no `aget()`
- Crontabs matched against the wrong timezone, and DST gaps were skipped
- Beat kept a broker connection it never used, and lost entries that asked to wait
- `send_task` by name ignored the task's own options
- PEP 649 lazy annotations broke task registration
- Revoked tasks were not marked `REVOKED` in the backend
- Cold-shutdown terminations were reported as task failures
- Reconnects lost in-flight bookkeeping, and broken connections were reused
- The event dispatcher dropped and leaked buffered events
- `autoretry_for` was ignored on async tasks
- Passwords in failover URLs were logged in the clear
- `redis_backend_use_ssl` was ignored on `redis://`, and sentinel ACL credentials were dropped
- Routing-only queues were added to the consumer set
- Control and event queues are now exclusive by default
- Django connections were opened only to be closed again

### Fixed (production readiness audit)

- `LoopWorker.start()` returned before its event loop was running
- `LoopWorker.stop()` left the loop unclosed, leaking its self-pipe on every pool restart
- The hard-timeout timer for sync tasks was never cancelled, parking a thread per task
- `CancelledError` was swallowed in the async task path instead of propagating
- The timer scheduled on the wall clock, so a clock step shifted or stampeded pending entries
- A deep backlog could starve the `max_tasks_per_child` and `max_memory_per_child` checks
- `celery-asyncio[hiredis]` and `[libvalkey]` installed nothing
- A local `uv build` swept vendored virtualenvs into the sdist

## v6.0.0a3

- Dual Valkey/Redis support; `redis` backend module renamed to `valkey_redis`
- API reference (mkdocstrings) and migration guide
- Flower packaged as a `[flower]` extra
- Soft time limits for sync tasks in the thread pool
- Redis PUBSUB replaced with polling in the result backend
- Django 6.0 Tasks backend removed in favour of
  [django-tasks-celery](https://github.com/oliverhaas/django-tasks-celery)
- Memcached backend, Python 2 compat shims, and dead code removed

## v6.0.0a2

Initial alpha of celery-asyncio.

### What works

- Async worker with hybrid asyncio + thread pool
- `async def` and regular `def` tasks in the same worker
- Valkey/Redis transport (with sorted-set priority queues, Lua scripts, fanout)
- AMQP transport (via aio-pika, RabbitMQ)
- Full CLI (`celery worker`, `celery inspect`, `celery control`, `celery result`)
- Task events and Celery Flower monitoring
- Worker restart (max tasks, max memory, stuck threads)
- Task timeouts (soft and hard, async and sync)
- Django 6.0 Tasks support via [django-tasks-celery](https://github.com/oliverhaas/django-tasks-celery)
- Delayed/scheduled tasks (countdown, eta)
- Task priority
- Task retries

### What's not yet tested

- Multi-worker deployments
- Rate limiting and autoscaling

### Breaking changes from upstream Celery

- Requires Python 3.14+
- Requires kombu-asyncio (not upstream kombu)
- Removed eventlet, gevent, and prefork pool backends
- Removed billiard dependency
- Default pool is `asyncio` (not `prefork`)
- Bootsteps are async (`async def start/stop`)
