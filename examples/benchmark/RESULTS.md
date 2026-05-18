# Benchmark Results

**Date:** 2026-05-18
**Hardware:** Worker pinned to 4 cores (Intel i7-8565U @ 1.80GHz) via `taskset -c 0,1,2,3`
**Broker / backend:** valkey 8 on `localhost:6379` (via docker-compose)
**kombu-asyncio:** `main` @ `ac52b681` (long-lived consumer iterations + restore-on-cancel)
**celery-asyncio:** `main` after sampler fix (see below)

The bench runs three workload profiles × three event-loop variants per
config. Every worker logs `[celeryapp] BENCH_UVLOOP=…, event loop: …` at
startup so uvloop activation is observable per run. We verified all 54
runs in this matrix activated the expected loop class (18 uvloop + 36
default selector) and ran `mingle: all alone` (no broker contamination).

**Workload profiles** (selectable via `run_all.py --profile`):

- `mixed` (30 000 tasks): historical balanced mix —
  io_heavy 40 %, cpu_heavy 30 %, mem_heavy 20 %, balanced 10 %.
- `cpu-only` (1 000 tasks): 100 % cpu_heavy (`cpu_iters=20 000` ≈ 5 ms
  pure-Python compute per task). Stresses the GIL.
- `io-only` (500 tasks): 100 % I/O sleep (`io_seconds=0.5` — realistic
  outbound API latency). Stresses concurrency.

**Event-loop variants** (toggled via `BENCH_UVLOOP=1`):

- Default CPython asyncio (`_UnixSelectorEventLoop`)
- uvloop 0.22.1 (libuv-backed `uvloop.Loop`)

## Methodology fix (vs prior RESULTS.md)

The sampler used to recreate `psutil.Process` objects for each child on
every iteration. `cpu_percent(interval=None)` computes the delta against
the *previous call on the same Process object*, so brand-new objects
returned 0 % every cycle. For single-process configs (aio, threads) the
parent process was primed once and the readings were accurate; **for
multi-process configs (prefork) every child read 0 % on its first
sample and the pool's CPU usage was severely undercounted** (mean_cpu
~16 % when 4 cores were actually running). The fix caches Process
objects by PID across iterations and primes new children on first
sight. The numbers below are the post-fix re-run.

What changed materially:

- `classic-prefork4-314` mixed `mean_cpu`: 20 % → **66 %**
- `classic-prefork4-314` cpu-only `mean_cpu`: 18 % → **389 %**
- `classic-prefork1-314` mixed `mean_cpu`: 6 % → **19 %**
- Idealised $/task for prefork rose ~3× — **prefork is no longer the
  dominant cost winner on mixed**; it now ties with aio configs at
  ~$0.05/M ideal Fargate.

## Strand bug fixed

The May 14 mixed-workload table reported 21–48 stranded tasks per aio
config (0.07–0.16 %). Fixed in kombu-asyncio
[#4](https://github.com/oliverhaas/kombu-asyncio/pull/4) (long-lived
consumer iterations) plus restore-on-cancel for cold paths.

**Result: `n_stranded = 0` on all 54 runs of this matrix.**

## Methodology notes

* Each row is a single run on a laptop, not a dedicated bench machine.
  **Wall and TPS are stable across reruns (±5 %); `mean_cpu` is noisier**
  because the psutil sampler depends on which child processes exist at
  sample time.
* Worker subprocesses run in their own session. Always check
  `mingle: all alone` in every worker log before trusting numbers —
  orphaned workers from previous bench runs steal tasks and silently
  tank throughput. This bug bit us once; the kill-everything pre-flight
  check is now part of the routine.
* `mean_cpu` is summed across CPU cores. On the 4-core taskset, 100 %
  ≈ one core saturated; 400 % ≈ all four cores saturated.

## Full results — mixed workload

Sorted by wall-clock.

