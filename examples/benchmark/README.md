# Benchmark: celery-asyncio vs classic celery

Apples-to-apples comparison of celery-asyncio against upstream Celery on a
deterministic, mixed CPU/I/O/memory workload. Worker is pinned to two CPUs
via `taskset -c 0,1` so every config sees a "2-CPU server".

## Task profile

One task with three knobs:

- `cpu_iters` -- inner-loop count of float arithmetic
- `io_seconds` -- sleep (`time.sleep` for sync, `asyncio.sleep` for async)
- `mem_kb` -- bytearray allocated for the task lifetime (touched per page)

Workload is a fixed mix produced by `workload.py` from a seeded RNG:

| profile    | weight | cpu_iters | io     | mem      |
| ---------- | -----: | --------: | -----: | -------: |
| io_heavy   |   40 % |       200 | 50 ms  |   16 KiB |
| cpu_heavy  |   30 % |     4 000 |  0 ms  |   32 KiB |
| mem_heavy  |   20 % |       100 | 10 ms  | 8192 KiB |
| balanced   |   10 % |     1 000 | 20 ms  |  256 KiB |

The exact same workload manifest is fed to every config.

## Configurations

celery-asyncio (Python 3.14 and 3.14t):
- `aio-async`  -- async-def task, 2 loop workers x 50 concurrency, 2 sync workers
- `aio-sync`   -- sync-def task, same pool (routed to sync threads)
- `aio-mixed`  -- alternating sync/async, same pool

Classic celery (Python 3.14, and 3.14t best-effort):
- `classic-prefork2`  -- `--pool prefork --concurrency 2`
- `classic-threads8`  -- `--pool threads --concurrency 8`

## Quick start

```bash
# 1. Broker
docker compose up -d

# 2. Venvs (creates .venv-async-314, .venv-async-314t, .venv-classic-314, .venv-classic-314t)
./setup_venvs.sh

# 3. Smoke test
python run_all.py --tasks 500

# 4. Full run
python run_all.py --tasks 10000

# Run a single config only:
python run_all.py --tasks 5000 --only aio-async-314
```

Results land in `results/<config>.json` and a summary table is printed at
the end. Each JSON file has the full per-tick (CPU%, RSS) sample series
under `samples`.

## Measurement

`runner.py` starts the worker in a subprocess, waits for `inspect ping`,
samples the worker process tree's `cpu_percent()` and `memory_info().rss`
every 0.5 s, publishes all tasks at once, and polls `AsyncResult.ready()`
until every task is done. Wall clock is start-of-publish to last-completed.
The sampling thread sums across the worker and all its children (so prefork
multiprocessing is captured).
