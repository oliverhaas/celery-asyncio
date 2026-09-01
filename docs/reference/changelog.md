# Changelog

## Unreleased

### Fixed

- `with Connection(...)` opened the connection on a throwaway event loop and
  closed it on another, so leaving the block raised `RuntimeError: Event loop
  is closed`. The sync context manager, `Control.purge()`, `Control.broadcast()`
  and `app.events.default_dispatcher()` now share one long-lived loop, which
  also unbreaks Flower and `celery -A app worker --purge`

- `with Connection(...)` raised from inside a running event loop, which is
  where Flower's tornado request handlers call it, so Flower's pages 500'd

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