| config                  | py    | variant | workers              | wall    | TPS    | peak RSS | mean RSS | peak CPU | mean CPU | stranded |
| ----------------------- | ----- | ------- | -------------------- | ------: | -----: | -------: | -------: | -------: | -------: | -------: |
| classic-prefork4-314    | 3.14  | sync    | prefork × 4          | 208.3 s |  144.0 |  284 MB  |  284 MB  |   133 %  |    66 %  |    0     |
| aio-async-l4c25-uvloop-314 | 3.14  | async   | 4 loop × 25 + 1 sync | 210.9 s |  142.2 |  120 MB  |  108 MB  |    97 %  |    64 %  |    0     |
| classic-prefork4-314t   | 3.14t | sync    | prefork × 4          | 215.3 s |  139.4 |  344 MB  |  332 MB  |   117 %  |    77 %  |    0     |
| aio-mixed-l2c50-s2-314  | 3.14  | mixed   | 2 loop × 50 + 2 sync | 219.6 s |  136.6 |  105 MB  |   99 MB  |    80 %  |    58 %  |    0     |
| aio-sync-s4-314         | 3.14  | sync    | 4 sync threads       | 221.4 s |  135.5 |   99 MB  |   93 MB  |    75 %  |    57 %  |    0     |
| aio-async-l4c25-314     | 3.14  | async   | 4 loop × 25 + 1 sync | 225.9 s |  132.8 |  116 MB  |  109 MB  |    90 %  |    63 %  |    0     |
| aio-mixed-l2c50-s2-uvloop-314 | 3.14  | mixed   | 2 loop × 50 + 2 sync | 228.9 s |  131.1 |  111 MB  |  103 MB  |    89 %  |    61 %  |    0     |
| aio-sync-s4-uvloop-314  | 3.14  | sync    | 4 sync threads       | 229.0 s |  131.0 |  110 MB  |  101 MB  |    82 %  |    60 %  |    0     |
| aio-async-l4c25-uvloop-314t | 3.14t | async   | 4 loop × 25 + 1 sync | 239.6 s |  125.2 |  106 MB  |   87 MB  |   118 %  |    86 %  |    0     |
| aio-sync-s4-uvloop-314t | 3.14t | sync    | 4 sync threads       | 240.5 s |  124.7 |  104 MB  |   85 MB  |   133 %  |    83 %  |    0     |
| aio-mixed-l2c50-s2-uvloop-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync | 252.7 s |  118.7 |  102 MB  |   82 MB  |   121 %  |    88 %  |    0     |
| aio-async-l4c25-314t    | 3.14t | async   | 4 loop × 25 + 1 sync | 275.0 s |  109.1 |   95 MB  |   76 MB  |    83 %  |    60 %  |    0     |
| aio-sync-s4-314t        | 3.14t | sync    | 4 sync threads       | 275.5 s |  108.9 |   98 MB  |   81 MB  |    73 %  |    56 %  |    0     |
| aio-mixed-l2c50-s2-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync | 277.0 s |  108.3 |   91 MB  |   75 MB  |    81 %  |    58 %  |    0     |
| classic-threads4-314    | 3.14  | sync    | threads × 4          | 537.1 s |   55.9 |   86 MB  |   86 MB  |    76 %  |    21 %  |    0     |
| classic-threads4-314t   | 3.14t | sync    | threads × 4          | 565.3 s |   53.1 |  105 MB  |   86 MB  |   108 %  |    26 %  |    0     |
| classic-prefork1-314    | 3.14  | sync    | prefork × 1          | 846.7 s |   35.4 |  111 MB  |  111 MB  |    46 %  |    19 %  |    0     |
| classic-prefork1-314t   | 3.14t | sync    | prefork × 1          | 874.5 s |   34.3 |  135 MB  |  132 MB  |    49 %  |    21 %  |    0     |

### Observations

**prefork-4-314 leads at 144.0 TPS, and aio-async-l4c25-uvloop-314 is
within 1 % (142.2 TPS).** Vanilla aio-async-l4c25-314 lands at 132.8 TPS,
so uvloop's +7 % helps it pass classic-prefork4 in raw throughput. The
strand-bug fix (long-lived consumer iterations) is what made this race
close — drain_events no longer tears down and recreates the consumer
state every cycle.

**uvloop is neutral on 3.14 sync/mixed pools** but **adds +15 % on
3.14t** (108.3 → 124.7, 108.9 → 125.2). Free-threaded asyncio has extra
coordination overhead in pure-Python paths that libuv's C
implementation sidesteps.

**3.14t still costs ~25 % on the mixed workload** (109 vs 144 TPS) — the
mix is 60 % I/O-bound, so free-threading's parallel-CPU benefit doesn't
fire. uvloop closes the gap to ~13 % (125 vs 144).

**Memory: aio dominates.** 75–120 MB peak RSS across every aio config
vs 284–344 MB for prefork-4. classic-threads4 sits between at 86–105 MB
but at less than half the TPS.

## Full results — cpu-only workload

Sorted by wall-clock.

