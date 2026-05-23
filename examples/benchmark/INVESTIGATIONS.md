# Performance investigations

A log of things tried while chasing the gap between the bench machine's
~3.67× FT scaling on cpu-only and the ~1.14× we see on our dev hardware.
Captures what we changed, why, and what the bench said.

## Hardware-specific note

These investigations were run on a workstation with faster cores than the
bench machine in [RESULTS.md](RESULTS.md). On the bench machine,
`aio-l4c25-314t` reaches `mean_cpu ≈ 367%` (close to 4 full cores). On the
dev box, the same config caps at `mean_cpu ≈ 115%` — the 4 LoopWorker
threads are mostly idle waiting for dispatch. Faster cores finish each task
sooner, so the per-task broker round-trip cost becomes the dominant share
of wall time, and the main thread can't refill the prefetch fast enough to
keep all four workers busy.

That cap is the constraint these experiments were aimed at lifting.

## Tools

* [`profile_run.py`](profile_run.py) — wraps a worker in `py-spy record`
  and drives a workload through it. Outputs flamegraph SVG +
  speedscope JSON to `profiles/`. Useful because the bench's `run_all.py`
  starts/stops workers per config and gives no profiling hook.
* py-spy 0.4.2 **does not work on Python 3.14t free-threaded builds**
  (`failed to get gil_thread_id`). All profiles below are from the 3.14
  GIL build. The architecture observations transfer — the same threads,
  the same broker code paths, the same syscalls.

## Round 1: five LoopWorker hot-path fixes

Profile-free, based on code review of `aio.py` + `state.py` looking for
FT contention points and per-task waste.

| Fix | What | Outcome |
| --- | --- | --- |
| 1 | Cache `build_async_tracer` per task (matching the sync `__trace__` pattern) so the trace closure isn't rebuilt on every call. | **Kept.** Matches the existing sync pattern; pure cleanup. |
| 2 | Cache `prepare_accept_content(app.conf.accept_content)` on the pool. | **Kept.** Trivial, no downside. |
| 3 | Drop `state._lock` from the hot path; split counter R-M-W into a small `_counter_lock`. | **Reverted.** Justified by "dict/WeakSet ops are atomic under FT"; the WeakSet part was never verified and the change overrode a prior explicit FT-safety decision (the lock was added on purpose). Zero measurable TPS gain on the dev box. |
| 4 | Round-robin dispatch in `_pick_loop_worker` instead of `min(workers, key=_active_count)`. | **Reverted.** Sacrificed least-loaded routing (route around slow workers) for theoretical "avoids N critical-section reads under FT." Zero measurable TPS gain. |
| 5 | Drop `LoopWorker._active_count_lock`. | **Reverted.** Per-LoopWorker 2-way lock that was already trivial. Removing it makes the diagnostic counter race under FT for zero measurable TPS gain. |

**Result:** all five passed tests, none changed cpu-only TPS on the dev
box. After honest review, #3/#4/#5 were rolled back — they traded real
properties (lock safety, load-balancing, counter accuracy) for zero
measured benefit. #1 and #2 stayed because they're pure cleanups with no
downside.

## Round 2: profile + disprove

Profiled `aio-async-l4c25` on 3.14 cpu-only at 200 Hz with `--threads --idle`.

**Main thread, baseline (acks on, logs on):**
```
41.5% select
26.0% _write_sendmsg
 7.2% logging flush
 5.3% logging handle
 5.2% _write_to_self (asyncio wakeup pipe)
 5.1% _read_ready__data_received
 4.0% _read_from_self
 2.8% WatchedFileHandler.reopenIfNeeded
```

**LoopWorker thread, baseline:**
```
65% select (idle waiting for dispatch)
22% _burn_cpu (actual task work)
 7% logging
 4% asyncio wakeup pipe
```

The first read of this profile suggested **acks were the main-thread
bottleneck** (26% sendmsg) and **logging was the second** (~15%). Two
experiments to confirm:

| Experiment | Hypothesis | TPS Δ |
| --- | --- | ---: |
| Replace `_ack` closure with no-op (tasks never get acked) | If acks are the main bottleneck, TPS should jump. | **0** |
| `_does_info = False` to suppress per-task `Task X received` / `succeeded` logs | If logging is the bottleneck, TPS should jump. | **0** |
| Both at once | If both together, TPS should jump. | **0** |
| `WatchedFileHandler` → `FileHandler` (no stat-per-emit) | Removes the 2.8% reopenIfNeeded. | **0** |
| `task_ignore_result=True` | Would skip result publishing entirely. | could not measure — bench runner polls `result.ready()`, which never returns when the backend is disabled |

