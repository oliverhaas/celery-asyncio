# Benchmark Results

**Date:** 2026-05-18
**Hardware:** Worker pinned to 4 cores (Intel i7-8565U @ 1.80GHz) via `taskset -c 0,1,2,3`
**Broker / backend:** valkey 8 on `localhost:6379` (via docker-compose)
**kombu-asyncio:** `main` @ `ac52b681` (long-lived consumer iterations + restore-on-cancel)
**celery-asyncio:** `main` @ `7442d572d`

The benchmark now runs three workload profiles (`run_all.py --profile …`):

- **mixed** (default): the historical balanced workload that exercises CPU, I/O,
  and memory simultaneously.
- **cpu-only**: 100 % cpu_heavy tasks (~5 ms of pure-Python compute each). Use
  to expose GIL contention vs free-threading.
- **io-only**: 100 % io_heavy tasks (500 ms sleep each — realistic for an
  outbound API call). Use to expose asyncio's concurrency advantage.

## Strand bug fixed

The May 14 mixed-workload table reported 21–48 stranded tasks per aio config
(0.07–0.16 %). That was a Redis-transport cancellation race in
`Channel.drain_events`: when its `asyncio.wait` timeout fired *after*
BZMPOP / the consume Lua script had already popped server-side but before the
client processed the reply, the popped tag was orphaned in `messages_index`
until the visibility-timeout restore re-enqueued it ~6 min later. The
benchmark's stall timeout fired first, recording the leak in `n_stranded`.

Fixed in kombu-asyncio
[#4](https://github.com/oliverhaas/kombu-asyncio/pull/4) (long-lived consumer
iterations — drain_events never cancels in the hot path) plus restore-on-cancel
for cold paths (post-pop but pre-deliver cancellation now re-queues the tag).

**Result: `n_stranded = 0` on all 36 runs of this matrix.**

## Workloads

### mixed (30 000 tasks)

| profile    | weight | cpu_iters | io     | mem      |
| ---------- | -----: | --------: | -----: | -------: |
| io_heavy   |   40 % |       200 | 50 ms  |   16 KiB |
| cpu_heavy  |   30 % |     4 000 |  0 ms  |   32 KiB |
| mem_heavy  |   20 % |       100 | 10 ms  | 8192 KiB |
| balanced   |   10 % |     1 000 | 20 ms  |  256 KiB |

### cpu-only (1 000 tasks)

`cpu_iters=20 000` (≈ 5 ms of float arithmetic per task), no I/O, no memory.

### io-only (500 tasks)

`io_seconds=0.5` (sync via `time.sleep`, async via `asyncio.sleep`), no CPU,
no memory.

## Full results — mixed workload

Sorted by wall-clock. `mean_cpu` can exceed 100 % on multi-core (summed across
cores).

| config                  | py    | variant | workers              | wall    | TPS    | peak RSS | mean RSS | peak CPU | mean CPU | stranded |
| ----------------------- | ----- | ------- | -------------------- | ------: | -----: | -------: | -------: | -------: | -------: | -------: |
| classic-prefork4-314    | 3.14  | sync    | prefork × 4          | 217.6 s |  137.9 |  284 MB  |  284 MB  |    67 %  |    26 %  |    0     |
| classic-prefork4-314t   | 3.14t | sync    | prefork × 4          | 228.8 s |  131.1 |  343 MB  |  332 MB  |    48 %  |    24 %  |    0     |
| aio-mixed-l2c50-s2-314  | 3.14  | mixed   | 2 loop × 50 + 2 sync | 243.9 s |  123.0 |   97 MB  |   91 MB  |    86 %  |    63 %  |    0     |
| aio-sync-s4-314         | 3.14  | sync    | 4 sync threads       | 248.6 s |  120.7 |   78 MB  |   77 MB  |    89 %  |    61 %  |    0     |
| aio-async-l4c25-314     | 3.14  | async   | 4 loop × 25 + 1 sync | 266.8 s |  112.5 |  100 MB  |   89 MB  |   113 %  |    68 %  |    0     |
| aio-sync-s4-314t        | 3.14t | sync    | 4 sync threads       | 338.2 s |   88.7 |   98 MB  |   80 MB  |    75 %  |    57 %  |    0     |
| aio-mixed-l2c50-s2-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync | 344.8 s |   87.0 |   99 MB  |   76 MB  |    80 %  |    60 %  |    0     |
| aio-async-l4c25-314t    | 3.14t | async   | 4 loop × 25 + 1 sync | 358.0 s |   83.8 |   84 MB  |   70 MB  |    84 %  |    61 %  |    0     |
| classic-threads4-314    | 3.14  | sync    | threads × 4          | 509.9 s |   58.8 |   86 MB  |   86 MB  |    89 %  |    28 %  |    0     |
| classic-threads4-314t   | 3.14t | sync    | threads × 4          | 549.6 s |   54.6 |  105 MB  |   86 MB  |   125 %  |    30 %  |    0     |
| classic-prefork1-314    | 3.14  | sync    | prefork × 1          | 866.7 s |   34.6 |  111 MB  |  111 MB  |    15 %  |     7 %  |    0     |
| classic-prefork1-314t   | 3.14t | sync    | prefork × 1          | 905.1 s |   33.1 |  135 MB  |  132 MB  |    15 %  |     7 %  |    0     |