| config                  | py    | variant | workers              | wall    | TPS    | peak RSS | mean RSS | peak CPU | mean CPU | stranded |
| ----------------------- | ----- | ------- | -------------------- | ------: | -----: | -------: | -------: | -------: | -------: | -------: |
| classic-prefork4-314    | 3.14  | sync    | prefork × 4          |   8.6 s |  117.0 |  253 MB  |  253 MB  |   406 %  |   389 %  |    0     |
| classic-prefork4-314t   | 3.14t | sync    | prefork × 4          |  11.8 s |   84.6 |  311 MB  |  310 MB  |   400 %  |   369 %  |    0     |
| aio-sync-s4-314t        | 3.14t | sync    | 4 sync threads       |  12.7 s |   78.4 |   60 MB  |   60 MB  |   399 %  |   377 %  |    0     |
| aio-mixed-l2c50-s2-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync |  12.8 s |   78.4 |   66 MB  |   64 MB  |   399 %  |   374 %  |    0     |
| aio-async-l4c25-314t    | 3.14t | async   | 4 loop × 25 + 1 sync |  12.9 s |   77.8 |   73 MB  |   70 MB  |   401 %  |   367 %  |    0     |
| aio-sync-s4-uvloop-314t | 3.14t | sync    | 4 sync threads       |  13.1 s |   76.6 |   66 MB  |   64 MB  |   401 %  |   377 %  |    0     |
| aio-async-l4c25-uvloop-314t | 3.14t | async   | 4 loop × 25 + 1 sync |  13.2 s |   75.5 |   73 MB  |   70 MB  |   401 %  |   380 %  |    0     |
| aio-mixed-l2c50-s2-uvloop-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync |  13.4 s |   74.7 |   70 MB  |   68 MB  |   401 %  |   368 %  |    0     |
| classic-threads4-314t   | 3.14t | sync    | threads × 4          |  19.7 s |   50.7 |   71 MB  |   70 MB  |   401 %  |   216 %  |    0     |
| classic-prefork1-314    | 3.14  | sync    | prefork × 1          |  24.9 s |   40.1 |  103 MB  |  103 MB  |   105 %  |   100 %  |    0     |
| classic-threads4-314    | 3.14  | sync    | threads × 4          |  25.7 s |   39.0 |   53 MB  |   53 MB  |   103 %  |    99 %  |    0     |
| aio-async-l4c25-314     | 3.14  | async   | 4 loop × 25 + 1 sync |  26.3 s |   38.1 |   44 MB  |   44 MB  |   103 %  |    99 %  |    0     |
| aio-mixed-l2c50-s2-314  | 3.14  | mixed   | 2 loop × 50 + 2 sync |  26.3 s |   38.0 |   44 MB  |   44 MB  |   103 %  |    99 %  |    0     |
| aio-sync-s4-314         | 3.14  | sync    | 4 sync threads       |  26.4 s |   37.9 |   44 MB  |   44 MB  |   103 %  |    99 %  |    0     |
| aio-sync-s4-uvloop-314  | 3.14  | sync    | 4 sync threads       |  27.2 s |   36.8 |   47 MB  |   47 MB  |   103 %  |    99 %  |    0     |
| aio-mixed-l2c50-s2-uvloop-314 | 3.14  | mixed   | 2 loop × 50 + 2 sync |  27.2 s |   36.7 |   48 MB  |   47 MB  |   102 %  |    99 %  |    0     |
| aio-async-l4c25-uvloop-314 | 3.14  | async   | 4 loop × 25 + 1 sync |  27.4 s |   36.5 |   48 MB  |   48 MB  |   103 %  |    98 %  |    0     |
| classic-prefork1-314t   | 3.14t | sync    | prefork × 1          |  33.0 s |   30.3 |  127 MB  |  127 MB  |   104 %  |   100 %  |    0     |

### Observations

**Free-threading delivers ~2 × speedup** to every multi-threaded aio
config — and `mean_cpu` now confirms full parallelism on all four cores:

| config             | 3.14 TPS | 3.14 mean_cpu | 3.14t TPS | 3.14t mean_cpu | speedup |
| ------------------ | -------: | ------------: | --------: | -------------: | ------: |
| aio-sync-s4        |     37.9 |          99 % |      78.4 |         377 %  |   2.07× |
| aio-async-l4c25    |     38.1 |          99 % |      77.8 |         367 %  |   2.04× |
| aio-mixed-l2c50-s2 |     38.0 |          99 % |      78.4 |         374 %  |   2.06× |
| classic-threads4   |     39.0 |          99 % |      50.7 |         216 %  |   1.30× |

`classic-threads4-314t` only reaches 216 % because celery's pure-Python
paths re-serialise via internal locks even under free-threading.
aio configs hit 367–380 % — all four pinned cores running Python in
parallel.

**prefork-4-314 still wins at 117 TPS**, 1.5 × the best free-threaded
aio. Multi-process sidesteps the GIL completely and pays no
thread-coordination tax. Now visible in `mean_cpu`: **389 %** (vs the
previously-undercounted 18 %).

**Free-threading hurts single-threaded prefork.** `prefork-4-314t`
(84.6 TPS) is **28 % slower** than prefork-4-314 (117.0). Each prefork
child is single-threaded, so free-threading's per-thread interpreter
overhead applies without any parallel-CPU benefit. Same picture in
classic-prefork1: 40 TPS on 3.14, 30 TPS on 3.14t.

**uvloop neutral.** All variants within ±3 % of the corresponding
non-uvloop entry. CPU-bound work spends almost no time in the event
loop, so libuv has nothing to optimise.

