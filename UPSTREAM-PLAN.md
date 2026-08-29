# Tracking upstream celery

This fork branched from [celery/celery](https://github.com/celery/celery) at `5fd0b3e00`
(v5.6.0rc1, 2025-11-02). Upstream is at `0ec260de7` (2026-08-26). 121 non-merge commits since
then touch `celery/`; 92 of those touch a file that still exists here.

This file records what was decided for each one, so the next sweep starts from `0ec260de7` and
nothing gets re-triaged from scratch. The kombu half of the same exercise lives in
`kombu-asyncio/UPSTREAM-PLAN.md`.

## How to read a verdict

| Verdict | Meaning |
|---|---|
| **Ported** | Landed here, verbatim or adapted. Commit named. |
| **Already handled** | The fork already does this. Location named. |
| **Not applicable** | The code, subsystem or dependency does not exist here. |
| **Open** | Triaged as real and missing, not yet done. Listed under "Still open". |

Verdicts marked **(unverified)** came from a triage pass and have not yet been checked against
the code by hand. Treat them as leads, not facts. Three of the kombu sweep's triage verdicts
turned out to be wrong when checked, so nothing gets ported on a triage verdict alone.

The fork is an async rewrite, so "not applicable" is common. What is missing, and therefore what
whole classes of upstream fix cannot land:

- no prefork, eventlet or gevent. `celery/concurrency/` is `aio.py` and `base.py`
- no hub, no fd poller, no `synloop`/`asynloop`. `celery/worker/loops.py` is asyncio
- no `celery/backends/` for database, rpc, mongodb, s3, gcs, elasticsearch, couchbase, arangodb,
  dynamodb, azure or consul. What exists: `base`, `asynchronous`, `cache`, `filesystem`,
  `valkey_redis`
- no `celery/backends/redis.py`. The equivalent is `valkey_redis.py`, a separate async
  implementation
- no `fast_trace_task` and no C-optimized trace
- no native delayed delivery bootstep
- no `celery/apps/multi.py`
- time limits are asyncio, not signals

## Re-running the sweep

```
git -C ../../celery/celery fetch origin
git -C ../../celery/celery log --oneline --no-merges 0ec260de7..origin/main -- celery/
```

Then drop anything touching only the subsystems listed above.

---

## Ported

| Upstream | Landed as | Note |
|---|---|---|
| `323f21d3f` RabbitMQ 4.3.0 compatibility for transient queues | `cdb775271` | Adapted. `control_queue_exclusive` and `event_queue_exclusive` default to a `None` sentinel meaning "exclusive unless durability was asked for". Resolved in `Control.__init__` and `EventReceiver` rather than left to kombu, so the setting reads the same on every kombu version. Supersedes `d9b39790d` and its revert `7bfa8d0c3`. |
| `134e3b89e` dusk_astronomical horizon | `0a7135952` | Verbatim. |
| `3511be41d` ImproperlyConfigured when ephem is missing | `0a7135952` | Adapted: adds the `solar` extra the hint points at, which this fork did not have. |
| `583fa06af` Shared `retry_kwargs` mutation in autoretry | `fdfb19cac` | Adapted, and it uncovered a fork-only bug that mattered more. See "Found along the way". |
| `acce2acc7` Skip publish when producer is None | `1f32bb270` | Verbatim. |
| `8b4b29c93` Stop leaking exceptions, cancel stale Heart timers | `1f32bb270` | Verbatim, including `Events._close()` calling `disable()` rather than `close()`. |
| `10f24ce07` Preserve re-buffered events during flush | `1f32bb270` | Verbatim. |
| `97ed017c0` + `f85031f61` Group-buffered events across a flush | `1f32bb270` | Adapted, and much worse here than upstream. See "Found along the way". |
| `066e96e01` `_default_consume_from` so routing-only queues stay out of consumers | `a5af881a0` | Verbatim. |
| `ece686299` `deselect` no longer promotes the routing table to a selection | `a5af881a0` | Verbatim. Depends on `066e96e01`. |
| `2e150f833` `select` keys `consume_from` by the real queue name, not the alias | `a5af881a0` | Verbatim. |
| `1a4768959` Defer the default queue lookup in the task sender | `a5af881a0` | Adapted: applied to `_create_async_task_sender` as well, which upstream does not have. |
| `1cc9ecf43` `Consumer.on_close()` no longer wipes in-flight reservations | `34c397bb8` | Verbatim. |
| `d6131816f` Tolerate errors from `request.cancel()` after connection loss | `34c397bb8` | Verbatim. |
| `201573a11` Null the pidbox consumer between reset cycles | `34c397bb8` | Adapted: the fork's `Pidbox.stop` is a coroutine and already swallows the cancel error, so only the `self.consumer = None` is new. |
| `03c79ac14` Guard the event control commands against a `None` dispatcher | `34c397bb8` | Adapted: applied to `enable_events` and `disable_events` too, not just `heartbeat`. |
| `63c191022` Do not fail a task on timeout during cold shutdown | `36e095705` | Adapted. `on_cold_shutdown` sets `state.should_terminate` itself, since the signal handler only sets it after the callback returns. Upstream's `task_consumer.cancel()` is skipped: it is a coroutine here and the handler is sync. |
| `713576800` Run the failure callbacks on a hard timeout, not just `mark_as_failure` | `36e095705` | Verbatim. Lands on the same `on_timeout` branch as `63c191022`. |
| `40c234919` Split `acks_on_failure` and `acks_on_timeout` | `14c1371fb` | Verbatim, minus the docs half: this fork has no `configuration.rst`. |

### Found along the way

Two fork-only defects that the ports above exposed. Neither exists upstream.

**`autoretry_for` did nothing on async tasks.** Calling a coroutine function only builds the
coroutine; nothing in it runs until it is awaited. The wrapper put the call inside `try/except`
but returned the coroutine straight out of it, so the except clauses never saw anything. A sync
task retried three times where the identical async task ran once and failed. In a fork whose
whole point is async tasks, that is the common case rather than an edge. Fixed in `fdfb19cac`.

**Every group-buffered event flush went out empty.** `flush()` handed `_publish` the live group
buffer and cleared it on the next line. Upstream loses only the offline re-buffer that way,
because its publish is synchronous. Here `producer.publish()` is a coroutine and the payload is
not read until the scheduled task runs, so the clear won every time and the broker received `[]`.
Fixed in `1f32bb270`. The existing `test_send_buffer_group` asserted `_publish` was called with
`[]`, so the bug was written down as an expectation.

## Not applicable

Grouped by what is missing, so a future sweep can classify most commits by inspection.

**No prefork / asynpool / eventlet / gevent / threads pool**
`ba729fcdb` thread pool time limits ·
`91696a3d6` green pool autoscale capacity ·
`d4eb32a78` free worker stalling after reconnect (AsynPool.flush) ·
`1690dd3ac` asynpool `_sentinel_poll` AttributeError ·
`3f0f0fe7e` asynpool return from finally ·
`d68250e55` warm shutdown, thread pool prefetch ·
`67b328abd` warm shutdown with eventlet>=0.37.0 ·
`ab711a0b2` broker heartbeats during graceful shutdown (prefork timer firing)

**No hub / synloop / fd poller**
`c167ccf4a` `hub.reset()` in synloop on connection error ·
`c38600d42` clear the hub timer on exception ·
`2b3c6fa38` reconnect after redis failover (hub half; the asynpool half is also absent) ·
`9c8145fc0` deferred ack synloop bug (`call_soon_ack`)

**Backend does not exist here**
`aa4c3b101`, `63f70cf9e`, `b1007a4bb`, `26dab80cb`, `0348d6425`, `f9ea6771e`, `026682218`
(database) · `1b40d9367`, `7c5d9a62d`, `6b20dcd2d` (rpc) · `b4edb74e7` (s3) ·
`19775b237` (gcs, cursesmon)

**No `celery/backends/redis.py` pub-sub ResultConsumer**
The fork's result consumer polls; there is no PUBSUB subscription to leak or unsubscribe.
`d35acd7f5` redundant unsubscribe · `74a7a63bb` / `4f1595434` / `4a11650b1` `_pending_messages`
leak, reverted and re-landed upstream · `6b1fad369` deprecated `get_connection` args ·
`8f0842b33` redis-py < 5.3.0 compat, and the floor here is redis>=7

**Subsystem absent**
`efc3a7f11` multi stopwait EPERM, there is no `apps/multi.py` ·
`5013b4a99`, `41bad6f22`, `30649dbd4`, `ba20bed77` native delayed delivery ·
`975840963` Sphinx extension ·
`730ab395c` reserved requests get the new dispatcher on reconnect, there is no
`reserved_requests`/`request.eventer` pairing here ·
`b313b6412` skip prefetch reduction under per-consumer QoS: `Tasks.start` installs a no-op
`set_prefetch_count` and computes no `qos_global`, so there is no per-consumer vs global
distinction to act on. Revisit if prefetch/QoS is ever implemented

**Docs, release prep, test fixtures**
`d96df921e`, `658230391`, `066092edc`, `a3f51e4c3`, `99b0a8977`, `bcc1798a8`,
`6a43c846f`, `21dbc73f8`, `cca111648`, `b446910f1`

---

## Still open

Triaged as real and missing but not yet checked line by line, let alone ported. Roughly ordered
by how much they matter. **Every one of these is (unverified).**

### Worker

`feb789acc` close the broken connection during recovery. Only half applies: the fork never calls
`collect()`, so the `socket_timeout` half is N/A, but the fork's `Connection` bootstep has no
`stop()` and `blueprint.restart()` uses `method="stop"`, so the dead connection really does
survive the restart. Needs async handling, since
`on_connection_error_after_connected` is sync and `connection.close()` is a coroutine ·
`333a82f74` mark revoked tasks REVOKED in the backend immediately. Must not be ported verbatim:
the sync `store_result` would do blocking Redis I/O on the event loop inside the pidbox handler.
Use `app.backend.amark_as_revoked` scheduled on the running loop, falling back to the sync path
when no loop is running

### Request, task and trace

`e2b276e60` mark a rejected task failed when it is not requeued · `1fe2a08d0` expose `time_limit` and
`soft_time_limit` on `task.request` · `b8f85213f` let a request's `ignore_result` beat the task
definition · `865922abd` dispatch the
chain and callbacks on the dedup fast path (the fork uses `build_async_tracer`, so this needs
looking at before it is believed) · `3f2cf57d3` a clear error when the worker registry is empty

### Canvas and results

`1d563dafb` stop eager chain execution on Ignore and Reject · `a094d2a89` restore GroupResult
fan-out in `chain.as_tuple()` · `379a629dc` keep group errbacks mutable · `df57d9ab9` O(K²)
message bloat in a chain of chords · `7eb644e52` finalize the ResultSet barrier when
`iter_native` begins · `bf1cf69e2` skip empty groups in chains · `2d560f5c1` `AsyncResult.exists()`

### Backends

`6e0d68308` handle UUID task ids in key-value store key generation · `1432d9b6c` propagate chained
task failures into the chord body · `477c816f9` wrap new-style errback calls so one raising
errback does not halt the chain · `72e9240aa` chord error handling when the body is a chain ·
`8eec5af31` + `f8668fcf5` a shared `reconnect_on_error` on `BaseResultConsumer`, without
`socket.timeout` in the reconnect conditions

### valkey_redis backend

`be0e2de23` allow `redis_backend_use_ssl` with a `redis://` URL · `9f0a61c61` Sentinel ACL, the
username never reaches `master_for()` · `0472aaccb` configurable additional connection errors ·
`22a03fa13` redis-py DriverInfo

### App, CLI, Django fixup

`fbd01579c` honour per-task serializer and execution options in `send_task` · `571efe812` warn
when routing options are set as task attributes · `e7c4454f8` `datefmt` on ColorFormatter and
TaskFormatter · `56d80409a` mask credentials in the alternates from `inspect stats` ·
`7ca0e0f18` `__class_getitem__` on the generic classes · `4886d5d0c` preload options for the
control command · `7735d2ba9` friendly CLI errors instead of tracebacks · `cc3350ef9` avoid
creating Django DB connections during cleanup · `a4f9beb41` close DB pools only in prefork mode,
which here means never · `8ea903b6d` defensive `pool_cls.__module__` checks in
`contrib/testing/worker.py`

### Python 3.14 / PEP 649

`4369baf04` `head_from_fun` follows `__wrapped__` on 3.14+ · `66bcdebb4` `fun_accepts_kwargs`
evaluates annotations eagerly · `e49270e35` NameError from TYPE_CHECKING-only annotations. These
three are one workstream. Check what the supported Python range actually is first.

### Beat and time

`fe9457327` a `celery_beat_task` header on tasks sent by beat · `f52429cfd` reheap entries that
ask to retry later · `1fcbf6fa4` normalize aware datetimes into the schedule timezone in
`crontab.remaining_delta` · `c30f42ad2` handle DST gaps in `make_aware`

### Ambiguous

`cb08d5042` reliable prefork detection. It reintroduces the `worker` argument to
`DjangoWorkerFixup.__init__` that `9d6ab110d` and `8ea903b6d` removed. Read the upstream history
before deciding which shape is the current one. With no prefork here it may be moot either way.
