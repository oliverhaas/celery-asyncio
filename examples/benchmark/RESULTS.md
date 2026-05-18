# Benchmark Results

**Date:** 2026-05-18
**Hardware:** Worker pinned to 4 cores (Intel i7-8565U @ 1.80GHz) via `taskset -c 0,1,2,3`
**Broker / backend:** valkey 8 on `localhost:6379` (via docker-compose)
**kombu-asyncio:** `main` @ `ac52b681` (long-lived consumer iterations + restore-on-cancel)
**celery-asyncio:** `main` @ `fb742fe27`

The bench now runs three workload profiles and three event-loop variants per
config. Every worker logs `[celeryapp] BENCH_UVLOOP=…, event loop: …` at
startup; we verified all 54 runs activated the expected loop class.

**Workload profiles** (selectable via `run_all.py --profile`):

- `mixed` (default, 30 000 tasks): the historical balanced mix —
  io_heavy 40 %, cpu_heavy 30 %, mem_heavy 20 %, balanced 10 %.
- `cpu-only` (1 000 tasks): 100 % cpu_heavy (`cpu_iters=20 000` ≈ 5 ms
  pure-Python compute per task). Stresses the GIL.
- `io-only` (500 tasks): 100 % I/O sleep (`io_seconds=0.5`, realistic
  outbound API latency). Stresses concurrency.

**Event-loop variants** (toggled via `BENCH_UVLOOP=1`):

- Default CPython asyncio (`_UnixSelectorEventLoop`)
- uvloop 0.22.1 (libuv-backed `uvloop.Loop`)

## Strand bug fixed