**Conclusion: none of the suspected per-task costs is the actual
bottleneck on this hardware.** The CPU % on each thread is what it spends
when it's *busy*, but main is 41.5% idle and each LoopWorker is 65% idle.
The system is waiting on something, not CPU-bound on it.

## Round 3: re-profile with acks + logs disabled

Removing the noise from the no-bottleneck items lets the real cost surface.

**Main thread (acks off, logs off):**
```
49.4% select (idle)
34.3% _write_sendmsg
 7.2% _write_to_self
 7.2% _read_ready__data_received
```

The 34% sendmsg is *still high* with no acks — so it's mostly the
**broker consume requests**, not acks. Each `drain_events` from kombu's
Redis transport is a non-blocking FAST consume (Lua script) or a blocking
BZMPOP, one message at a time.

**LoopWorker thread (acks off, logs off):**
```
46% select (idle)
30% _burn_cpu (compute)
17% astore_result chain (result publish)
10% _write_sendmsg (the raw socket write inside astore_result)
```

The **result store is the visible LoopWorker cost.** Reading
[`base.py:1268`](../../celery/backends/base.py) shows it does
**two Redis round-trips per task**:

1. `_aget_task_meta_for(task_id)` — GET the current meta to check
   if it's already SUCCESS (dedup against rerun-after-lost-worker races).
2. `_aset_with_state(...)` — SET the new meta.

Both `await`ed serially on the LoopWorker's loop. Plus a latent issue:
the backend's `async_client` is a single `cached_property` that all four
LoopWorker loops share. `redis.asyncio.Redis` is loop-bound; sharing across
event loops works in practice but is not the intended use and likely
internally serializes.

## Per-task wall budget (dev box, 65 TPS / 4 workers)

```
~62 ms per task per worker
  ~18 ms  _burn_cpu (compute)
~10-15 ms  result-store async overhead (2 RTTs + Python encoding)
  ~30 ms  idle waiting on asyncio scheduling between hops
```

The "idle waiting" is what's not directly visible in any one syscall —
it's the combined cost of context switches, wakeup-pipe ping-pong between
threads, and the asyncio scheduler running through its ready queue. Each
per-task hop pays a small amount; with 2-3 hops per task per worker, it
adds up.

## What didn't work, and why

* **Lock-removal fixes (Round 1)** — correct, but the threads were never
  contending on those locks on this hardware. They're work-bound, not
  lock-bound.
* **Disabling acks** — acks contribute CPU but not wall-time gating. Main
  thread has plenty of headroom (49% idle); freeing more headroom doesn't
  speed up the LoopWorkers, which are themselves idle waiting.
* **Disabling per-task logs** — same reason.
* **`WatchedFileHandler` → `FileHandler`** — only kills the per-emit
  `stat()` (2.8% of main); the `flush()` write syscall (7%) stays.

## Round 4: directions tried

### 1. Atomic result store via Lua script — landed

[`base.py:_astore_result`](../../celery/backends/base.py) does GET-then-SET
across two separate `await`s, i.e. two Redis round-trips per task. The
async paths in this backend don't pipeline. Replaced for the Redis backend
(JSON serializer only) with a Lua script that does check + write atomically
server-side in one round-trip, and **widens** the dedup rule from "only
`SUCCESS` is sticky" to "any state in
[`celery.states.READY_STATES`](../../celery/states.py)" (i.e. `SUCCESS`,
`FAILURE`, `REVOKED`):

```lua
local existing = redis.call('GET', KEYS[1])
if existing then
    local ok, decoded = pcall(cjson.decode, existing)
    if ok and type(decoded) == 'table' then
        local status = decoded.status
        if status == 'SUCCESS' or status == 'FAILURE' or status == 'REVOKED' then
            return status
        end
    end
end
local expires = tonumber(ARGV[2])
if expires and expires > 0 then
    redis.call('SETEX', KEYS[1], expires, ARGV[1])
else
    redis.call('SET', KEYS[1], ARGV[1])
end
return false
```

Lives in [`valkey_redis.py`](../../celery/backends/valkey_redis.py) as
`_ASTORE_RESULT_LUA` + a `cached_property` registered script + an
`_astore_result` override that logs `logger.error` whenever the Lua
returns an existing terminal state (i.e. a write was dropped — almost
always a redelivered task; worth surfacing because it indicates
broker/ack instability). Non-JSON serializers fall back to the base
GET+SET path (the Lua check needs `cjson`).