### Observations

**Prefork-4 still wins raw TPS** at 137.9 (3.99 × prefork-1), exactly the
near-linear multi-process scaling we saw on May 14. Each worker process gets
its own broker connection and its own GIL.

**celery-asyncio's three pool layouts cluster within 10 %** of each other on
the mixed mix (112–123 TPS on 3.14). The sync variant is slightly faster than
async here because the workload is dominated by short tasks where the
event-loop bookkeeping doesn't pay off; the async variant only catches up when
the I/O share grows (see io-only below).

**3.14t costs ~22–28 % wall-clock on this workload** — same direction as
May 14. Free-threading's per-thread interpreter overhead applies on every
thread but the parallel-CPU win doesn't fire because 60 % of tasks are
I/O-bound. The exception is `classic-prefork4-314t` (only 5 % slower than
3.14) — each prefork child is single-threaded so the overhead is barely
visible.

**Memory remains aio's clear win.** Peak RSS is 70–100 MB across every aio
config vs 284–343 MB for prefork-4. classic-threads4 sits between at 86–105 MB
but pays in throughput (54–59 TPS).

## Full results — cpu-only workload

Sorted by wall-clock.

| config                  | py    | variant | workers              | wall    | TPS    | peak RSS | mean RSS | peak CPU | mean CPU | stranded |
| ----------------------- | ----- | ------- | -------------------- | ------: | -----: | -------: | -------: | -------: | -------: | -------: |
| classic-prefork4-314    | 3.14  | sync    | prefork × 4          |   9.8 s |  102.3 |  253 MB  |  253 MB  |    52 %  |    18 %  |    0     |
| classic-prefork4-314t   | 3.14t | sync    | prefork × 4          |  12.7 s |   78.6 |  310 MB  |  310 MB  |    21 %  |    13 %  |    0     |
| aio-sync-s4-314t        | 3.14t | sync    | 4 sync threads       |  14.1 s |   71.0 |   61 MB  |   60 MB  |   401 %  |   376 %  |    0     |
| aio-async-l4c25-314t    | 3.14t | async   | 4 loop × 25 + 1 sync |  14.1 s |   71.0 |   72 MB  |   69 MB  |   401 %  |   374 %  |    0     |
| aio-mixed-l2c50-s2-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync |  14.2 s |   70.6 |   63 MB  |   62 MB  |   401 %  |   376 %  |    0     |
| classic-threads4-314t   | 3.14t | sync    | threads × 4          |  20.5 s |   48.8 |   71 MB  |   71 MB  |   399 %  |   228 %  |    0     |
| classic-prefork1-314    | 3.14  | sync    | prefork × 1          |  27.2 s |   36.7 |  103 MB  |  103 MB  |     8 %  |     5 %  |    0     |
| classic-threads4-314    | 3.14  | sync    | threads × 4          |  28.4 s |   35.2 |   53 MB  |   53 MB  |   104 %  |   100 %  |    0     |
| aio-mixed-l2c50-s2-314  | 3.14  | mixed   | 2 loop × 50 + 2 sync |  29.2 s |   34.2 |   44 MB  |   44 MB  |   103 %  |   100 %  |    0     |
| aio-sync-s4-314         | 3.14  | sync    | 4 sync threads       |  29.3 s |   34.2 |   44 MB  |   44 MB  |   104 %  |   100 %  |    0     |
| aio-async-l4c25-314     | 3.14  | async   | 4 loop × 25 + 1 sync |  31.7 s |   31.5 |   44 MB  |   44 MB  |   103 %  |   101 %  |    0     |
| classic-prefork1-314t   | 3.14t | sync    | prefork × 1          |  35.4 s |   28.3 |  126 MB  |  126 MB  |     6 %  |     4 %  |    0     |

