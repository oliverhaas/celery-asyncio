"""Send example tasks and print results.

Start the worker first (from this directory):
    PYTHONPATH=. python -m celery -A celeryapp worker --loglevel=info -E

Then run this script:
    PYTHONPATH=. python run.py
"""

import time
from datetime import UTC, datetime, timedelta

from celeryapp import app
from tasks import add, flaky_task, multiply, slow_add

TIMEOUT = 30


def wait_for_result(async_result, timeout=TIMEOUT):
    """Poll the backend for the task result (sync drainer not available)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = app.backend.get_task_meta(async_result.id)
        if meta["status"] == "SUCCESS":
            return meta["result"]
        if meta["status"] == "FAILURE":
            raise RuntimeError(f"Task failed: {meta.get('result')}")
        if meta["status"] == "REVOKED":
            raise RuntimeError("Task was revoked")
        time.sleep(0.25)
    raise TimeoutError(f"Task {async_result.id} did not complete within {timeout}s")


def main():
    print("=" * 60)
    print("celery-asyncio example")
    print("=" * 60)

    # --- Basic task ---
    print("\n1) Basic task: add(2, 3)")
    result = add.delay(2, 3)
    print(f"   result = {wait_for_result(result)}")

    # --- Async task ---
    print("\n2) Async task: slow_add(10, 20) — runs on the event loop")
    result = slow_add.delay(10, 20)
    print(f"   result = {wait_for_result(result)}")

    # --- Delayed task (countdown) ---
    print("\n3) Delayed task: add(10, 20) with countdown=3s")
    result = add.apply_async(args=(10, 20), countdown=3)
    print("   waiting for delayed result...")
    print(f"   result = {wait_for_result(result)}")

    # --- Delayed task (eta) ---
    eta = datetime.now(tz=UTC) + timedelta(seconds=3)
    print(f"\n4) ETA task: multiply(6, 7) with eta={eta.isoformat()}")
    result = multiply.apply_async(args=(6, 7), eta=eta)
    print("   waiting for eta result...")
    print(f"   result = {wait_for_result(result)}")

    # --- Priority tasks ---
    print("\n5) Priority tasks: three add() calls with different priorities")
    low = add.apply_async(args=(1, 1), priority=0)
    mid = add.apply_async(args=(2, 2), priority=128)
    high = add.apply_async(args=(3, 3), priority=255)
    print(f"   low  (priority=0):   {wait_for_result(low)}")
    print(f"   mid  (priority=128): {wait_for_result(mid)}")
    print(f"   high (priority=255): {wait_for_result(high)}")

    # --- Retry task ---
    print("\n6) Flaky task (retries up to 3 times):")
    result = flaky_task.delay()
    try:
        print(f"   result = {wait_for_result(result)}")
    except RuntimeError:
        # With 50% fail rate and max_retries=3, there's a ~6% chance
        # all 4 attempts fail — that's expected, retries still worked.
        print("   exhausted all retries (expected sometimes with 50% fail rate)")

    print("\n" + "=" * 60)
    print("All tasks completed successfully!")
    print("=" * 60)


if __name__ == "__main__":
    main()