## Full results — io-only workload

Sorted by wall-clock.

| config                  | py    | variant | workers              | wall    | TPS    | peak RSS | mean RSS | peak CPU | mean CPU | stranded |
| ----------------------- | ----- | ------- | -------------------- | ------: | -----: | -------: | -------: | -------: | -------: | -------: |
| aio-async-l4c25-314     | 3.14  | async   | 4 loop × 25 + 1 sync |   3.3 s |  153.5 |   46 MB  |   46 MB  |    40 %  |    31 %  |    0     |
| aio-async-l4c25-uvloop-314 | 3.14  | async   | 4 loop × 25 + 1 sync |   3.3 s |  150.6 |   50 MB  |   49 MB  |    38 %  |    31 %  |    0     |
| aio-async-l4c25-uvloop-314t | 3.14t | async   | 4 loop × 25 + 1 sync |   3.9 s |  126.7 |   64 MB  |   63 MB  |    39 %  |    30 %  |    0     |
| aio-async-l4c25-314t    | 3.14t | async   | 4 loop × 25 + 1 sync |   5.3 s |   94.0 |   63 MB  |   61 MB  |    33 %  |    24 %  |    0     |
| aio-mixed-l2c50-s2-314  | 3.14  | mixed   | 2 loop × 50 + 2 sync |  63.1 s |    7.9 |   47 MB  |   47 MB  |    32 %  |     3 %  |    0     |
| aio-sync-s4-314         | 3.14  | sync    | 4 sync threads       |  63.1 s |    7.9 |   48 MB  |   48 MB  |    22 %  |     3 %  |    0     |
| aio-sync-s4-uvloop-314  | 3.14  | sync    | 4 sync threads       |  63.1 s |    7.9 |   51 MB  |   51 MB  |    23 %  |     3 %  |    0     |
| classic-threads4-314    | 3.14  | sync    | threads × 4          |  63.2 s |    7.9 |   54 MB  |   54 MB  |    15 %  |     4 %  |    0     |
| aio-sync-s4-uvloop-314t | 3.14t | sync    | 4 sync threads       |  63.2 s |    7.9 |   66 MB  |   66 MB  |    24 %  |     4 %  |    0     |
| aio-mixed-l2c50-s2-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync |  63.2 s |    7.9 |   63 MB  |   62 MB  |    24 %  |     3 %  |    0     |
| aio-mixed-l2c50-s2-uvloop-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync |  63.3 s |    7.9 |   64 MB  |   64 MB  |    31 %  |     3 %  |    0     |
| aio-sync-s4-314t        | 3.14t | sync    | 4 sync threads       |  63.3 s |    7.9 |   64 MB  |   63 MB  |    18 %  |     4 %  |    0     |
| classic-prefork4-314t   | 3.14t | sync    | prefork × 4          |  63.3 s |    7.9 |  310 MB  |  310 MB  |    15 %  |     5 %  |    0     |
| aio-mixed-l2c50-s2-uvloop-314 | 3.14  | mixed   | 2 loop × 50 + 2 sync |  63.3 s |    7.9 |   50 MB  |   50 MB  |    30 %  |     3 %  |    0     |
| classic-threads4-314t   | 3.14t | sync    | threads × 4          |  63.3 s |    7.9 |   71 MB  |   71 MB  |    15 %  |     4 %  |    0     |
| classic-prefork4-314    | 3.14  | sync    | prefork × 4          |  63.4 s |    7.9 |  253 MB  |  253 MB  |    19 %  |     6 %  |    0     |
| classic-prefork1-314    | 3.14  | sync    | prefork × 1          | 252.5 s |    2.0 |  103 MB  |  103 MB  |    28 %  |     2 %  |    0     |
| classic-prefork1-314t   | 3.14t | sync    | prefork × 1          | 252.7 s |    2.0 |  127 MB  |  126 MB  |     6 %  |     2 %  |    0     |

### Observations

**This is what asyncio is for.** `aio-async-l4c25-314` finishes 500
tasks of 500 ms each in **3.3 s at 153.5 TPS — 20 × faster** than any
4-slot config (all bunched at 7.9 TPS / 63 s) and **77 × faster** than
prefork-1 (2.0 TPS / 252 s).

**Sync threads can't unlock I/O concurrency.** `aio-sync-s4`,
`aio-mixed-l2c50-s2`, `classic-threads4`, `classic-prefork4` — all land
at exactly 7.9 TPS. With only 4 in-flight slots, 500 tasks × 500 ms ÷ 4
= 62.5 s wall is unavoidable. `aio-mixed` doesn't help: sync-variant
tasks always go to sync threads even when async slots are idle.

**uvloop is slightly slower on aio-async-314** (153.5 → 150.6 TPS,
−2 %). Default asyncio's selector is fine for the broker's small message
volume; libuv's bookkeeping is overhead the workload doesn't need.