### Observations

**Free-threaded Python lets in-process threads actually parallelise CPU work.**
Every multi-threaded config sees ~2× speedup going from 3.14 to 3.14t:

| config             | 3.14 TPS | 3.14t TPS | speedup |
| ------------------ | -------: | --------: | ------: |
| aio-sync-s4        |     34.2 |      71.0 |   2.07× |
| aio-async-l4c25    |     31.5 |      71.0 |   2.25× |
| aio-mixed-l2c50-s2 |     34.2 |      70.6 |   2.07× |
| classic-threads4   |     35.2 |      48.8 |   1.39× |

`mean_cpu` confirms it: the 314t aio configs all hit ~375 % (≈ all 4 pinned
cores running Python in parallel). On regular 3.14 they cap at 100 % (one
thread holds the GIL at a time). classic-threads4-314t only reaches 228 %
because kombu/celery's pure-Python paths contain locks that re-serialise even
under free-threading.

**Prefork-4 still wins on absolute TPS** (102.3 TPS, 1.44 × the best
free-threaded aio). Each process has its own GIL, so prefork sidesteps the
problem entirely. But:

**Prefork-4-314t is actually slower than prefork-4-314** (78.6 vs 102.3 TPS,
−23 %). Each prefork child is single-threaded, so free-threading's per-thread
interpreter overhead applies without the parallel-CPU benefit. Free-threading
*costs* you when you're already running multi-process.

**Memory delta is dramatic.** Free-threaded aio runs the same CPU workload at
~60 MB peak RSS vs 253–310 MB for prefork-4 — a 4–5 × reduction. This is the
canonical free-threading win: same parallelism, one address space.

## Full results — io-only workload

Sorted by wall-clock.

| config                  | py    | variant | workers              | wall    | TPS    | peak RSS | mean RSS | peak CPU | mean CPU | stranded |
| ----------------------- | ----- | ------- | -------------------- | ------: | -----: | -------: | -------: | -------: | -------: | -------: |
| aio-async-l4c25-314     | 3.14  | async   | 4 loop × 25 + 1 sync |   3.8 s |  132.2 |   46 MB  |   45 MB  |    41 %  |    35 %  |    0     |
| aio-async-l4c25-314t    | 3.14t | async   | 4 loop × 25 + 1 sync |   5.3 s |   94.4 |   62 MB  |   61 MB  |    30 %  |    24 %  |    0     |
| aio-sync-s4-314t        | 3.14t | sync    | 4 sync threads       |  63.1 s |    7.9 |   64 MB  |   63 MB  |    18 %  |     4 %  |    0     |
| aio-sync-s4-314         | 3.14  | sync    | 4 sync threads       |  63.2 s |    7.9 |   48 MB  |   48 MB  |    24 %  |     4 %  |    0     |
| aio-mixed-l2c50-s2-314  | 3.14  | mixed   | 2 loop × 50 + 2 sync |  63.2 s |    7.9 |   47 MB  |   47 MB  |    31 %  |     3 %  |    0     |
| classic-threads4-314    | 3.14  | sync    | threads × 4          |  63.2 s |    7.9 |   54 MB  |   54 MB  |    62 %  |     4 %  |    0     |
| aio-mixed-l2c50-s2-314t | 3.14t | mixed   | 2 loop × 50 + 2 sync |  63.3 s |    7.9 |   63 MB  |   62 MB  |    26 %  |     3 %  |    0     |
| classic-threads4-314t   | 3.14t | sync    | threads × 4          |  63.3 s |    7.9 |   71 MB  |   71 MB  |    22 %  |     4 %  |    0     |
| classic-prefork4-314t   | 3.14t | sync    | prefork × 4          |  63.4 s |    7.9 |  310 MB  |  310 MB  |    10 %  |     4 %  |    0     |
| classic-prefork4-314    | 3.14  | sync    | prefork × 4          |  63.4 s |    7.9 |  253 MB  |  252 MB  |    63 %  |     4 %  |    0     |
| classic-prefork1-314    | 3.14  | sync    | prefork × 1          | 252.5 s |    2.0 |  103 MB  |  103 MB  |     4 %  |     1 %  |    0     |
| classic-prefork1-314t   | 3.14t | sync    | prefork × 1          | 252.7 s |    2.0 |  126 MB  |  126 MB  |     4 %  |     1 %  |    0     |

