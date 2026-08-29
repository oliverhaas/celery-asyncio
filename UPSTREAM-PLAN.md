# Tracking upstream celery

This fork branched from [celery/celery](https://github.com/celery/celery) at `5fd0b3e00`
(v5.6.0rc1, 2025-11-02). Upstream is at `0ec260de7` (2026-08-26). 121 non-merge commits since
then touch `celery/`; 92 of those touch a file that still exists here.

This file records what was decided for each one, so the next sweep starts from `0ec260de7` and
nothing gets re-triaged from scratch. The kombu half of the same exercise lives in
`kombu-asyncio/UPSTREAM-PLAN.md`.

**Status: the sweep is done.** Every commit that applies here has been ported and each one is
pinned by a test that fails without it. One verdict is still unwritten (`cb08d5042`, under "Still
open"), and the ports turned up ten fork-only defects and gaps along the way, listed under "Found
along the way". The last of those, one broker connection per published task, is the biggest thing
this exercise found and is not fixed yet.

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
| `e2b276e60` Mark a rejected task failed when it is not requeued | `5cd912678` | Verbatim. |
| `b8f85213f` Let a request's `ignore_result` beat the task definition | `8e61fa099` | Adapted: applied to `build_async_tracer` and `ahandle_error_state` as well, which upstream does not have. |
| `1fe2a08d0` Expose `time_limit` and `soft_time_limit` on `task.request` | `fc3cfc464` | Adapted: `Context.update()` detects a written `timelimit` by comparing the stored object's identity, since every input form `dict.update` accepts replaces it. |
| `865922abd` Dispatch the chain and callbacks on the dedup fast path | `a43f31759` | Adapted: an awaiting `_adispatch_callbacks_and_chain` twin for `build_async_tracer`, which upstream does not have. The `Reject` passthrough was added to both tracers and to `trace_task`. |
| `333a82f74` Mark revoked tasks REVOKED in the backend immediately | `66350406f` | Adapted: `amark_as_revoked` scheduled on the running loop rather than upstream's blocking call, since the pidbox handler runs on the worker's event loop. The existing schedule-or-run idiom was extracted into `_schedule()` and given the strong task reference its two copies lacked. |
| `feb789acc` Close the broken connection before reconnecting | `31866ad09` | Adapted, and only half applies: the fork's handler never calls `collect()`. `Connection.close()` is a coroutine here, so `on_connection_error_after_connected` and its call site became `async`. |
| `1d563dafb` Stop an eager chain on Ignore and Reject | `3997b6195` | Adapted: applied to `aapply` as well. It exposed a fork-only defect in `EagerResult`, fixed in `2bb2a49b1`. See "Found along the way". |
| `bf1cf69e2` Skip empty groups in chains | `45b4c9813` | Adapted, three hunks: `prepare_steps`, `group.__or__` and `_chord.freeze`. Generator-backed groups are left alone in all three, since asking whether one is empty consumes it. |
| `a094d2a89` Restore GroupResult fan-out in `chain.as_tuple()` | `45b4c9813` | Verbatim. Only `prev_task` is reset after a chord upgrade; resetting `prev_res` too collapsed consecutive groups into a single result. |
| `df57d9ab9` O(K²) message bloat in a chain of chords | `962bf44fd` | Verbatim. Upstream's size test passes pre-fix -- the chords collapse into one task, so max == min over a single element. Ours asserts the task count too. |
| `379a629dc` Keep group errbacks mutable | `3997b6195` | Adapted, and it does less than the upstream message claims: `clone(immutable=True)` never made anything immutable, because `clone()` copies `immutable` from the source signature and an `immutable` kwarg only lands in `options`. All the fix removes is a stray execution option riding into the published message. The test asserts that, not the mutability. |
| `2d560f5c1` `AsyncResult.exists()` | `2e1d9be90` | Adapted: `atask_result_exists` twins throughout, defaulting to `sync_to_async` on `Backend` and overridden natively on `RedisBackend`, plus `AsyncResult.aexists()`. The database and mongodb hunks are dropped -- neither backend exists here. |
| `6e0d68308` UUID task ids in key-value store key generation | `2c48c6594` | Verbatim. `ensure_bytes` passes a non-string through unchanged and the `bytes.join()` below then raises `TypeError`. |
| `477c816f9` One raising errback no longer halts the rest | `2c48c6594` | Verbatim. The old-style path below already got this from the group it is called through. |
| `1432d9b6c` Reach the chord body when a chained step fails | `1ff72dc00` | Adapted: applied to `amark_as_failure` as well. A chord step completes when its *body* does, so an enclosing chord waits on the body id, not the chord's own -- marking only the latter left the body PENDING and `chord_unlock` retried without bound. |
| `72e9240aa` Chord error handling when the body is a chain | `1ff72dc00` | Adapted, and only hunks 1 and 3 of 3. `prepare_steps` now sets `self.id` from the real last result, and `chord_error_from_stack` mints a `task_id` for a callback that was never frozen. Hunk 2 (an explicit `task.body.link_error(errback)` in `prepare_steps`) is **not** ported: `_chord.link_error` already links the body unconditionally on both sides of `task_allow_error_cb_on_chord_header`, and upstream's hunk only survives being a no-op because `append_to_list_option` dedupes. A parametrized test pins both halves of that -- present, and not twice. |
| `be0e2de23` Allow `redis_backend_use_ssl` with a `redis://` URL | `00ee290db` | Verbatim. Only the query string is a scheme mismatch now; the setting takes the same dict as `broker_use_ssl`, which honours it against a `redis://` URL. |
| `9f0a61c61` Sentinel ACL: the username never reached `master_for()` | `00ee290db` | Verbatim. `master_for()` opens a *new* connection to the master, so the sentinel's own credentials do not carry over to it. |
| `0472aaccb` Configurable additional connection errors | `258a2ffd6` | Adapted, and shortened: upstream's four-branch normalisation collapses to one `isinstance` check. The option is read straight off the conf rather than through the `_transport_options` cached_property -- reading it from `__init__` would materialise the cache there and freeze every other transport option at construction time. Upstream hit the same trap and fixed it inside the PR; a test pins it here. The docs hunk is dropped, there is no `backends-and-brokers/` in this fork. |
| `22a03fa13` redis-py DriverInfo | `258a2ffd6` | Adapted: upstream reaches for `redis` unconditionally, but this fork resolves the client library from the URL scheme and valkey-py has no `DriverInfo`, so the lookup goes through `self.redis` and falls back to the deprecated `lib_name`/`lib_version` pair. Both forms verified against the sync *and* async connection-pool constructors of both libraries. |
| `e49270e35` NameError from TYPE_CHECKING-only annotations | `79b5f7442` | Adapted. Upstream gates its 3.14 branch on `sys.version_info`; this fork requires 3.14, so the gate is dropped and only the new path is kept. `_getfullargspec` asks `inspect.signature` for `Format.STRING`, so lazy annotations are never evaluated, and `_get_annotations` in `app/base.py` tries the real objects first and settles for strings. Upstream fixed only `head_from_fun`; `arity_greater` and `fun_takes_argument` call the same helper here and are covered too. |
| `66bcdebb4` `fun_accepts_kwargs` evaluates annotations eagerly | `79b5f7442` | Folded into the same `_getfullargspec` helper. The shape that surfaces it is a signal receiver annotated `sender: Celery` with `Celery` imported under `TYPE_CHECKING`. |
| `4369baf04` `head_from_fun` must not follow `__wrapped__` | `79b5f7442` | Adapted as `follow_wrapped=False`, which restores `getfullargspec`'s documented behaviour. Upstream needed this as a follow-up because swapping in `inspect.signature` silently changed which callable gets introspected: a task built with `functools.wraps` over a variadic DI wrapper was validated against the *inner* signature, so `apply_async` rejected arguments the wrapper accepts. Bound methods are unwrapped to `__func__` for the same reason, to keep `self` in `args`. |
| `7ca0e0f18` `__class_getitem__` on the generic classes | `1fe8ed1f4` | Adapted: `Celery`, `Task`, `AsyncResult`, `_LocalStack`, `FallbackContext` and `class_property`. Upstream's list also names `_FastLocalStack`, which this fork does not have. Annotation support only, no runtime behaviour. |
| `4886d5d0c` preload options for the `control` command | `1fe8ed1f4` | Adapted. `control` was the one remote command whose callback took no `**kwargs`, so an app-registered preload option arrived as an unexpected keyword argument. Upstream's test passes `--workdir`, a global option that never reaches the callback either way; ours uses a real preload option, the `--ini` that `test_preload_cli.py` already exercises. |
| `7735d2ba9` friendly CLI errors instead of tracebacks | `1fe8ed1f4` | Verbatim. `handle_remote_command_error` turns a dead broker into a one-line message naming it plus `EX_UNAVAILABLE`, and anything else into a summary of the failing command. Wired into `status`, `inspect` and `control`. |
| `e7c4454f8` `datefmt` on ColorFormatter and TaskFormatter | `5d495a271` | Adapted. `datefmt` goes last in `ColorFormatter.__init__` rather than upstream's second position, so the existing positional call `ColorFormatter(fmt, False)` still means "no colour" and not "no date format". The `worker_log_datefmt` and `worker_task_log_datefmt` settings thread through `setup_logging_subsystem`, `setup_task_loggers` and `setup_handlers` as `self.datefmt if datefmt is None else datefmt`, never `or`: `""` is a valid datefmt. |
| `56d80409a` mask credentials in the `alternates` from `inspect stats` | `5d495a271` | Verbatim. `params.pop("password")` only covers the primary; the failover URLs in `alternates` still carry theirs in userinfo form. Both the string and the list form are sanitized. |
| `571efe812` warn when routing is set as a task attribute | `b0fd5dace` | Adapted. `removal="7.0"` rather than upstream's `"6.0"`, since this package *is* `6.0.0a3` and that matches the existing `remove_by` values in `defaults.py`. The check is `attr in cls.__dict__`, not `getattr`: `priority` is a `Task` class attribute *and* a `from_config` entry, so a `getattr` check would warn on every task ever bound, and warn again on every re-bind. |
| `fbd01579c` honour the task's own options in `send_task` | `f2a35cf1c` | Adapted, and placed in `_prepare_task_message` rather than `send_task`, so `asend_task` gets it too. Lookup goes through `self._tasks`, not `self.tasks`, so sending to a name this process does not know neither finalizes the app nor raises under `autofinalize=False`. Upstream's `getattr(registry, "get")` fallback is dropped, since `TaskRegistry` is a dict, but the `inspect.ismethod` guard is kept so a class left in the registry is skipped rather than called and crashed on. An `_OMITTED` sentinel keeps an explicit `expires=None` able to clear a merged-in default. |
| `cc3350ef9` avoid creating Django DB connections during cleanup | `74df3e7e4` | Verbatim as `connections.all(initialized_only=True)`. Upstream's Django < 4.1 `except TypeError` fallback is dropped; the django extra here requires 6.0. |
| `a4f9beb41` close DB pools only in prefork mode | `74df3e7e4` | Adapted to "never", by removing the `close_pool()` call outright rather than gating it on a `worker_pool` setting this fork does not have. Upstream closes the pool because each prefork child owns one and has to hand it back. This worker is a single process with one shared pool, and `_close_database` runs on every task prerun *and* postrun, so closing it here would tear the pool down between tasks and leave pooling doing nothing but adding a layer. A test pins that. |
| `c30f42ad2` handle DST gaps in `make_aware` | `40fa9525e` | Adapted. A wall clock time a spring-forward skipped over now resolves to the instant it would have been, via `dateutil.tz.resolve_imaginary`, instead of naming an hour that never existed. The gap check is a separate `_is_imaginary` helper called *before* `_is_ambiguous`, not nested inside it as upstream does: dateutil reports a gap as ambiguous too, but that is an artefact of how it compares the two folds rather than something to build on. A `ValueError` from a tz that answers `is_ambiguous` but cannot do transition arithmetic is treated as "not imaginary". Five tests, including a gap of other than an hour (Lord Howe shifts 30 minutes). |
| `1fcbf6fa4` normalize aware datetimes into the schedule timezone | `40fa9525e` | Verbatim in effect. `crontab.remaining_delta` now converts both `last_run_at` and `now` into the timezone the crontab fields are written in before matching any hour or weekday against them, and returns `last_run_at` in that frame rather than converting back. An aware `last_run_at` in another timezone is what django-celery-beat hands over; before this, the next run landed a full day out in the tests added here. |
| `f52429cfd` reheap entries that ask to wait | `7e79f7bec` | Adapted. When the heap says an entry is ready but `is_due()` says not yet, it is popped and repushed at the time it asked for, so it stops sitting on top and starving everything behind it (celery#7649). Upstream's "someone mutated the heap under us" branch is restructured as an early return. Three existing tests asserted the old `tick()` return values and were tightened rather than dropped, since the new code returns the real remaining interval where the old one returned `max_interval`. |
| `fe9457327` mark messages sent by beat | `7e79f7bec` | Adapted. `Scheduler.apply_async` adds a `celery_beat_task: True` header, which is the only way a consumer can tell a scheduled message from a hand-sent one. Written into a fresh dict rather than upstream's `options.setdefault("headers", {})[...] = ...`: `entry.options.copy()` is shallow, so mutating in place reaches through into the headers dict the schedule entry is holding and permanently marks it. Upstream has that latent bug; a test here pins that we do not. |

### Found along the way

Fork-only defects and gaps that the ports above exposed. None of them exist upstream.

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

**The async tracer is almost untested.** Before `a43f31759` the suite had no test that went through
`build_async_tracer`, `atrace_task` or `ahandle_error_state` at all, so both halves of `b8f85213f`
were written blind: every `trace.py` port has to be applied twice, by hand, with only the sync half
verifiable. `a43f31759` added an `atrace()` helper and three async tests covering the dedup fast
path, but that is three tests against roughly six hundred lines of async tracer. The sync
`test_trace.py` cases are the model -- most of them have no async twin. Still the fork's largest
coverage gap, and worth closing before the next `trace.py` sweep.

**Two test modules had been skipping silently.** `tests/unit/tasks/test_canvas.py` and
`tests/unit/utils/test_functional.py` both opened with `pytest.importorskip("pytest_subtests")`.
pytest 9 vendored subtests into `_pytest/subtests.py` and the standalone distribution is no longer
installed, so the guard skipped both modules whole -- 268 tests, for however long the venv has been
on pytest 9. Removing the guard in `3e1646513` brought all 268 back, passing unmodified. Worth
grepping for `importorskip` after any test-dependency bump.

**And a third one.** `tests/unit/app/test_app.py` opens with `pydantic = pytest.importorskip("pydantic")`,
but pydantic was in no dependency group, so the whole module went quiet -- 108 tests, not just the
handful about pydantic tasks. The fork does support them (`app/base.py` has `pydantic_wrapper` and
the `pydantic=` / `pydantic_strict=` / `pydantic_context=` / `pydantic_dump_kwargs=` task options),
and upstream imports pydantic unconditionally in its test requirements. Adding `pydantic` to the dev
group in `79b5f7442` brought all 108 back, passing. Three modules in one sweep: a module-level
`importorskip` on something nobody installs is indistinguishable from deleting the file.

**`EagerResult` had no `aget()`.** It overrides `get()` to return `None` for a task that produced no
result, but inherited `aget()` from `AsyncResult`, which reads the value straight out of the cache
and so returned the `Ignore`/`Reject` instance instead. The two halves of the API disagreed on
exactly the states `1d563dafb` is about. Fixed in `2bb2a49b1`. A fork where every sync method has an
async twin needs the twins audited whenever a sync method is *overridden*, not just when one is
added.

**One line of leaked global state was hiding 23 test failures.** `Logging.setup()` calls
`logging.captureWarnings(True)` and never switches it back off, so `warnings.showwarning` stayed
replaced for the rest of the session. From Python 3.14, `catch_warnings(record=True)` only records
while `showwarning` is the stock one, which means every `pytest.warns` after the first test that
runs the worker or beat CLI silently saw nothing. The 23 failures on main looked unrelated to each
other -- platform privilege checks, an iso8601 deprecation, the consumer's `ensure_connected`,
`test_trace_catches_exception`, the Django fixup -- and every one of them passed in isolation.
Restoring `showwarning` in the `sanity_logging_side_effects` fixture in `c5036f2d5` cleared all 23
at once. Ordering-dependent failures that pass individually are worth one bisect before they are
worth one triage.

**The barrier / `on_ready` machinery is inert.** The fork dropped vine and replaced it with a
home-grown `barrier` in `celery/utils/promises.py`. That barrier has no `finalized` gate, `finalize()`
only fires an already-empty barrier, and `_pending` is never drained -- so a barrier with pending
promises never becomes ready. `add_pending_result` / `remove_pending_result` are no-op stubs, and
`ResultSet._on_ready` only calls `on_ready()` when `backend.is_async`. Result waiting works because
it polls (`wait_for_pending` / `await_for_pending`); the callback path is vestigial. Practical
consequence: `ResultSet.then()` and `GroupResult.then()` do not fire. Any upstream commit touching
drainers, barriers or `on_ready` is a no-op here until that is either removed or made real -- see
`7eb644e52` under "Not applicable".

**Beat carried a broker connection it never used.** `Scheduler.producer` was
`self.Producer(self._ensure_connected(), auto_declare=False)`, but kombu-asyncio's
`ensure_connection` is a coroutine, so the Producer wrapped an un-awaited coroutine object and
every due task cost a RuntimeWarning. `_reset_producer` had the same problem with `close()`.
Nothing downstream ever read the producer: `app._send_task_message` is
`async_to_sync(self._asend_task_message)`, which opens its own connection and ignores the one it
is handed. The whole chain was removed in `7ba68d3ca` rather than repaired. Where upstream's
scheduler owns a connection, this one owns nothing, so a port that reaches for `self.producer`
has to be rethought rather than translated.

**One broker connection per published task, never closed.** Instrumenting `kombu.Connection` over
five `.delay()` calls gives `{'init': 7, 'connect': 5, 'close': 0}`, and the `.adelay()` path is
identical. `app._asend_task_message` calls `self.connection_for_write()` when it is passed no
connection, instead of the shared `app.async_connection` that `_prepare_task_message` already uses
and that `ensure_async_connection()` exists to hand out. Not fixed yet, and not an upstream port:
`asgiref.async_to_sync` builds a fresh event loop per call from a purely-sync caller and asyncio
transports are loop-bound, so the shared connection has to be keyed by loop rather than by app.
This is the largest single finding of the sweep, and it should land before any benchmark against
upstream celery, since connection setup would otherwise dominate what the numbers measure.

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
`ab711a0b2` broker heartbeats during graceful shutdown (prefork timer firing) ·
`8ea903b6d` defensive `pool_cls.__module__` checks. Upstream guards against a pool class that has
no `__module__`, then branches on whether it names prefork. Neither `contrib/testing/worker.py`
nor `worker/components.py` inspects `pool_cls` at all here -- there is one pool

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
`8f0842b33` redis-py < 5.3.0 compat, and the floor here is redis>=7 ·
`8eec5af31` + `f8668fcf5` a shared `reconnect_on_error` on `BaseResultConsumer`, without
`socket.timeout` in the reconnect conditions. `BaseResultConsumer` is a documented no-op stub here
and its only subclass, `valkey_redis.ResultConsumer`, is a stub too -- `start`, `stop`,
`drain_events`, `consume_from` and `cancel_for` all do nothing, so nothing would ever call it

**Subsystem absent**
`efc3a7f11` multi stopwait EPERM, there is no `apps/multi.py` ·
`5013b4a99`, `41bad6f22`, `30649dbd4`, `ba20bed77` native delayed delivery ·
`975840963` Sphinx extension ·
`730ab395c` reserved requests get the new dispatcher on reconnect, there is no
`reserved_requests`/`request.eventer` pairing here ·
`b313b6412` skip prefetch reduction under per-consumer QoS: `Tasks.start` installs a no-op
`set_prefetch_count` and computes no `qos_global`, so there is no per-consumer vs global
distinction to act on. Revisit if prefetch/QoS is ever implemented ·
`3f2cf57d3` a clear error when `fast_trace_task`'s registry is empty. The bare `ValueError` it
replaces needs a process where `setup_worker_optimizations()` never ran, which upstream hits with
the spawn prefork pool. Here `apps/worker.py` calls it in the same process the aio pool runs in,
so `_localized` and `use_fast_trace_task` are always set together. Upstream's message names
`--pool=solo` and `--pool=threads`, neither of which exists in this fork

**No working barrier to finalize**
`7eb644e52` finalize the ResultSet barrier when `iter_native` begins. Upstream's fix flips vine's
`finalized` flag so a barrier whose promises all arrived before the caller started iterating can
still fire. The fork's barrier has no such flag and never drains `_pending`, so the call would be a
provable no-op with a comment claiming otherwise. Revisit together with the vestigial `on_ready`
machinery described in "Found along the way".

**Docs, release prep, test fixtures**
`d96df921e`, `658230391`, `066092edc`, `a3f51e4c3`, `99b0a8977`, `bcc1798a8`,
`6a43c846f`, `21dbc73f8`, `cca111648`, `b446910f1`

---

## Still open

Every commit triaged as applicable has now been ported. What is left is one verdict nobody has
written down.

### Ambiguous

`cb08d5042` reliable prefork detection. It reintroduces the `worker` argument to
`DjangoWorkerFixup.__init__` that `9d6ab110d` and `8ea903b6d` removed, so that the fixup can ask
the worker which pool it is running. There is one pool here and it is not prefork, so the answer is
a constant and the argument buys nothing. Almost certainly **not applicable**; left here only
because the upstream history around it churned three times and is worth reading once before the
verdict is written down.