**Why the widened rule.** The upstream behavior — only `SUCCESS` is sticky
— is asymmetric: a redelivered task that fails the second time can
silently overwrite a previously-recorded `SUCCESS`, but a redelivered task
that succeeds the second time will overwrite a previously-recorded
`FAILURE` (which downstream consumers may already have acted on).
Terminal-state stickiness treats all completed states the same: once a
task finishes, its outcome is the outcome.

**Profile delta:** LoopWorker `_write_sendmsg` 3.7% → 1.7%, `select` idle
65% → 71%. Confirmed the second round-trip is gone.
**Bench delta on dev box:** 0 TPS (idle absorbs it; LoopWorkers were
already 65% idle before).
**Expected on bench machine:** real TPS gain — there the LoopWorkers run
at 367% mean_cpu, so a 5%-ish CPU saving translates to throughput.

**Note on alternatives.** A redis-py `pipeline()` would also get this to
one round-trip with ~5 lines of code instead of ~50, but cannot preserve
the dedup atomicity: between `pipe.get()` and `pipe.set()` (resolved
together by `pipe.execute()`), the SET is unconditional, so concurrent
writers can both observe non-terminal state and both write, losing one of
the outcomes. The Lua version makes the check + write atomic on the Redis
server side.

### 2. Per-LoopWorker async Redis client — attempted, reverted

Idea: replace the backend's `cached_property async_client` with a per-loop
cache keyed by `id(asyncio.get_running_loop())`. Each LoopWorker loop gets
its own `redis.asyncio.Redis` client with its own connection pool,
sidestepping the loop-bound nature of `redis-py`'s async pool.

Two ways this bit us, both fixed in code that's now reverted:

* **Reentrant-lock deadlock.** First implementation held `_async_lock`
  inside `_astore_result_script` and *then* called `async_client`, which
  tried to re-acquire the same lock. Fix: construct the inner object
  *outside* the lock, only hold it for the dict swap. Verified with a
  small standalone reproducer (single loop + 4 threaded loops, all
  finished).
* **Bench hangs after N tasks.** Even with the deadlock fix, the cpu-only
  bench hangs around task 300–500. The worker completes that prefix at
  ~83 TPS (faster than baseline 65), then stops succeeding while still
  receiving. No errors in the worker log. Suspected resource leak (each
  per-loop client builds its own `ConnectionPool`; subsequent
  re-construction in lost-race paths leaves the discarded pool's
  connections hanging until GC) but not confirmed.

Reverted to `cached_property` + the matching `cached_property` script.
The architectural concern remains valid; the fix is just harder than the
one-pager makes it look.

If you pick this up again:
- Wire client close into the pool's `on_stop`/`shutdown`, don't rely on GC.
- Investigate whether the issue is connection pool exhaustion (each loop
  has its own pool with default `max_connections=50`; lost-race
  constructions can stack up).
- A `weakref.WeakValueDictionary` keyed by `loop` (not `id(loop)`) might
  be cleaner — loops are hashable, and a WeakValueDictionary auto-evicts
  when the loop is GC'd.
- Consider giving the pool an explicit `max_connections` based on
  `loop_concurrency` instead of the default 50.

### 3. Skip per-task acks when `task_acks_late=False` — not attempted

When the worker is configured to ack-on-receive (the default and the bench
setting), `_ack` is still called from the trace closure on every task —
just deferred to the main loop. With `acks_late=False`, the message could
be acked the moment it leaves the broker (pre-dispatch, batched with the
consume RTT) and the per-task back-channel deleted entirely.

Skipped because:
1. The Round-2 experiment already showed that making `_ack` a no-op gives
   **zero TPS improvement on this hardware** — main thread isn't ack-bound.
2. The change spans kombu transport + celery consumer, with real risk of
   breaking restore-on-cancel semantics.

Worth revisiting if profile evidence on the bench machine (where the
threads actually run busy) shows acks as a bottleneck. On the dev box
they aren't.

## Suggested measurement protocol

For perf work specifically:

* Always re-run a clean baseline alongside any change. The bench's
  stable steady state is ~65 TPS on 314t / ~35 TPS on 314 cpu-only on the
  dev box; deviations beyond ±2 TPS are real, anything inside is noise.
* `FLUSHALL` Redis between runs. State leaks from a killed/interrupted
  run can drop TPS by 10× without any visible error — there's no
  log line that says "you have stale messages from a previous run".
* `ps -ef | grep celery.*worker` should return nothing before starting.
  Stray workers from previous runs steal tasks silently.
* py-spy only works on the GIL build. The architecture observations
  transfer to 3.14t — same broker round-trips, same wakeup pipes — but
  3.14t-specific contention (per-object locks, biased refcounting) won't
  show up until py-spy or another profiler grows FT support.