**uvloop helps aio-async-314t** (94.0 → 126.7 TPS, +35 %) — same
free-threading-asyncio interaction that helped on mixed. The 3.14t async
pool is the worst-affected config in the matrix; uvloop recovers most
(but not all) of the ground vs vanilla 3.14.

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
| classic-threads4-314    |  55.9 |        3.83  |    1 540   |   **$0.04** |     $0.21  | 1 vCPU / 0.5 GB    |
| aio-sync-s4-314         | 135.5 |        4.21  |      684   |     $0.05   |     $0.09  | 1 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-314  | 136.6 |        4.24  |      726   |     $0.05   |     $0.09  | 1 vCPU / 0.5 GB    |
| aio-async-l4c25-uvloop-314 | 142.2 |        4.54  |      759   |     $0.05   |     $0.08  | 1 vCPU / 0.5 GB    |
| aio-sync-s4-uvloop-314  | 131.0 |        4.55  |      768   |     $0.05   |     $0.09  | 1 vCPU / 0.5 GB    |
| classic-prefork4-314    | 144.0 |        4.56  |    1 974   |     $0.05   |     $0.16  | 2 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-uvloop-314 | 131.1 |        4.65  |      786   |     $0.05   |     $0.09  | 1 vCPU / 0.5 GB    |
| aio-async-l4c25-314     | 132.8 |        4.75  |      822   |     $0.05   |     $0.09  | 1 vCPU / 0.5 GB    |
| classic-threads4-314t   |  53.1 |        4.96  |    1 617   |     $0.06   |     $0.44  | 2 vCPU / 0.5 GB    |
| aio-sync-s4-314t        | 108.9 |        5.12  |      740   |     $0.06   |     $0.11  | 1 vCPU / 0.5 GB    |
| classic-prefork1-314    |  35.4 |        5.31  |    3 136   |     $0.06   |     $0.18  | 0.5 vCPU / 0.5 GB  |
| aio-mixed-l2c50-s2-314t | 108.3 |        5.38  |      694   |     $0.06   |     $0.11  | 1 vCPU / 0.5 GB    |
| aio-async-l4c25-314t    | 109.1 |        5.45  |      700   |     $0.06   |     $0.11  | 1 vCPU / 0.5 GB    |
| classic-prefork4-314t   | 139.4 |        5.52  |    2 385   |     $0.06   |     $0.17  | 2 vCPU / 0.5 GB    |
| classic-prefork1-314t   |  34.3 |        6.24  |    3 853   |     $0.07   |     $0.18  | 0.5 vCPU / 0.5 GB  |
| aio-sync-s4-uvloop-314t | 124.7 |        6.66  |      681   |     $0.08   |     $0.19  | 2 vCPU / 0.5 GB    |
| aio-async-l4c25-uvloop-314t | 125.2 |        6.88  |      692   |     $0.08   |     $0.18  | 2 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-uvloop-314t | 118.7 |        7.44  |      693   |     $0.08   |     $0.19  | 2 vCPU / 0.5 GB    |

**The previous "prefork wins $/task by 3 ×" claim was wrong** — it was
the sampler bug. With correct CPU accounting, **prefork-4-314 sits at
$0.05/M ideal, tied with most aio configs** at the same TPS. The
ranking now is dominated by *how busy each config keeps its CPU*:

* **classic-threads4-314 is cheapest** because it uses one core (GIL-bound)
  and lets the rest of the CPU budget go to zero — Fargate-ideal doesn't
  bill for idle. Throughput is much lower though (55 TPS vs 144 for
  prefork-4), so this is only a win if you don't care about latency.
* **aio + prefork-4 + threads4-314t all cluster at $0.05–0.06/M** —
  same wall, similar CPU, the differences are noise in the sampler.
* **uvloop on 3.14t adds 25–35 % to the idealised cost** because the
  loop is doing more work per second of wall: TPS goes up but CPU
  consumption goes up proportionally.

**Provisioned cost is where aio still wins.** prefork-4-314 hits
peak_cpu 133 % so it rounds up to a 2 vCPU Fargate slot ($0.16/M). aio
configs peak at 75–97 % and fit in 1 vCPU ($0.08–0.09/M). On a Fargate
deployment with strict per-slot sizing, aio is ~half the cost of
prefork.

### Mixed — at 800 MB per worker process

The bench app is bare-bones (~75–120 MB RSS). A typical Django/Celery
worker with ORM cache, app code, and framework imports loaded sits
closer to 800 MB. For prefork, multiply by the number of children;
aio/threads keep one process.