### Observations

**This is what asyncio is for.** `aio-async-l4c25-314` hits 132 TPS on 500 ms
tasks — **16 × faster than any 4-slot sync config** and **66 × faster than
prefork-1**. With 100 in-flight async slots the workload finishes in 3.8 s, vs
~63 s for any 4-slot config (4 × 500 ms × 125 batches) and ~250 s for prefork-1
(500 × 500 ms). The async pool turns I/O latency from a serial bottleneck into
something you can hide entirely.

**Sync threads can't unlock I/O concurrency.** `aio-sync-s4`, `classic-threads4`,
`classic-prefork4`, and even `aio-mixed-l2c50-s2` all land at 7.9 TPS / 63 s —
the four configs share the *same* in-flight cap of 4 slots (4 sync threads or
4 prefork processes), and `time.sleep` blocks the slot. Their wall-time is
purely set by the broker → worker round-trip. `aio-mixed` doesn't outperform
the others because its dispatcher routes the I/O task to the 2 sync threads
(half the bottleneck) — mixed isn't actually a hedge for pure I/O work.

**314 beats 314t for aio-async on io-only** (132 vs 94 TPS, −29 %). Same
free-threading overhead-without-benefit pattern as prefork above: nothing in
the io-only workload uses multiple parallel cores, so 314t's per-thread cost
is pure overhead.

**Memory at 4-slot parity.** All four 4-slot configs deliver the same 7.9 TPS
but with very different RSS: 48–71 MB for aio/threads, 253–310 MB for
prefork-4. Per-process baseline RSS is independent of workload shape — prefork
pays it for every child whether they're working or sleeping.

## Cost per task (AWS Fargate, Linux/x86, us-east-1)

**Pricing reference (May 2026):** $0.04048 per vCPU·hour, $0.004445 per
GB·hour. Per-second: ~$1.124e-5 per vCPU·s, ~$1.235e-6 per GB·s.

**Two cost models:**

- **Idealised** — pay only for the resources the worker actually consumed:
  `cost = (mean_cpu × wall × vCPU·s rate) + (mean_rss × wall × GB·s rate)`.
- **Provisioned** — Fargate slot sized to the worker's peak. CPU rounded up
  to {0.25, 0.5, 1, 2, 4, 8} vCPU based on `peak_cpu`; memory rounded up to
  the next 0.5 GiB above `peak_rss × 1.25`.

### Mixed workload — sorted by idealised cost

| config                  | TPS   | vCPU·s / 1 k | MB·s / 1 k |  ideal $/1M |  prov $/1M | Fargate slot       |
| ----------------------- | ----: | -----------: | ---------: | ----------: | ---------: | ------------------ |
| classic-prefork4-314    | 137.9 |        1.86  |    2 062   |   **$0.02** |     $0.09  | 1 vCPU / 0.5 GB    |
| classic-prefork4-314t   | 131.1 |        1.87  |    2 531   |     $0.02   |     $0.05  | 0.5 vCPU / 0.5 GB  |
| classic-prefork1-314    |  34.6 |        1.96  |    3 210   |     $0.03   |     $0.10  | 0.25 vCPU / 0.5 GB |
| classic-prefork1-314t   |  33.1 |        2.11  |    3 979   |     $0.03   |     $0.10  | 0.25 vCPU / 0.5 GB |
| classic-threads4-314    |  58.8 |        4.81  |    1 465   |     $0.06   |     $0.20  | 1 vCPU / 0.5 GB    |
| aio-sync-s4-314         | 120.7 |        5.09  |      641   |     $0.06   |     $0.10  | 1 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-314  | 123.0 |        5.14  |      741   |     $0.06   |     $0.10  | 1 vCPU / 0.5 GB    |
| classic-threads4-314t   |  54.6 |        5.57  |    1 572   |     $0.06   |     $0.42  | 2 vCPU / 0.5 GB    |
| aio-async-l4c25-314     | 112.5 |        6.06  |      791   |     $0.07   |     $0.21  | 2 vCPU / 0.5 GB    |
| aio-sync-s4-314t        |  88.7 |        6.45  |      897   |     $0.07   |     $0.13  | 1 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-314t |  87.0 |        6.87  |      872   |     $0.08   |     $0.14  | 1 vCPU / 0.5 GB    |
| aio-async-l4c25-314t    |  83.8 |        7.31  |      840   |     $0.08   |     $0.14  | 1 vCPU / 0.5 GB    |