The May 14 mixed-workload table reported 21–48 stranded tasks per aio config
(0.07–0.16 %). That was a Redis-transport cancellation race in
`Channel.drain_events` (see kombu-asyncio
[#4](https://github.com/oliverhaas/kombu-asyncio/pull/4)). Long-lived
consumer iterations now eliminate the race in the hot path, and
restore-on-cancel handles the remaining cold paths.

**Result: `n_stranded = 0` on all 54 runs of this matrix.**

## Methodology notes

* Each row is a single 30 000 / 1 000 / 500-task run on a laptop, not a
  dedicated bench machine. **Wall and TPS are stable across reruns
  (±5–10 %); `mean_cpu` is noisier** because the psutil sampler depends
  on which child processes psutil sees at sample time and on overall
  system load.
* Worker subprocesses run in their own session and inherit the broker
  URL. **Always check `mingle: all alone` in the worker log before
  trusting numbers** — an orphaned worker from a previous bench run will
  steal tasks and tank the throughput of the worker under test. This
  bug bit us once; the kill-everything check is now part of pre-flight.
* `mean_cpu` is summed across CPU cores. On the 4-core taskset, 100 %
  ≈ one core saturated; 400 % ≈ all four cores saturated.

## Full results — mixed workload

Sorted by wall-clock.

| config                  | py    | variant | workers              | wall    | TPS    | peak RSS | mean RSS | peak CPU | mean CPU | stranded |
| ----------------------- | ----- | ------- | -------------------- | ------: | -----: | -------: | -------: | -------: | -------: | -------: |
| classic-prefork4-314    | 3.14  | sync    | prefork × 4          | 208.3 s |  144.0 |  284 MB  |  284 MB  |    51 %  |    20 %  |    0     |
| aio-async-l4c25-314     | 3.14  | async   | 4 loop × 25 + 1 sync | 209.6 s |  143.1 |  108 MB  |  100 MB  |    97 %  |    59 %  |    0     |
| aio-async-l4c25-uvloop-314 | 3.14  | async   | 4 loop × 25 + 1 sync | 211.2 s |  142.0 |  120 MB  |  112 MB  |    96 %  |    65 %  |    0     |
| classic-prefork4-314t   | 3.14t | sync    | prefork × 4          | 215.2 s |  139.4 |  344 MB  |  333 MB  |    33 %  |    20 %  |    0     |
| aio-mixed-l2c50-s2-314  | 3.14  | mixed   | 2 loop × 50 + 2 sync | 219.3 s |  136.8 |  105 MB  |   99 MB  |    84 %  |    58 %  |    0     |
| aio-sync-s4-314         | 3.14  | sync    | 4 sync threads       | 221.9 s |  135.2 |  106 MB  |  100 MB  |    83 %  |    57 %  |    0     |
| aio-mixed-l2c50-s2-uvloop-314 | 3.14  | mixed   | 2 loop × 50 + 2 sync | 228.0 s |  131.6 |  111 MB  |  102 MB  |    85 %  |    61 %  |    0     |
| aio-sync-s4-uvloop-314  | 3.14  | sync    | 4 sync threads       | 228.9 s |  131.1 |  110 MB  |  101 MB  |    79 %  |    60 %  |    0     |
| aio-sync-s4-uvloop-314t | 3.14t | sync    | 4 sync threads       | 237.3 s |  126.4 |  103 MB  |   85 MB  |   124 %  |    83 %  |    0     |
| aio-async-l4c25-uvloop-314t | 3.14t | async   | 4 loop × 25 + 1 sync | 241.0 s |  124.5 |  106 MB  |   87 MB  |   123 %  |    86 %  |    0     |
| aio-mixed-l2c50-s2-uvloop-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync | 242.5 s |  123.7 |  110 MB  |   84 MB  |   120 %  |    85 %  |    0     |
| aio-async-l4c25-314t    | 3.14t | async   | 4 loop × 25 + 1 sync | 275.2 s |  109.0 |   96 MB  |   75 MB  |    84 %  |    60 %  |    0     |
| aio-sync-s4-314t        | 3.14t | sync    | 4 sync threads       | 275.2 s |  109.0 |   98 MB  |   80 MB  |    77 %  |    56 %  |    0     |
| aio-mixed-l2c50-s2-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync | 277.0 s |  108.3 |   98 MB  |   74 MB  |    79 %  |    58 %  |    0     |
| classic-threads4-314    | 3.14  | sync    | threads × 4          | 544.7 s |   55.1 |   94 MB  |   94 MB  |    95 %  |    21 %  |    0     |
| classic-threads4-314t   | 3.14t | sync    | threads × 4          | 560.2 s |   53.5 |  105 MB  |   86 MB  |   130 %  |    27 %  |    0     |
| classic-prefork1-314    | 3.14  | sync    | prefork × 1          | 846.3 s |   35.4 |  111 MB  |  111 MB  |    16 %  |     6 %  |    0     |
| classic-prefork1-314t   | 3.14t | sync    | prefork × 1          | 874.2 s |   34.3 |  135 MB  |  132 MB  |    15 %  |     6 %  |    0     |

### Observations

**prefork-4-314 still wins raw TPS at 144.0**, but `aio-async-l4c25-314`
is now within 1 % (143.1 TPS) and **`aio-async-l4c25-uvloop-314` lands at
142.0** — essentially a three-way tie at the top. The strand-bug fix
(long-lived consumer iterations) is responsible: drain_events no longer
tears down and recreates the consumer state every cycle.

**uvloop is neutral or slightly negative on 3.14.** Across all three aio
layouts, vanilla and uvloop measurements are within ±3 %. The default
CPython asyncio loop is already efficient enough that libuv's overhead
amortises only over much longer workloads than this benchmark exposes.

**uvloop helps 3.14t by +14–16 %** on multi-threaded aio configs
(108.3 → 123.7, 109.0 → 124.5, 109.0 → 126.4). Free-threaded asyncio has
extra coordination overhead that uvloop's C implementation sidesteps.

**3.14t still costs ~25 % on the mixed workload** (109 vs 143 TPS) — the
mix is I/O-dominated (60 % of tasks sleep) so free-threading's parallel-CPU
benefit doesn't fire. uvloop closes the gap to ~14 % (126 vs 144) but
doesn't eliminate it.

**Memory remains aio's strength.** Peak RSS is 75–120 MB across every aio
config vs 284–344 MB for prefork-4. classic-threads4 sits between at
86–105 MB but at less than half the TPS.

## Full results — cpu-only workload

Sorted by wall-clock.

| config                  | py    | variant | workers              | wall    | TPS    | peak RSS | mean RSS | peak CPU | mean CPU | stranded |
| ----------------------- | ----- | ------- | -------------------- | ------: | -----: | -------: | -------: | -------: | -------: | -------: |
| classic-prefork4-314    | 3.14  | sync    | prefork × 4          |   8.6 s |  116.2 |  253 MB  |  253 MB  |    42 %  |    16 %  |    0     |
| classic-prefork4-314t   | 3.14t | sync    | prefork × 4          |  11.2 s |   89.3 |  311 MB  |  310 MB  |    22 %  |    12 %  |    0     |
| aio-async-l4c25-314t    | 3.14t | async   | 4 loop × 25 + 1 sync |  12.6 s |   79.2 |   73 MB  |   70 MB  |   402 %  |   376 %  |    0     |
| aio-sync-s4-314t        | 3.14t | sync    | 4 sync threads       |  12.6 s |   79.2 |   61 MB  |   60 MB  |   401 %  |   378 %  |    0     |
| aio-mixed-l2c50-s2-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync |  12.8 s |   78.2 |   66 MB  |   64 MB  |   400 %  |   374 %  |    0     |
| aio-async-l4c25-uvloop-314t | 3.14t | async   | 4 loop × 25 + 1 sync |  13.2 s |   75.7 |   72 MB  |   70 MB  |   402 %  |   381 %  |    0     |
| aio-sync-s4-uvloop-314t | 3.14t | sync    | 4 sync threads       |  13.2 s |   75.5 |   65 MB  |   64 MB  |   401 %  |   377 %  |    0     |
| aio-mixed-l2c50-s2-uvloop-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync |  13.3 s |   75.4 |   71 MB  |   68 MB  |   401 %  |   377 %  |    0     |
| classic-threads4-314t   | 3.14t | sync    | threads × 4          |  18.9 s |   52.8 |   71 MB  |   70 MB  |   401 %  |   228 %  |    0     |
| classic-prefork1-314    | 3.14  | sync    | prefork × 1          |  25.0 s |   40.0 |  103 MB  |  103 MB  |     6 %  |     4 %  |    0     |
| aio-sync-s4-314         | 3.14  | sync    | 4 sync threads       |  26.0 s |   38.5 |   44 MB  |   44 MB  |   103 %  |   100 %  |    0     |
| aio-mixed-l2c50-s2-314  | 3.14  | mixed   | 2 loop × 50 + 2 sync |  26.1 s |   38.4 |   44 MB  |   44 MB  |   103 %  |   100 %  |    0     |
| classic-threads4-314    | 3.14  | sync    | threads × 4          |  26.2 s |   38.1 |   53 MB  |   53 MB  |   103 %  |   100 %  |    0     |
| aio-async-l4c25-314     | 3.14  | async   | 4 loop × 25 + 1 sync |  26.3 s |   38.0 |   44 MB  |   44 MB  |   103 %  |   100 %  |    0     |
| aio-sync-s4-uvloop-314  | 3.14  | sync    | 4 sync threads       |  27.1 s |   36.9 |   47 MB  |   47 MB  |   103 %  |   100 %  |    0     |
| aio-mixed-l2c50-s2-uvloop-314 | 3.14  | mixed   | 2 loop × 50 + 2 sync |  27.2 s |   36.8 |   47 MB  |   47 MB  |   103 %  |   100 %  |    0     |
| aio-async-l4c25-uvloop-314 | 3.14  | async   | 4 loop × 25 + 1 sync |  27.3 s |   36.7 |   48 MB  |   48 MB  |   104 %  |   100 %  |    0     |
| classic-prefork1-314t   | 3.14t | sync    | prefork × 1          |  33.1 s |   30.2 |  127 MB  |  126 MB  |     6 %  |     3 %  |    0     |

### Observations

**Free-threading on CPU-bound workloads delivers ~2× speedup** to every
multi-threaded aio config:

| config             | 3.14 TPS | 3.14t TPS | speedup |
| ------------------ | -------: | --------: | ------: |
| aio-sync-s4        |     38.5 |      79.2 |   2.06× |
| aio-async-l4c25    |     38.0 |      79.2 |   2.08× |
| aio-mixed-l2c50-s2 |     38.4 |      78.2 |   2.04× |
| classic-threads4   |     38.1 |      52.8 |   1.39× |

The 3.14t aio configs hit `mean_cpu ≈ 375 %` — all four pinned cores
running Python in parallel. Vanilla 3.14 caps at 100 % (GIL serialises).
`classic-threads4-314t` only reaches 228 % because celery's pure-Python
paths re-serialise via internal locks even under free-threading.

**prefork-4-314 still wins at 116 TPS**, 1.46 × the best free-threaded
aio config. Multi-process sidesteps the GIL completely and pays no
thread-coordination tax.

**Free-threading hurts single-threaded configs.** `prefork-4-314t`
(78.6 → 89.3 TPS in repeated runs; here 89.3) is **23 % slower** than
prefork-4-314 (116.2). Each prefork child is single-threaded, so
free-threading's per-thread interpreter overhead applies without any
parallel-CPU benefit.

**uvloop neutral on CPU-only.** All variants within ±3 % of their
non-uvloop counterparts. CPU-bound work spends almost no time in the
event loop, so libuv has nothing to optimise.

## Full results — io-only workload

Sorted by wall-clock.

| config                  | py    | variant | workers              | wall    | TPS    | peak RSS | mean RSS | peak CPU | mean CPU | stranded |
| ----------------------- | ----- | ------- | -------------------- | ------: | -----: | -------: | -------: | -------: | -------: | -------: |
| aio-async-l4c25-314     | 3.14  | async   | 4 loop × 25 + 1 sync |   3.1 s |  159.4 |   46 MB  |   46 MB  |    43 %  |    36 %  |    0     |
| aio-async-l4c25-uvloop-314 | 3.14  | async   | 4 loop × 25 + 1 sync |   3.3 s |  150.0 |   50 MB  |   49 MB  |    38 %  |    34 %  |    0     |
| aio-async-l4c25-uvloop-314t | 3.14t | async   | 4 loop × 25 + 1 sync |   3.9 s |  129.3 |   64 MB  |   63 MB  |    41 %  |    29 %  |    0     |
| aio-async-l4c25-314t    | 3.14t | async   | 4 loop × 25 + 1 sync |   4.9 s |  103.0 |   63 MB  |   61 MB  |    33 %  |    24 %  |    0     |
| aio-mixed-l2c50-s2-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync |  63.1 s |    7.9 |   63 MB  |   62 MB  |    34 %  |     3 %  |    0     |
| aio-mixed-l2c50-s2-uvloop-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync |  63.2 s |    7.9 |   65 MB  |   64 MB  |    33 %  |     3 %  |    0     |
| aio-sync-s4-uvloop-314t | 3.14t | sync    | 4 sync threads       |  63.2 s |    7.9 |   66 MB  |   66 MB  |    24 %  |     4 %  |    0     |
| aio-sync-s4-uvloop-314  | 3.14  | sync    | 4 sync threads       |  63.2 s |    7.9 |   51 MB  |   51 MB  |    23 %  |     3 %  |    0     |
| classic-threads4-314t   | 3.14t | sync    | threads × 4          |  63.2 s |    7.9 |   71 MB  |   71 MB  |    14 %  |     4 %  |    0     |
| aio-sync-s4-314         | 3.14  | sync    | 4 sync threads       |  63.2 s |    7.9 |   48 MB  |   48 MB  |    27 %  |     3 %  |    0     |
| aio-mixed-l2c50-s2-uvloop-314 | 3.14  | mixed   | 2 loop × 50 + 2 sync |  63.3 s |    7.9 |   50 MB  |   50 MB  |    32 %  |     3 %  |    0     |
| classic-prefork4-314    | 3.14  | sync    | prefork × 4          |  63.3 s |    7.9 |  253 MB  |  253 MB  |    44 %  |     4 %  |    0     |
| aio-sync-s4-314t        | 3.14t | sync    | 4 sync threads       |  63.3 s |    7.9 |   64 MB  |   63 MB  |    16 %  |     4 %  |    0     |
| aio-mixed-l2c50-s2-314  | 3.14  | mixed   | 2 loop × 50 + 2 sync |  63.3 s |    7.9 |   47 MB  |   47 MB  |    40 %  |     3 %  |    0     |
| classic-threads4-314    | 3.14  | sync    | threads × 4          |  63.3 s |    7.9 |   54 MB  |   54 MB  |    72 %  |     4 %  |    0     |
| classic-prefork4-314t   | 3.14t | sync    | prefork × 4          |  63.4 s |    7.9 |  311 MB  |  310 MB  |     8 %  |     3 %  |    0     |
| classic-prefork1-314    | 3.14  | sync    | prefork × 1          | 252.6 s |    2.0 |  103 MB  |  103 MB  |     4 %  |     1 %  |    0     |
| classic-prefork1-314t   | 3.14t | sync    | prefork × 1          | 252.6 s |    2.0 |  127 MB  |  127 MB  |     4 %  |     1 %  |    0     |

### Observations

**This is what asyncio is for.** `aio-async-l4c25-314` finishes 500
tasks of 500 ms each in **3.1 s at 159.4 TPS — 20 × faster** than any
4-slot config (all bunched at 7.9 TPS / 63 s) and **80 × faster** than
prefork-1 (2.0 TPS / 252 s). With 100 in-flight async slots the workload
is over before the broker can even ack the first batch synchronously.

**Sync threads can't unlock I/O concurrency.** `aio-sync-s4`,
`aio-mixed-l2c50-s2`, `classic-threads4`, `classic-prefork4` — and even
`aio-mixed`'s mixed dispatch — all land at exactly 7.9 TPS. The
`mixed-async` task body uses `await asyncio.sleep`, but when dispatched
through a sync executor (4 worker threads) it falls back to
`time.sleep`, which blocks the thread. **Mixed pool does not dynamically
rebalance work between sync and async slots** — sync-variant tasks always
go to sync threads, async-variant tasks always go to loop slots — so
mixed gives you the worse of the two for any single-variant workload.

**uvloop is slightly slower on aio-async-314** (159.4 → 150.0 TPS,
−6 %). Default asyncio's selector is well-optimised for the broker's
small message volume; libuv's bookkeeping is overhead the workload
doesn't need.

**uvloop helps aio-async-314t** (103.0 → 129.3 TPS, +25 %) — the same
free-threading-asyncio interaction that helped on mixed. The 3.14t async
pool is the worst-affected config in the matrix; uvloop recovers most
(but not all) of the ground vs vanilla 3.14.

**Memory blowout for prefork on I/O work.** `classic-prefork4-314` at
253 MB peak (4 processes × ~63 MB) is mostly idle (mean CPU 4 %) but
holds 5 × the RSS of an aio-async config and finishes 20 × slower.

## Cost per task (AWS Fargate, Linux/x86, us-east-1)

**Pricing reference (May 2026):** $0.04048 per vCPU·h, $0.004445 per
GB·h. Per-second: ~$1.124 × 10⁻⁵ per vCPU·s, ~$1.235 × 10⁻⁶ per GB·s.

**Two cost models:**

- **Idealised** — pay only for resources actually consumed:
  `cost = (mean_cpu × wall × vCPU·s) + (mean_rss × wall × GB·s)`.
- **Provisioned** — Fargate slot sized to peak load: CPU rounded up to
  {0.25, 0.5, 1, 2, 4, 8} vCPU based on `peak_cpu`; memory rounded up to
  the next 0.5 GiB above `peak_rss × 1.25`.

### Mixed — sorted by idealised cost

| config                  | TPS   | vCPU·s / 1 k | MB·s / 1 k |  ideal $/1M |  prov $/1M | Fargate slot       |
| ----------------------- | ----: | -----------: | ---------: | ----------: | ---------: | ------------------ |
| classic-prefork4-314    | 144.0 |        1.36  |    1 974   |   **$0.02** |     $0.08  | 1 vCPU / 0.5 GB    |
| classic-prefork4-314t   | 139.4 |        1.42  |    2 389   |     $0.02   |     $0.04  | 0.5 vCPU / 0.5 GB  |
| classic-prefork1-314    |  35.4 |        1.66  |    3 134   |     $0.02   |     $0.10  | 0.25 vCPU / 0.5 GB |
| classic-prefork1-314t   |  34.3 |        1.66  |    3 852   |     $0.02   |     $0.10  | 0.25 vCPU / 0.5 GB |
| classic-threads4-314    |  55.1 |        3.85  |    1 708   |     $0.05   |     $0.22  | 1 vCPU / 0.5 GB    |
| aio-async-l4c25-314     | 143.1 |        4.12  |      699   |     $0.05   |     $0.08  | 1 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-314  | 136.8 |        4.22  |      721   |     $0.05   |     $0.09  | 1 vCPU / 0.5 GB    |
| aio-sync-s4-314         | 135.2 |        4.23  |      739   |     $0.05   |     $0.09  | 1 vCPU / 0.5 GB    |
| aio-sync-s4-uvloop-314  | 131.1 |        4.55  |      772   |     $0.05   |     $0.09  | 1 vCPU / 0.5 GB    |
| aio-async-l4c25-uvloop-314 | 142.0 |        4.55  |      788   |     $0.05   |     $0.08  | 1 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-uvloop-314 | 131.6 |        4.61  |      775   |     $0.05   |     $0.09  | 1 vCPU / 0.5 GB    |
| classic-threads4-314t   |  53.5 |        5.00  |    1 599   |     $0.06   |     $0.43  | 2 vCPU / 0.5 GB    |
| aio-sync-s4-314t        | 109.0 |        5.11  |      734   |     $0.06   |     $0.11  | 1 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-314t | 108.3 |        5.36  |      687   |     $0.06   |     $0.11  | 1 vCPU / 0.5 GB    |
| aio-async-l4c25-314t    | 109.0 |        5.49  |      688   |     $0.06   |     $0.11  | 1 vCPU / 0.5 GB    |
| aio-sync-s4-uvloop-314t | 126.4 |        6.57  |      672   |     $0.07   |     $0.18  | 2 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-uvloop-314t | 123.7 |        6.84  |      676   |     $0.08   |     $0.19  | 2 vCPU / 0.5 GB    |
| aio-async-l4c25-uvloop-314t | 124.5 |        6.95  |      698   |     $0.08   |     $0.19  | 2 vCPU / 0.5 GB    |

**prefork still wins $/task by ~3×** on Fargate-ideal pricing. Same
observation as May 14: aio trades CPU for concurrency, and on this
I/O-share-of-60 % mix the prefork worker really is idle during
`time.sleep`. aio (with or without uvloop) bills CPU even during async
sleep because the event loop is servicing other coroutines.

**uvloop adds 10–20 % to idealised CPU cost on 3.14** — the loop is
doing more work per second of wall, not less. On 3.14t the extra TPS
roughly cancels the extra CPU, leaving cost flat at $0.06–0.08/M.

### Mixed — at 800 MB per worker process

The bench app is bare-bones (~75–120 MB RSS). A typical
Django/Celery worker with ORM cache, app code, and framework imports
loaded sits closer to 800 MB. For prefork, multiply by the number of
children; aio/threads keep one process.

| config                  | procs | RSS modelled | k8s m6i 60%  | k8s m7g 60%  |
| ----------------------- | ----: | -----------: | -----------: | -----------: |
| classic-prefork4-314    |   4   |    3 200 MB  |  **$0.039**  |  **$0.033**  |
| classic-prefork4-314t   |   4   |    3 200 MB  |    $0.041    |    $0.035    |
| classic-prefork1-314    |   1   |      800 MB  |    $0.045    |    $0.039    |
| classic-prefork1-314t   |   1   |      800 MB  |    $0.046    |    $0.039    |
| classic-threads4-314    |   1   |      800 MB  |    $0.085    |    $0.072    |
| aio-async-l4c25-314     |   1   |      800 MB  |    $0.085    |    $0.073    |
| aio-mixed-l2c50-s2-314  |   1   |      800 MB  |    $0.088    |    $0.074    |
| aio-sync-s4-314         |   1   |      800 MB  |    $0.088    |    $0.075    |
| aio-async-l4c25-uvloop-314 |   1   |      800 MB  |    $0.094    |    $0.080    |
| aio-sync-s4-uvloop-314  |   1   |      800 MB  |    $0.094    |    $0.080    |
| aio-mixed-l2c50-s2-uvloop-314 |   1   |      800 MB  |    $0.095    |    $0.081    |
| aio-sync-s4-314t        |   1   |      800 MB  |    $0.106    |    $0.090    |
| classic-threads4-314t   |   1   |      800 MB  |    $0.108    |    $0.092    |
| aio-mixed-l2c50-s2-314t |   1   |      800 MB  |    $0.111    |    $0.095    |
| aio-async-l4c25-314t    |   1   |      800 MB  |    $0.114    |    $0.097    |
| aio-sync-s4-uvloop-314t |   1   |      800 MB  |    $0.135    |    $0.115    |
| aio-mixed-l2c50-s2-uvloop-314t |   1   |      800 MB  |    $0.140    |    $0.119    |
| aio-async-l4c25-uvloop-314t |   1   |      800 MB  |    $0.143    |    $0.121    |

(`k8s m6i` and `k8s m7g` use `m6i.large` / Graviton `m7g.large` hourly
pricing split 9.1 : 1 between CPU and memory and scaled by 1 / 0.60 for
realistic cluster utilisation.)

**At 800 MB per process, prefork-4 (4 × 800 MB = 3.2 GB) still wins
~2 ×** over the best aio config. The crossover moved in slightly compared
to last month — see below.

### Crossover memory (mixed)

Solving `prefork-4-314` vs `aio-async-l4c25-314` for break-even:

- aio extra CPU per task: `0.00412 − 0.00136 = 0.00276 vCPU·s/task` → +$3.1 × 10⁻⁸
- prefork extra memory per task: `(4·M − M) × 209.6 / 30 000 = 0.0210 · M MB·s`

Setting equal: **M ≈ 1 200 MB per process**. Below that prefork is
cheaper; above that aio is cheaper.

| RSS / process | prefork-4 $/1M | aio-async $/1M | winner            |
| ------------: | -------------: | -------------: | ----------------- |
|        100 MB |        $0.020  |     $0.061     | prefork (3.0 ×)   |
|        800 MB |        $0.039  |     $0.085     | prefork (2.2 ×)   |
|      1 200 MB |        $0.051  |     $0.096     | prefork (1.9 ×)   |
|      1 500 MB |        $0.060  |     $0.104     | prefork (1.7 ×)   |
|      2 500 MB |        $0.090  |     $0.131     | prefork (1.5 ×)   |
|      5 000 MB |        $0.166  |     $0.198     | prefork (1.2 ×)   |
|     10 000 MB |        $0.318  |     $0.331     | prefork (1.04 ×)  |
|     15 000 MB |        $0.470  |     $0.463     | aio (1.02 ×)      |

asyncio's break-even is now around **~14 GB RSS per worker** on this
laptop's CPU/memory mix — much higher than the May 14 estimate (~2.5 GB)
because the new long-lived consumer iterations make aio's CPU much more
efficient, narrowing aio's CPU disadvantage but also widening the RSS
range where prefork still wins.

### CPU-only — sorted by idealised cost

| config                  | TPS   | vCPU·s / 1 k | MB·s / 1 k |  ideal $/1M |  prov $/1M | Fargate slot       |
| ----------------------- | ----: | -----------: | ---------: | ----------: | ---------: | ------------------ |
| classic-prefork1-314    |  40.0 |        1.03  |    2 580   |   **$0.01** |     $0.09  | 0.25 vCPU / 0.5 GB |
| classic-prefork1-314t   |  30.2 |        1.13  |    4 193   |     $0.02   |     $0.11  | 0.25 vCPU / 0.5 GB |
| classic-prefork4-314    | 116.2 |        1.36  |    2 178   |     $0.02   |     $0.05  | 0.5 vCPU / 0.5 GB  |
| classic-prefork4-314t   |  89.3 |        1.31  |    3 474   |     $0.02   |     $0.04  | 0.25 vCPU / 0.5 GB |
| aio-sync-s4-314         |  38.5 |       25.91  |    1 148   |     $0.29   |     $0.60  | 2 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-314  |  38.4 |       26.18  |    1 152   |     $0.30   |     $0.60  | 2 vCPU / 0.5 GB    |
| classic-threads4-314    |  38.1 |       26.29  |    1 398   |     $0.30   |     $0.61  | 2 vCPU / 0.5 GB    |
| aio-async-l4c25-314     |  38.0 |       26.46  |    1 166   |     $0.30   |     $0.61  | 2 vCPU / 0.5 GB    |
| aio-sync-s4-uvloop-314  |  36.9 |       27.00  |    1 273   |     $0.31   |     $0.63  | 2 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-uvloop-314 |  36.8 |       27.17  |    1 287   |     $0.31   |     $0.63  | 2 vCPU / 0.5 GB    |
| aio-async-l4c25-uvloop-314 |  36.7 |       27.25  |    1 303   |     $0.31   |     $0.63  | 2 vCPU / 0.5 GB    |
| classic-threads4-314t   |  52.8 |       43.10  |    1 334   |     $0.49   |     $1.72  | 8 vCPU / 0.5 GB    |
| aio-async-l4c25-314t    |  79.2 |       47.47  |      885   |     $0.53   |     $1.14  | 8 vCPU / 0.5 GB    |
| aio-sync-s4-314t        |  79.2 |       47.72  |      757   |     $0.54   |     $1.14  | 8 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-314t |  78.2 |       47.82  |      814   |     $0.54   |     $1.16  | 8 vCPU / 0.5 GB    |
| aio-sync-s4-uvloop-314t |  75.5 |       49.89  |      847   |     $0.56   |     $1.20  | 8 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-uvloop-314t |  75.4 |       50.01  |      906   |     $0.56   |     $1.20  | 8 vCPU / 0.5 GB    |
| aio-async-l4c25-uvloop-314t |  75.7 |       50.28  |      920   |     $0.57   |     $1.20  | 8 vCPU / 0.5 GB    |

**Prefork dominates CPU-only at 15–30 × the cost-efficiency** of every
other config. CPU-bound work has no I/O to hide behind, so every vCPU·s
the worker burns shows up directly. Each prefork child has its own GIL,
so it just executes Python at native speed without inter-thread
coordination overhead.

**Free-threading doubles TPS but also doubles total CPU consumption** —
$/task stays roughly flat at the bottom of the table. The win is "more
work per process," not "less compute per task."

**Provisioned cost is brutal for free-threaded aio** — peak CPU hits
402 % so the Fargate slot rounds up to 8 vCPU even though average
utilisation is far lower. If you can use Fargate-burstable / k8s
burstable (idealised model), 314t aio costs ~10 % less than provisioned
314 aio. If you have to provision for peak, free-threaded ends up more
expensive.

**uvloop neutral.** All variants within ±3 % of the corresponding
non-uvloop entry.

### I/O-only — sorted by idealised cost

| config                  | TPS   | vCPU·s / 1 k | MB·s / 1 k |  ideal $/1M |  prov $/1M | Fargate slot       |
| ----------------------- | ----: | -----------: | ---------: | ----------: | ---------: | ------------------ |
| aio-async-l4c25-uvloop-314 | 150.0 |        2.23  |      327   |   **$0.03** |     $0.04  | 0.5 vCPU / 0.5 GB  |
| aio-async-l4c25-314     | 159.4 |        2.25  |      287   |     $0.03   |     $0.04  | 0.5 vCPU / 0.5 GB  |
| aio-async-l4c25-uvloop-314t | 129.3 |        2.25  |      486   |     $0.03   |     $0.05  | 0.5 vCPU / 0.5 GB  |
| aio-async-l4c25-314t    | 103.0 |        2.28  |      594   |     $0.03   |     $0.06  | 0.5 vCPU / 0.5 GB  |
| aio-mixed-l2c50-s2-uvloop-314 |   7.9 |        3.54  |    6 351   |     $0.05   |     $0.79  | 0.5 vCPU / 0.5 GB  |
| aio-mixed-l2c50-s2-uvloop-314t |   7.9 |        3.66  |    8 146   |     $0.05   |     $0.79  | 0.5 vCPU / 0.5 GB  |
| aio-mixed-l2c50-s2-314t |   7.9 |        3.79  |    7 891   |     $0.05   |     $0.79  | 0.5 vCPU / 0.5 GB  |
| aio-mixed-l2c50-s2-314  |   7.9 |        4.05  |    5 975   |     $0.05   |     $0.79  | 0.5 vCPU / 0.5 GB  |
| aio-sync-s4-314         |   7.9 |        4.17  |    6 107   |     $0.05   |     $0.79  | 0.5 vCPU / 0.5 GB  |
| aio-sync-s4-uvloop-314  |   7.9 |        4.17  |    6 459   |     $0.05   |     $0.43  | 0.25 vCPU / 0.5 GB |
| aio-sync-s4-314t        |   7.9 |        4.56  |    7 987   |     $0.06   |     $0.43  | 0.25 vCPU / 0.5 GB |
| aio-sync-s4-uvloop-314t |   7.9 |        4.55  |    8 291   |     $0.06   |     $0.43  | 0.25 vCPU / 0.5 GB |
| classic-threads4-314t   |   7.9 |        5.31  |    8 927   |     $0.07   |     $0.43  | 0.25 vCPU / 0.5 GB |
| classic-threads4-314    |   7.9 |        5.57  |    6 828   |     $0.07   |     $1.50  | 1 vCPU / 0.5 GB    |
| classic-prefork4-314    |   7.9 |        4.43  |   31 973   |     $0.09   |     $0.79  | 0.5 vCPU / 0.5 GB  |
| classic-prefork4-314t   |   7.9 |        3.93  |   39 382   |     $0.09   |     $0.43  | 0.25 vCPU / 0.5 GB |
| classic-prefork1-314    |   2.0 |        5.05  |   52 040   |     $0.12   |     $1.73  | 0.25 vCPU / 0.5 GB |
| classic-prefork1-314t   |   2.0 |        5.56  |   63 958   |     $0.14   |     $1.73  | 0.25 vCPU / 0.5 GB |

**For I/O-bound work, aio-async is both fastest and cheapest.** That's
the only ranking flip relative to mixed/cpu-only. With most of the worker
sitting in `asyncio.sleep` the event loop costs almost nothing, and the
massive TPS lead amortises broker/setup overhead across vastly more tasks.

**Prefork's per-process memory bill bites hard.** `classic-prefork4-314`
spends 32 000 MB·s per 1 000 tasks vs aio-async's 287 — a 110 × gap. RSS
is the same as on the mixed workload (~250 MB) but wall is 20 × longer,
so integrated memory·time is 20 × larger too.

**aio-sync / aio-mixed / classic-threads4 / classic-prefork4** all
converge at $0.05–0.09 / M, separated mostly by the memory term. Same
in-flight cap of 4 slots = same wall = same fundamental cost; the
differences are noise in the CPU sampler.

## Summary

| profile  | best $/task                            | best TPS                  | when to use |
| -------- | -------------------------------------- | ------------------------- | ----------- |
| mixed    | prefork-4-314 ($0.02/M)                | prefork-4-314 (144 TPS)   | most production workloads — prefork's per-process GIL avoidance wins as long as worker RSS stays under ~1.2 GB; above that, the gap narrows |
| cpu-only | prefork-1-314 ($0.01/M)                | prefork-4-314 (116 TPS)   | embarrassingly-parallel CPU work — prefork is dominant. Free-threading lets aio match prefork on TPS at half the RSS, but doesn't reduce $/task |
| io-only  | aio-async-l4c25-314 ($0.03/M)          | aio-async-l4c25-314 (159 TPS) | high-latency outbound I/O — aio's 100-slot async pool finishes 20 × faster than 4-slot configs |

### When to pick uvloop

- **3.14 + asyncio:** neutral or slightly slower — skip it. The default
  selector loop is already well-optimised for celery's broker traffic.
- **3.14t + asyncio:** worth it — uvloop closes 60 % of the 3.14 → 3.14t
  performance gap on the mixed workload. Adds ~7 MB RSS, no other downside.

## Known issues surfaced

1. **Orphaned worker contamination.** Killing a runner during a benchmark
   leaves its `start_new_session=True` celery worker child alive. That
   stray worker keeps consuming from the broker queue, so the *next*
   bench config gets ~half its tasks stolen by the ghost. Pre-flight
   check: `ps -ef | grep "celery.*worker"` should return nothing before
   starting a sweep, and each worker log's `mingle:` line should say
   `all alone`, not `sync with N nodes`. We hit this once; the bench
   numbers above are post-cleanup.

2. **Free-threaded 3.14t penalty on the mixed workload.** ~25 % wall-clock
   slower than 3.14 on every aio config (109 vs 143 TPS). The mix is
   60 % I/O-bound; free-threading's per-thread interpreter overhead
   applies on every thread but the parallel-CPU benefit doesn't fire.
   uvloop closes most of this gap (124–126 TPS), so 3.14t aio + uvloop
   is the right combination if you're committed to 3.14t.

3. **aio-mixed pool doesn't dynamically rebalance.** `--variant mixed`
   alternates sync/async tasks across the 2 sync threads + 2 loop
   workers. On a workload that's all-one-variant (io-only or cpu-only),
   the mixed pool performs identically to its 4-slot sync equivalent
   because the rebalancing never kicks in.