| config                  | procs | RSS modelled | k8s m6i 60%  | k8s m7g 60%  |
| ----------------------- | ----: | -----------: | -----------: | -----------: |
| classic-threads4-314    |   1   |      800 MB  |  **$0.084**  |  **$0.072**  |
| aio-sync-s4-314         |   1   |      800 MB  |    $0.088    |    $0.074    |
| aio-mixed-l2c50-s2-314  |   1   |      800 MB  |    $0.088    |    $0.075    |
| aio-async-l4c25-uvloop-314 |   1   |      800 MB  |    $0.094    |    $0.080    |
| aio-sync-s4-uvloop-314  |   1   |      800 MB  |    $0.094    |    $0.080    |
| aio-mixed-l2c50-s2-uvloop-314 |   1   |      800 MB  |    $0.096    |    $0.082    |
| aio-async-l4c25-314     |   1   |      800 MB  |    $0.098    |    $0.084    |
| classic-prefork4-314    |   4   |    3 200 MB  |    $0.103    |    $0.088    |
| aio-sync-s4-314t        |   1   |      800 MB  |    $0.107    |    $0.091    |
| classic-threads4-314t   |   1   |      800 MB  |    $0.107    |    $0.091    |
| aio-mixed-l2c50-s2-314t |   1   |      800 MB  |    $0.112    |    $0.095    |
| aio-async-l4c25-314t    |   1   |      800 MB  |    $0.113    |    $0.096    |
| classic-prefork1-314    |   1   |      800 MB  |    $0.118    |    $0.101    |
| classic-prefork4-314t   |   4   |    3 200 MB  |    $0.123    |    $0.104    |
| aio-sync-s4-uvloop-314t |   1   |      800 MB  |    $0.137    |    $0.116    |
| classic-prefork1-314t   |   1   |      800 MB  |    $0.137    |    $0.117    |
| aio-async-l4c25-uvloop-314t |   1   |      800 MB  |    $0.141    |    $0.120    |
| aio-mixed-l2c50-s2-uvloop-314t |   1   |      800 MB  |    $0.153    |    $0.130    |

**At 800 MB per process, aio beats prefork-4** — single-process aio
configs sit at $0.084–0.098, while prefork-4 with 4 × 800 MB = 3.2 GB
costs $0.103. This is a clean inversion of the May 14 result and matches
what real-world Django workers experience.

### Crossover memory (mixed)

Solving `prefork-4-314` vs `aio-async-l4c25-uvloop-314` (best aio) for
break-even on Fargate-ideal pricing:

- aio extra CPU per task: `(4.54 − 4.56) / 1000 ≈ −0.00002 vCPU·s`
  (aio is now slightly *cheaper* on CPU)
- prefork extra memory per task: `(4·M − M) × 208.3 / 30 000 = 0.0208·M MB·s`

With aio's CPU now matching prefork's, **aio is cheaper at any RSS > 0**
on Fargate-ideal pricing. The crossover that existed in the May 14
result (≈ 2.5 GB per process) doesn't exist any more — long-lived
consumer iterations brought aio's CPU efficiency down to prefork's,
removing prefork's last cost advantage.

| RSS / process | prefork-4 $/1M  | aio-async-uvloop $/1M | winner            |
| ------------: | --------------: | --------------------: | ----------------- |
|        100 MB |        $0.053   |          $0.052       | tied              |
|        800 MB |        $0.103   |          $0.094       | aio (1.10 ×)      |
|      1 500 MB |        $0.152   |          $0.137       | aio (1.11 ×)      |
|      2 500 MB |        $0.222   |          $0.197       | aio (1.13 ×)      |
|      5 000 MB |        $0.396   |          $0.349       | aio (1.13 ×)      |

(`prefork-4 $/1M` includes 4 × `RSS/process`; aio is single-process.)

The constant ~12 % aio advantage at higher RSS reflects the 4 ×
memory-cost multiplier on prefork. The two configs now cost the same
on CPU; prefork pays more only on memory.

### CPU-only — sorted by idealised cost