**prefork wins $/task by ~3×.** Same observation as May 14: aio trades CPU
for concurrency. The mixed workload's `io_heavy` slice (40 % of tasks, 50 ms
sleep each) costs zero CPU under prefork — the process really sleeps. Under
aio the event loop services other tasks during that 50 ms, billing CPU even
when each individual task is "sleeping". That trade is the right one when
latency or single-process throughput matter (and when your worker baseline RSS
is large enough that the per-process cost dominates; see below).

### Mixed workload — at 800 MB per worker process

The bench app is bare-bones (~70–110 MB RSS). A typical production
Django/Celery worker with ORM cache, app code, and framework imports loaded
sits closer to **800 MB**. For prefork we multiply by the number of children;
aio/threads pay the 800 MB once.

| config                  | procs | RSS modelled | k8s m6i 60%  | k8s m7g 60%  |
| ----------------------- | ----: | -----------: | -----------: | -----------: |
| classic-prefork4-314    |   4   |    3 200 MB  |  **$0.050**  |  **$0.042**  |
| classic-prefork4-314t   |   4   |    3 200 MB  |    $0.051    |    $0.043    |
| classic-prefork1-314    |   1   |      800 MB  |    $0.052    |    $0.044    |
| classic-prefork1-314t   |   1   |      800 MB  |    $0.055    |    $0.047    |
| classic-threads4-314    |   1   |      800 MB  |    $0.104    |    $0.088    |
| aio-sync-s4-314         |   1   |      800 MB  |    $0.105    |    $0.090    |
| aio-mixed-l2c50-s2-314  |   1   |      800 MB  |    $0.106    |    $0.090    |
| classic-threads4-314t   |   1   |      800 MB  |    $0.119    |    $0.101    |
| aio-async-l4c25-314     |   1   |      800 MB  |    $0.125    |    $0.106    |
| aio-sync-s4-314t        |   1   |      800 MB  |    $0.134    |    $0.114    |
| aio-mixed-l2c50-s2-314t |   1   |      800 MB  |    $0.143    |    $0.121    |
| aio-async-l4c25-314t    |   1   |      800 MB  |    $0.152    |    $0.129    |

(`k8s m6i` and `k8s m7g` use `m6i.large` and Graviton `m7g.large` hourly
pricing split 9.1 : 1 between CPU and memory, then scaled by 1 / 0.60 for
realistic cluster utilisation.)

**aio's memory advantage shows up at 800 MB.** prefork-4 with 4 × 800 MB =
3.2 GB still wins, but barely — prefork-1 (1 × 800 MB) is now a closer
neighbour. The single-process aio configs sit ~2 × above prefork-4 in $/task,
not the ~3.5 × they showed at bench-app RSS. The crossover is below.

### Crossover memory (mixed workload)

Solving `prefork4-314` vs `aio-async-l4c25-314` for break-even cost-per-task:

- aio extra CPU per task: `0.00606 − 0.00186 = 0.00420 vCPU·s/task` → +$4.7e-8
- prefork extra memory per task: `(4·M − M) × 217.6 / 30 000 = 0.0218 · M MB·s`
  (M = MB per worker process)

Setting equal: **M ≈ 1 800 MB per process**. Below that prefork is cheaper;
above that aio is cheaper.

| RSS / process | prefork-4 $/1M | aio-async $/1M | winner            |
| ------------: | -------------: | -------------: | ----------------- |
|       100 MB  |       $0.022   |     $0.082     | prefork (3.7×)    |
|       800 MB  |       $0.050   |     $0.125     | prefork (2.5×)    |
|     1 500 MB  |       $0.076   |     $0.137     | prefork (1.8×)    |
|     1 800 MB  |       $0.087   |     $0.140     | prefork (1.6×)    |
|     2 500 MB  |       $0.114   |     $0.150     | prefork (1.3×)    |
|     5 000 MB  |       $0.205   |     $0.187     | aio (1.10×)       |
|    10 000 MB  |       $0.388   |     $0.262     | aio (1.48×)       |

