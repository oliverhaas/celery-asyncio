# Benchmark Results

**Date:** 2026-05-14
**Hardware:** Worker pinned to 4 cores (Intel i7-8565U @ 1.80GHz) via `taskset -c 0,1,2,3`
**Broker / backend:** valkey 8 on `localhost:6379` (via docker-compose)
**Workload:** 30,000 tasks per config, deterministic mix from seed 42:
- 40% I/O-heavy   (cpu_iters=200, sleep=50 ms, mem=16 KiB)
- 30% CPU-heavy   (cpu_iters=4 000, sleep=0,  mem=32 KiB)
- 20% mem-heavy   (cpu_iters=100, sleep=10 ms, mem=8 192 KiB)
- 10% balanced    (cpu_iters=1 000, sleep=20 ms, mem=256 KiB)

## Full results

Sorted by wall-clock time. `wall` is end-to-end from first-publish to last-result-observed.
`mean_cpu` and `mean_rss` are the average of 0.5 s samples of the worker process tree;
`mean_cpu` can exceed 100% on multi-core (it is summed across cores).

| config                  | py    | variant | workers              | wall    | TPS    | peak RSS | mean RSS | peak CPU | mean CPU | stranded |
| ----------------------- | ----- | ------- | -------------------- | ------: | -----: | -------: | -------: | -------: | -------: | -------: |
| classic-prefork4-314    | 3.14  | sync    | prefork × 4          | 210.5 s | 142.5  |  283 MB  |  283 MB  |    39 %  |    21 %  |    0     |
| classic-prefork4-314t   | 3.14t | sync    | prefork × 4          | 217.4 s | 138.0  |  344 MB  |  332 MB  |    37 %  |    20 %  |    0     |
| aio-mixed-l2c50-s2-314  | 3.14  | mixed   | 2 loop ×50 + 2 sync  | 290.5 s | 103.2  |   94 MB  |   89 MB  |    93 %  |    60 %  |   29     |
| aio-sync-s4-314         | 3.14  | sync    | 4 sync threads       | 291.0 s | 102.9  |   79 MB  |   78 MB  |    89 %  |    59 %  |   48     |
| aio-async-l4c25-314     | 3.14  | async   | 4 loop × 25 + 1 sync | 301.7 s |  99.4  |  108 MB  |   99 MB  |   108 %  |    63 %  |   21     |
| aio-sync-s4-314t        | 3.14t | sync    | 4 sync threads       | 348.4 s |  86.0  |   99 MB  |   80 MB  |    90 %  |    56 %  |   32     |
| aio-mixed-l2c50-s2-314t | 3.14t | mixed   | 2 loop ×50 + 2 sync  | 352.2 s |  85.1  |   92 MB  |   75 MB  |    90 %  |    59 %  |   25     |
| aio-async-l4c25-314t    | 3.14t | async   | 4 loop × 25 + 1 sync | 378.7 s |  79.2  |   85 MB  |   71 MB  |    89 %  |    61 %  |   23     |
| classic-threads4-314    | 3.14  | sync    | threads × 4          | 540.5 s |  55.5  |   86 MB  |   86 MB  |   102 %  |    22 %  |    0     |
| classic-threads4-314t   | 3.14t | sync    | threads × 4          | 562.7 s |  53.3  |  105 MB  |   86 MB  |   101 %  |    27 %  |    0     |
| classic-prefork1-314    | 3.14  | sync    | prefork × 1          | 868.9 s |  34.5  |  111 MB  |  111 MB  |    16 %  |     7 %  |    0     |
| classic-prefork1-314t   | 3.14t | sync    | prefork × 1          | 899.7 s |  33.3  |  135 MB  |  132 MB  |    15 %  |     7 %  |    0     |

## Observations

**Prefork scales near-linearly.** `prefork4` is 4.12× `prefork1` (142 vs 34.5 TPS).
Each worker process gets its own broker connection and its own GIL, so on a 4-core
budget the I/O sleeps overlap cleanly across processes.

**celery-asyncio's per-process throughput is ~3× single-worker prefork.** At similar
RSS (≈ 80–110 MB) the aio pool delivers 99–103 TPS on 3.14 versus 34.5 TPS for
single-process prefork. The aio pool drives its event loops to 60% mean CPU even
on a single worker process.