| config                  | TPS   | vCPU·s / 1 k | MB·s / 1 k |  ideal $/1M |  prov $/1M | Fargate slot       |
| ----------------------- | ----: | -----------: | ---------: | ----------: | ---------: | ------------------ |
| classic-prefork1-314    |  40.1 |       24.97  |    2 569   |   **$0.28** |     $0.58  | 2 vCPU / 0.5 GB    |
| classic-threads4-314    |  39.0 |       25.39  |    1 368   |     $0.29   |     $0.59  | 2 vCPU / 0.5 GB    |
| aio-async-l4c25-314     |  38.1 |       25.94  |    1 162   |     $0.29   |     $0.61  | 2 vCPU / 0.5 GB    |
| aio-sync-s4-314         |  37.9 |       26.00  |    1 160   |     $0.29   |     $0.61  | 2 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-314  |  38.0 |       26.00  |    1 163   |     $0.29   |     $0.61  | 2 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-uvloop-314 |  36.7 |       26.89  |    1 290   |     $0.30   |     $0.63  | 2 vCPU / 0.5 GB    |
| aio-sync-s4-uvloop-314  |  36.8 |       26.95  |    1 274   |     $0.30   |     $0.63  | 2 vCPU / 0.5 GB    |
| aio-async-l4c25-uvloop-314 |  36.5 |       27.01  |    1 311   |     $0.31   |     $0.63  | 2 vCPU / 0.5 GB    |
| classic-prefork1-314t   |  30.3 |       32.89  |    4 181   |     $0.37   |     $0.76  | 2 vCPU / 0.5 GB    |
| classic-prefork4-314    | 117.0 |       33.25  |    2 163   |     $0.38   |     $0.77  | 8 vCPU / 0.5 GB    |
| classic-threads4-314t   |  50.7 |       42.67  |    1 388   |     $0.48   |     $1.79  | 8 vCPU / 0.5 GB    |
| classic-prefork4-314t   |  84.6 |       43.63  |    3 662   |     $0.50   |     $1.07  | 8 vCPU / 0.5 GB    |
| aio-async-l4c25-314t    |  77.8 |       47.12  |      901   |     $0.53   |     $1.16  | 8 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-314t |  78.4 |       47.77  |      819   |     $0.54   |     $0.58  | 4 vCPU / 0.5 GB    |
| aio-sync-s4-314t        |  78.4 |       48.04  |      762   |     $0.54   |     $0.58  | 4 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-uvloop-314t |  74.7 |       49.27  |      911   |     $0.56   |     $1.21  | 8 vCPU / 0.5 GB    |
| aio-sync-s4-uvloop-314t |  76.6 |       49.28  |      835   |     $0.56   |     $1.18  | 8 vCPU / 0.5 GB    |
| aio-async-l4c25-uvloop-314t |  75.5 |       50.28  |      925   |     $0.57   |     $1.20  | 8 vCPU / 0.5 GB    |

**Counter-intuitively, single-threaded configs are the cheapest** for
CPU-only work on Fargate-ideal pricing. `prefork-1-314` and the
GIL-bound 3.14 aio configs all spend ~26 vCPU·s per 1 k tasks because
each task uses 1 core × 5 ms compute and the Python interpreter is
single-threaded — no parallel speedup means no extra cores billed.

**prefork-4 and free-threaded aio cost ~$0.50/M** — they finish 3 × as
fast but use 4 cores, so total CPU·seconds is unchanged. You're paying
the same amount of compute, just in parallel. For a Fargate-ideal
billing model, **TPS doesn't matter** — only total CPU consumption.

**Provisioned cost is brutal for free-threaded aio** — peak CPU 401 %
forces the Fargate slot up to 8 vCPU even though the workload finishes
in 13 s. If you can't use burst pricing, single-threaded prefork is
4 × cheaper for CPU-only work.

**uvloop neutral.** All variants within ±3 % of their non-uvloop
counterpart on the cost axis.

### I/O-only — sorted by idealised cost

| config                  | TPS   | vCPU·s / 1 k | MB·s / 1 k |  ideal $/1M |  prov $/1M | Fargate slot       |
| ----------------------- | ----: | -----------: | ---------: | ----------: | ---------: | ------------------ |
| aio-async-l4c25-314     | 153.5 |        2.03  |      297   |   **$0.02** |     $0.04  | 0.5 vCPU / 0.5 GB  |
| aio-async-l4c25-uvloop-314 | 150.6 |        2.09  |      325   |     $0.02   |     $0.04  | 0.5 vCPU / 0.5 GB  |
| aio-async-l4c25-uvloop-314t | 126.7 |        2.36  |      496   |     $0.03   |     $0.05  | 0.5 vCPU / 0.5 GB  |
| aio-async-l4c25-314t    |  94.0 |        2.52  |      652   |     $0.03   |     $0.07  | 0.5 vCPU / 0.5 GB  |
| aio-mixed-l2c50-s2-314  |   7.9 |        3.41  |    5 967   |     $0.05   |     $0.79  | 0.5 vCPU / 0.5 GB  |
| aio-mixed-l2c50-s2-uvloop-314 |   7.9 |        3.42  |    6 341   |     $0.05   |     $0.79  | 0.5 vCPU / 0.5 GB  |
| aio-mixed-l2c50-s2-uvloop-314t |   7.9 |        3.80  |    8 122   |     $0.05   |     $0.79  | 0.5 vCPU / 0.5 GB  |
| aio-sync-s4-314         |   7.9 |        4.04  |    6 097   |     $0.05   |     $0.43  | 0.25 vCPU / 0.5 GB |
| aio-mixed-l2c50-s2-314t |   7.9 |        3.92  |    7 880   |     $0.05   |     $0.43  | 0.25 vCPU / 0.5 GB |
| aio-sync-s4-uvloop-314  |   7.9 |        4.17  |    6 440   |     $0.05   |     $0.43  | 0.25 vCPU / 0.5 GB |
| aio-sync-s4-314t        |   7.9 |        4.55  |    7 983   |     $0.06   |     $0.43  | 0.25 vCPU / 0.5 GB |
| aio-sync-s4-uvloop-314t |   7.9 |        4.55  |    8 291   |     $0.06   |     $0.43  | 0.25 vCPU / 0.5 GB |
| classic-threads4-314    |   7.9 |        5.43  |    6 812   |     $0.07   |     $0.43  | 0.25 vCPU / 0.5 GB |
| classic-threads4-314t   |   7.9 |        5.57  |    8 942   |     $0.07   |     $0.43  | 0.25 vCPU / 0.5 GB |
| classic-prefork4-314    |   7.9 |        6.97  |   32 047   |     $0.12   |     $0.43  | 0.25 vCPU / 0.5 GB |
| classic-prefork4-314t   |   7.9 |        6.71  |   39 233   |     $0.12   |     $0.43  | 0.25 vCPU / 0.5 GB |
| classic-prefork1-314    |   2.0 |        8.08  |   52 060   |     $0.15   |     $3.15  | 0.5 vCPU / 0.5 GB  |
| classic-prefork1-314t   |   2.0 |        8.59  |   63 935   |     $0.17   |     $1.73  | 0.25 vCPU / 0.5 GB |

