"""Send example tasks via Django's task API and print results.

Start the worker first:
    celery -A proj worker --loglevel=info -E

Then run this script:
    python run.py
"""

import os
import time
from datetime import UTC, datetime, timedelta

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")

import django

django.setup()

from proj.tasks import add, high_priority_task, multiply, slow_add, task_with_context

TIMEOUT = 30
POLL_INTERVAL = 0.2


def wait_for_result(task_obj, result):
    """Poll until the task result is finished or timeout is reached."""
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        result = task_obj.get_result(result.id)
        if result.is_finished:
            return result
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Task {result.id} did not finish within {TIMEOUT}s")


def main():
    print("=" * 60)
    print("Django Tasks + celery-asyncio example")
    print("=" * 60)

    # --- Basic task ---
    print("\n1) Basic task: add(2, 3)")
    result = wait_for_result(add, add.enqueue(2, 3))
    print(f"   result = {result.return_value}")

    # --- Async task ---
    print("\n2) Async task: slow_add(10, 20) — async def with 1s sleep")
    result = wait_for_result(slow_add, slow_add.enqueue(10, 20))
    print(f"   result = {result.return_value}")

    # --- Delayed task (run_after with timedelta-style datetime) ---
    eta = datetime.now(tz=UTC) + timedelta(seconds=3)
    print(f"\n3) Delayed task: add(10, 20) with run_after=+3s")
    delayed = add.using(run_after=eta)
    result = wait_for_result(add, delayed.enqueue(10, 20))
    print(f"   result = {result.return_value}")

    # --- ETA task ---
    eta = datetime.now(tz=UTC) + timedelta(seconds=3)
    print(f"\n4) ETA task: multiply(6, 7) with run_after={eta.isoformat()}")
    result = wait_for_result(multiply, multiply.using(run_after=eta).enqueue(6, 7))
    print(f"   result = {result.return_value}")

    # --- Priority tasks ---
    print("\n5) Priority tasks: three add() calls with different priorities")
    low = add.using(priority=-50).enqueue(1, 1)
    mid = add.enqueue(2, 2)  # priority=0 (default)
    high = add.using(priority=50).enqueue(3, 3)
    print(f"   low  (priority=-50): {wait_for_result(add, low).return_value}")
    print(f"   mid  (priority=0):   {wait_for_result(add, mid).return_value}")
    print(f"   high (priority=50):  {wait_for_result(add, high).return_value}")

    # --- High-priority task (set at definition time) ---
    print("\n6) High-priority task (priority=50 in @task decorator):")
    result = wait_for_result(high_priority_task, high_priority_task.enqueue("hello"))
    print(f"   result = {result.return_value}")

    # --- Task with context ---
    print("\n7) Task with context (takes_context=True):")
    result = wait_for_result(task_with_context, task_with_context.enqueue("world"))
    print(f"   result = {result.return_value}")

    print("\n" + "=" * 60)
    print("All tasks completed successfully!")
    print("=" * 60)


if __name__ == "__main__":
    main()