The crossover moved from ~2 500 MB (May 14) down to ~1 800 MB — the new
kombu transport gives aio a small CPU efficiency bump (long-lived consumer
tasks aren't torn down each iteration). asyncio still doesn't win on cost
for typical web-app workers (~0.5–1 GB RSS), but the bar is lower than it
was last month.

### CPU-only workload — sorted by idealised cost

| config                  | TPS   | vCPU·s / 1 k | MB·s / 1 k |  ideal $/1M |  prov $/1M | Fargate slot       |
| ----------------------- | ----: | -----------: | ---------: | ----------: | ---------: | ------------------ |
| classic-prefork1-314    |  36.7 |        1.28  |    2 805   |   **$0.02** |     $0.09  | 0.25 vCPU / 0.5 GB |
| classic-prefork1-314t   |  28.3 |        1.38  |    4 473   |     $0.02   |     $0.12  | 0.25 vCPU / 0.5 GB |
| classic-prefork4-314    | 102.3 |        1.75  |    2 471   |     $0.02   |     $0.12  | 1 vCPU / 0.5 GB    |
| classic-prefork4-314t   |  78.6 |        1.68  |    3 940   |     $0.02   |     $0.04  | 0.25 vCPU / 0.5 GB |
| classic-threads4-314    |  35.2 |       28.53  |    1 511   |     $0.32   |     $0.66  | 2 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-314  |  34.2 |       29.20  |    1 291   |     $0.33   |     $0.67  | 2 vCPU / 0.5 GB    |
| aio-sync-s4-314         |  34.2 |       29.32  |    1 284   |     $0.33   |     $0.68  | 2 vCPU / 0.5 GB    |
| aio-async-l4c25-314     |  31.5 |       31.91  |    1 402   |     $0.36   |     $0.73  | 2 vCPU / 0.5 GB    |
| classic-threads4-314t   |  48.8 |       46.72  |    1 451   |     $0.53   |     $0.93  | 4 vCPU / 0.5 GB    |
| aio-async-l4c25-314t    |  71.0 |       52.64  |      967   |     $0.59   |     $1.28  | 8 vCPU / 0.5 GB    |
| aio-sync-s4-314t        |  71.0 |       52.98  |      848   |     $0.60   |     $1.28  | 8 vCPU / 0.5 GB    |
| aio-mixed-l2c50-s2-314t |  70.6 |       53.37  |      886   |     $0.60   |     $1.28  | 8 vCPU / 0.5 GB    |

**Prefork dominates CPU-only at 15–30 × the cost-efficiency** of every other
config. CPU-bound work has nothing to hide and no I/O time for an event-loop
to amortise across — every vCPU·second the worker burns shows up directly.
And every prefork child has its own GIL, so it just executes at native speed
without thread-coordination overhead.

**Free-threading shifts TPS but not $/task.** 314t free-threaded aio configs
double TPS (35 → 71) but also double CPU consumption (the same work runs
across 4 cores instead of 1), so the $/task stays roughly flat at the
top-end. The win for free-threading on CPU is "I can fit more work in one
process" — not "I burn less compute".

**Provisioned cost is brutal for free-threaded aio:** peak CPU hits 400 %,
forcing the Fargate slot up to 8 vCPU even though mean utilisation is
much lower. If you can use the Fargate idealised numbers (k8s burstable),
the 314t aio configs are ~10 % cheaper than 314 aio; if you have to provision
for peak, 314t costs more.

### I/O-only workload — sorted by idealised cost

| config                  | TPS   | vCPU·s / 1 k | MB·s / 1 k |  ideal $/1M |  prov $/1M | Fargate slot       |
| ----------------------- | ----: | -----------: | ---------: | ----------: | ---------: | ------------------ |
| aio-async-l4c25-314t    |  94.4 |        2.57  |      648   |   **$0.03** |     $0.07  | 0.5 vCPU / 0.5 GB  |
| aio-async-l4c25-314     | 132.2 |        2.63  |      343   |     $0.03   |     $0.05  | 0.5 vCPU / 0.5 GB  |
| aio-mixed-l2c50-s2-314  |   7.9 |        3.79  |    5 943   |     $0.05   |     $0.79  | 0.5 vCPU / 0.5 GB  |
| aio-mixed-l2c50-s2-314t |   7.9 |        4.05  |    7 898   |     $0.06   |     $0.79  | 0.5 vCPU / 0.5 GB  |
| aio-sync-s4-314         |   7.9 |        4.80  |    6 094   |     $0.06   |     $0.43  | 0.25 vCPU / 0.5 GB |
| aio-sync-s4-314t        |   7.9 |        4.67  |    7 953   |     $0.06   |     $0.43  | 0.25 vCPU / 0.5 GB |
| classic-threads4-314t   |   7.9 |        5.19  |    8 949   |     $0.07   |     $0.43  | 0.25 vCPU / 0.5 GB |
| classic-threads4-314    |   7.9 |        5.44  |    6 792   |     $0.07   |     $1.50  | 1 vCPU / 0.5 GB    |
| classic-prefork4-314    |   7.9 |        4.95  |   32 004   |     $0.09   |     $1.50  | 1 vCPU / 0.5 GB    |
| classic-prefork4-314t   |   7.9 |        4.69  |   39 253   |     $0.10   |     $0.43  | 0.25 vCPU / 0.5 GB |
| classic-prefork1-314    |   2.0 |        5.56  |   52 019   |     $0.13   |     $1.73  | 0.25 vCPU / 0.5 GB |
| classic-prefork1-314t   |   2.0 |        5.56  |   63 874   |     $0.14   |     $1.73  | 0.25 vCPU / 0.5 GB |

**For I/O-bound work, aio-async is both the cheapest and the fastest.** That's
the only ranking flip relative to mixed/cpu-only. With most of the worker
sitting in `asyncio.sleep` the event loop costs almost nothing, and the
massive TPS lead amortises the broker/setup overhead across vastly more
tasks. The 100-slot async pool processes the same work in 3.8 s that takes
prefork-4 63 s.

**Prefork's per-process memory bill bites hard here.** `classic-prefork4`
spends 32 000 MB·s per 1 000 tasks vs aio-async's 343 — almost 100 ×. The
mean RSS is identical to mixed (~250 MB), but the wall-time is *eight times
longer*, so the integrated memory·time charge is eight times larger too.
That's why prefork-1 lands at the bottom of the table even though its CPU
share is tiny.

**aio-sync, aio-mixed, classic-threads4, classic-prefork4** all converge at
$0.06–0.09 per million. Same in-flight cap, same wall, same $/task. The
choice between them is dictated by other axes (memory, deployment shape),
not throughput on this profile.

## Summary

| profile  | best $/task | best TPS | when to use |
| -------- | ----------- | -------- | ----------- |
| mixed    | prefork-4 ($0.02/M) | prefork-4 (138 TPS) | most production workloads — prefork's per-process GIL avoidance wins as long as worker RSS stays under ~1.8 GB |
| cpu-only | prefork (any) ($0.02/M) | prefork-4 (102 TPS) | embarrassingly-parallel CPU work — prefork is dominant. Free-threading lets aio match prefork on TPS at half the RSS, but doesn't reduce $/task |
| io-only  | aio-async ($0.03/M) | aio-async-314 (132 TPS) | high-latency outbound I/O (API calls, DB waits) — aio's concurrency cap of 100+ slots leaves the 4-slot configs in the dust |

**Picking a pool comes down to your workload's I/O share and your worker's
baseline RSS.** The new kombu transport (PR
[#4](https://github.com/oliverhaas/kombu-asyncio/pull/4)) restores zero-strand
delivery on every config — pool choice can be made on throughput and cost
alone, not on which config tolerates the cancellation race best.

## Known issues surfaced

1. **3.14t async slowdown on the mixed workload** — confirmed unchanged from
   May 14. The mixed mix is I/O-dominated, so free-threading's per-thread
   interpreter overhead applies without the parallel-CPU benefit. The
   cpu-only profile inverts this: 2.0–2.25 × speedup on free-threaded aio
   versus regular 3.14.

2. **aio-mixed pool doesn't dynamically rebalance.** If `--variant mixed`
   alternates sync/async tasks across the 2 sync threads + 2 loop workers,
   the sync side becomes a hard 2-slot bottleneck for sync-flavored tasks
   even when the async slots are idle. On `io-only` this caps the mixed pool
   at the same 7.9 TPS as a 4-slot sync pool. Worth knowing if the workload
   profile is unknown at deploy time.