**For I/O-bound work, aio-async wins both speed AND cost** — 6–8 × the
TPS and 2.5 × cheaper than the next-best (mixed/sync 4-slot configs at
$0.05/M). With most of the worker sitting in `asyncio.sleep` the event
loop costs almost nothing; the massive TPS lead amortises broker/setup
overhead across vastly more tasks.

**prefork's per-process memory bill bites hard.** `classic-prefork4-314`
spends 32 000 MB·s per 1 000 tasks vs aio-async's 297 — a 100 × gap.
RSS is the same as on the mixed workload (~250 MB) but wall is 20 ×
longer, so integrated memory·time is 20 × larger.

## Summary

| profile  | best $/task (Fargate ideal)            | best TPS                  | when to use |
| -------- | -------------------------------------- | ------------------------- | ----------- |
| mixed    | classic-threads4-314 ($0.04/M); aio + prefork-4 tied at $0.05/M | classic-prefork4-314 (144 TPS) | aio matches prefork on TPS and cost on a clean bench app, and wins at typical app RSS (≥ 800 MB). For burstable pricing, threads4-314 is cheapest but only at 55 TPS |
| cpu-only | classic-prefork1-314 ($0.28/M)         | classic-prefork4-314 (117 TPS) | parallel CPU work — prefork-1 is cheapest because it uses one core; prefork-4 / free-threaded aio finish 3 × faster but use 4 × the CPU·s, so cost is similar but TPS is higher |
| io-only  | aio-async-l4c25-314 ($0.02/M)          | aio-async-l4c25-314 (154 TPS) | high-latency outbound I/O — aio dominates by 6–20 × on both metrics |

### When to pick uvloop

- **3.14 + asyncio:** neutral or slightly slower — skip it. Default
  selector is already well-optimised for celery's broker traffic.
- **3.14t + asyncio:** worth it — uvloop closes ~50 % of the
  3.14 → 3.14t performance gap on mixed and ~80 % on io-only. Adds
  ~7 MB RSS, no other downside.

## Known issues surfaced

1. **CPU sampler bug (fixed in this run).** The previous RESULTS.md
   undercounted prefork's CPU usage by ~4 × because `psutil.Process`
   objects were recreated each sample. With the fix in place, the cost
   story flipped from "prefork wins by 3 ×" to "prefork ties aio on
   clean app RSS, aio wins at 800 MB+ per process." This is a
   measurement-quality finding; the underlying performance hasn't
   changed.

2. **Orphaned worker contamination.** Killing a runner mid-flight
   leaves its celery worker child alive thanks to
   `start_new_session=True`. That stray worker keeps consuming from the
   broker queue and steals tasks from the next config under test.
   Pre-flight: `ps -ef | grep "celery.*worker"` should return nothing,
   and every worker log should say `mingle: all alone` (not `sync with
   N nodes`).

3. **Free-threaded 3.14t penalty on the mixed workload.** ~25 %
   wall-clock slower than 3.14 on every aio config (109 vs 144 TPS).
   The mix is 60 % I/O-bound; free-threading's per-thread interpreter
   overhead applies on every thread but the parallel-CPU benefit
   doesn't fire. uvloop closes most of the gap (124–125 TPS).

4. **aio-mixed pool doesn't dynamically rebalance.** `--variant mixed`
   alternates sync/async tasks across the 2 sync threads + 2 loop
   workers. On a single-variant workload (io-only or cpu-only) it
   performs identically to its 4-slot sync equivalent.