**3.14t (free-threaded) costs ~17 % wall-clock on this workload.** The mix is
I/O-bound (60% of tasks sleep), so the parallel-CPU gain free-threading offers
doesn't materialise, while the per-thread interpreter slowdown still applies.
Free-threading is a win when the workload is CPU-heavy enough to benefit from
multi-core compute *and* you have more cores than worker processes.

**Classic threads pool is uncompetitive in both Python versions** (53–56 TPS).
Free-threading doesn't rescue it: kombu/celery's pure-Python paths contain
locks and a single broker connection that re-serialise the work regardless of
the GIL state.

**prefork4 uses ~3× the RSS** of any aio config (283 MB vs ~80–110 MB) — that's
the price of multiprocessing's per-process overhead. On 3.14t prefork's RSS is
worse still (344 MB) because free-threaded CPython has a larger interpreter
footprint per child.

## Cost per task (AWS Fargate, Linux/x86, us-east-1)

**Pricing reference (as of May 2026):** $0.04048 per vCPU·hour, $0.004445 per GB·hour
(see [AWS Fargate Pricing](https://aws.amazon.com/fargate/pricing/)). Converted to
per-second: ~$1.124e-5 per vCPU·second, ~$1.235e-6 per GB·second. ARM Graviton is
~20 % cheaper but not modelled here.

**Two cost models:**

- **Idealised** -- pay only for the resources the worker actually consumed:
  `cost = (mean_cpu × wall × vCPU·s rate) + (mean_rss × wall × GB·s rate)`.
  Fair apples-to-apples comparison of compute efficiency.
- **Provisioned** -- pay for a Fargate slot sized to the worker's peak:
  CPU rounded up to {0.25, 0.5, 1, 2, 4} vCPU based on `peak_cpu`, memory rounded
  up to the next 0.5 GiB above `peak_rss × 1.25` for headroom. Closer to a real
  deployment bill where you over-provision.

Sorted by idealised cost per million tasks:

| config                  | TPS   | vCPU·s / 1 k | MB·s / 1 k |  ideal $/1M |  prov $/1M | Fargate slot |
| ----------------------- | ----: | -----------: | ---------: | ----------: | ---------: | -----------: |
| classic-prefork4-314    | 142.5 |        1.45  |    1 986   |   **$0.02** |     $0.04  | 0.5 vCPU / 0.5 GB |
| classic-prefork4-314t   | 138.0 |        1.49  |    2 407   |     $0.02   |     $0.05  | 0.5 vCPU / 0.5 GB |
| classic-prefork1-314    |  34.5 |        2.00  |    3 201   |     $0.03   |     $0.10  | 0.25 vCPU / 0.5 GB |
| classic-prefork1-314t   |  33.3 |        2.01  |    3 959   |     $0.03   |     $0.10  | 0.25 vCPU / 0.5 GB |
| classic-threads4-314    |  55.5 |        3.93  |    1 546   |     $0.05   |     $0.42  | 2 vCPU / 0.5 GB |
| classic-threads4-314t   |  53.3 |        5.12  |    1 608   |     $0.06   |     $0.43  | 2 vCPU / 0.5 GB |
| aio-sync-s4-314         | 102.9 |        5.73  |      758   |     $0.07   |     $0.12  | 1 vCPU / 0.5 GB |
| aio-mixed-l2c50-s2-314  | 103.2 |        5.78  |      858   |     $0.07   |     $0.11  | 1 vCPU / 0.5 GB |
| aio-async-l4c25-314     |  99.4 |        6.34  |      995   |     $0.07   |     $0.23  | 2 vCPU / 0.5 GB |
| aio-sync-s4-314t        |  86.0 |        6.56  |      934   |     $0.07   |     $0.14  | 1 vCPU / 0.5 GB |
| aio-mixed-l2c50-s2-314t |  85.1 |        6.90  |      877   |     $0.08   |     $0.14  | 1 vCPU / 0.5 GB |
| aio-async-l4c25-314t    |  79.2 |        7.70  |      891   |     $0.09   |     $0.15  | 1 vCPU / 0.5 GB |

### What this means

**prefork wins on $/task by 3–4×.** Counter-intuitive at first glance: aio is faster
overall on a single worker (~3× the TPS of `prefork1`), yet costs more per task.

Reason: the `io_heavy` portion of the workload (40% of tasks, 50 ms sleep each)
costs **zero CPU** under prefork — the worker process really is idle during
`time.sleep`. Under aio the event loop continues servicing other concurrent
tasks during that 50 ms, charging CPU for that scheduling work even though
each individual task is "sleeping". aio trades CPU for concurrency, which is
the right trade when latency or single-process throughput matters; prefork
trades wall-clock for CPU efficiency, which is the right trade when total cost
is what you optimise for.

**aio's memory advantage is real, but small in absolute $.** aio uses
0.76–1.0 GB·s per 1 k tasks vs prefork-4's 2.0 GB·s, but the memory cost
component is two orders of magnitude smaller than the CPU cost on Fargate, so
this barely moves the needle.

**Free-threading costs an extra ~17–22 %** on every config that uses threads
(both classic-threads and aio). For purely CPU-parallel workloads on more cores
this would invert; on this I/O-heavy mix the per-thread interpreter overhead
dominates.

**Caveats:**

- Numbers are based on a 30 000-task batch. Short-burst overhead (worker
  startup, broker connect) is amortised over a long run; for tiny batches the
  ranking can shift.
- Real Fargate has a 1-minute minimum and discrete slot sizes that aren't
  modelled in the idealised column.
- ARM (Graviton) Fargate is ~20 % cheaper if your code is compatible.

## Cost per task (Kubernetes on EC2)

Fargate's per-slot pricing isn't the only option. On managed Kubernetes you
pay for the underlying EC2 node, and pods can request a low CPU share and
burst above it whenever the node has spare capacity. That model rewards low
average CPU usage and penalises poor bin-packing.

We derive implied per-resource rates from
[`m6i.large`](https://instances.vantage.sh/aws/ec2/m6i.large) (2 vCPU + 8 GB
at $0.096/h, us-east-1) and
[`m7g.large`](https://instances.vantage.sh/aws/ec2/m7g.large) (Graviton ARM,
$0.0816/h) by splitting the hourly price 9.1 : 1 between CPU and memory --
the same ratio Fargate uses. Two utilisation factors:

- **100 %** -- theoretical lower bound assuming the cluster is always full.
- **60 %** -- realistic for clusters with daemonsets, scheduling slack, and
  burst headroom.

| config                  | TPS   | Fargate ideal | k8s m6i 100% | k8s m6i 60% | k8s m7g 60% |
| ----------------------- | ----: | ------------: | -----------: | ----------: | ----------: |
| classic-prefork4-314    | 142.5 |       $0.019  |      $0.015  | **$0.026**  | **$0.022**  |
| classic-prefork4-314t   | 138.0 |       $0.020  |      $0.016  |    $0.027   |    $0.023   |
| classic-prefork1-314    |  34.5 |       $0.026  |      $0.022  |    $0.036   |    $0.031   |
| classic-prefork1-314t   |  33.3 |       $0.027  |      $0.023  |    $0.038   |    $0.032   |
| classic-threads4-314    |  55.5 |       $0.046  |      $0.038  |    $0.063   |    $0.054   |
| classic-threads4-314t   |  53.3 |       $0.060  |      $0.049  |    $0.082   |    $0.069   |
| aio-sync-s4-314         | 102.9 |       $0.065  |      $0.054  |    $0.090   |    $0.076   |
| aio-mixed-l2c50-s2-314  | 103.2 |       $0.066  |      $0.054  |    $0.091   |    $0.077   |
| aio-async-l4c25-314     |  99.4 |       $0.072  |      $0.060  |    $0.099   |    $0.085   |
| aio-sync-s4-314t        |  86.0 |       $0.075  |      $0.062  |    $0.103   |    $0.087   |
| aio-mixed-l2c50-s2-314t |  85.1 |       $0.079  |      $0.065  |    $0.108   |    $0.092   |
| aio-async-l4c25-314t    |  79.2 |       $0.088  |      $0.072  |    $0.120   |    $0.102   |

The prefork-vs-aio ratio holds across all hosting models; the choice of
provider changes absolute cost, not the ranking. Graviton saves ~15 %
uniformly. Fargate's idealised column tracks k8s-EC2 at ~60 % utilisation
because Fargate's slot premium and k8s's slack happen to cancel out.

## Cost per task at 800 MB per worker process

The bench app is bare-bones (~70-110 MB RSS per process). A typical
production Django/Celery worker with ORM cache, framework imports, and app
code loaded sits closer to **800 MB per process**. CPU usage per task is the
same; only the memory baseline scales. For prefork we multiply by the number
of worker processes (each child gets its own 800 MB after fork copy-on-write
drift); aio and threads keep a single process and pay for 800 MB once.

| config                  | procs | RSS modelled | k8s m6i 60% | k8s m7g 60% |
| ----------------------- | ----: | -----------: | ----------: | ----------: |
| classic-prefork4-314    |   4   |    3 200 MB  | **$0.060**  | **$0.051**  |
| classic-prefork4-314t   |   4   |    3 200 MB  |    $0.061   |    $0.052   |
| classic-prefork1-314    |   1   |      800 MB  |    $0.069   |    $0.059   |
| classic-prefork1-314t   |   1   |      800 MB  |    $0.071   |    $0.060   |
| classic-threads4-314    |   1   |      800 MB  |    $0.085   |    $0.072   |
| aio-sync-s4-314         |   1   |      800 MB  |    $0.101   |    $0.086   |
| aio-mixed-l2c50-s2-314  |   1   |      800 MB  |    $0.102   |    $0.087   |
| classic-threads4-314t   |   1   |      800 MB  |    $0.104   |    $0.088   |
| aio-async-l4c25-314     |   1   |      800 MB  |    $0.111   |    $0.094   |
| aio-sync-s4-314t        |   1   |      800 MB  |    $0.117   |    $0.099   |
| aio-mixed-l2c50-s2-314t |   1   |      800 MB  |    $0.122   |    $0.104   |
| aio-async-l4c25-314t    |   1   |      800 MB  |    $0.136   |    $0.115   |

The aio configs are now ~4× more memory-efficient than prefork-4 in absolute
terms (800 vs 3 200 MB), but the gap closes only to 1.85× ($0.060 vs $0.111
on k8s+m6i+60 %), not all the way: aio still spends ~4× more CPU per task,
and CPU is ~9× more valuable per unit than memory in Fargate's price ratio.

### Crossover memory

Solving `prefork4-314` vs `aio-async-l4c25-314` for break-even cost-per-task:

- aio's extra CPU per task: `0.00634 - 0.00145 = 0.00489 vCPU·s` -> +$5.5e-8/task
- prefork's extra memory per task: `(4·M - M) × 210.5 / 30 000 = 0.0211·M MB·s`
  (here M = MB per worker process)

Setting the two costs equal gives **M ≈ 2 500 MB per process**. Below that
prefork is cheaper; above that aio is cheaper.

| RSS per process | prefork-4 $/1M | aio-async $/1M | winner            |
| --------------: | -------------: | -------------: | ----------------- |
|        100 MB   |      $0.027    |     $0.099     | prefork (3.7×)    |
|        800 MB   |      $0.060    |     $0.111     | prefork (1.87×)   |
|      1 500 MB   |      $0.092    |     $0.123     | prefork (1.33×)   |
|      2 500 MB   |      $0.139    |     $0.139     | tied              |
|      5 000 MB   |      $0.255    |     $0.181     | aio (1.41×)       |
|     10 000 MB   |      $0.487    |     $0.264     | aio (1.84×)       |

So the answer to *"when does asyncio start to win on cost?"* is:
**once each worker's baseline memory exceeds ~2.5 GB.** That's plausible for
workers with large ML models or big in-memory caches loaded at boot, but
unusual for typical web-app workers in the ~0.5–1 GB range. The break-even
point also depends on the workload's CPU/IO mix: more I/O-heavy workloads
shrink aio's CPU surplus and drop the crossover memory; pure CPU-bound work
pushes it out of reach because there is no idle time for prefork's `sleep`
to be "free".

## Known issues surfaced

1. **kombu-asyncio strands ~0.05–0.16 % of tasks** in the redis-transport's
   `messages_index` ZSET. Root cause: when `Channel.drain_events` cancels its
   `_consume_regular` task on timeout, the cancellation can arrive while the
   atomic consume Lua script has already executed on the redis server. The
   message is popped from the queue ZSET and the index ZSET's score is bumped
   to `now + visibility_timeout + RCI = 360 s`, but the Python coroutine raises
   `CancelledError` before reading the reply — the payload is lost from the
   worker process. The default visibility-timeout restore eventually
   re-enqueues those tasks ~6 min later; this benchmark's stall-timeout of 15 s
   declares "done" first, recording the leak in `n_stranded`.

2. **3.14t async slowdown is real, not a free-threading bug.** Both loop
   threads run in true parallel on 3.14t — verified via per-thread CPU times
   on a CPU-bound microbench (~25 s user-time each thread, 25 s wall-clock =
   2× parallelism). The slowdown comes entirely from free-threaded CPython's
   per-thread interpreter overhead, which the I/O-heavy workload here can't
   amortise.
