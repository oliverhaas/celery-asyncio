"""Fire a burst of tasks for load testing / Flower demo.

Usage:
    PYTHONPATH=. python load.py              # 200 tasks (default)
    PYTHONPATH=. python load.py 1000         # 1000 tasks
    PYTHONPATH=. python load.py 500 --async  # 500 slow async tasks (1s sleep)
    PYTHONPATH=. python load.py 100000 --fast  # 100k fast async tasks (10ms sleep)
"""

import random
import sys
import time

from tasks import add, fast_async, multiply, slow_add

DEFAULT_COUNT = 200


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_COUNT
    async_only = "--async" in sys.argv
    fast_only = "--fast" in sys.argv

    print(f"Sending {count} tasks...")
    t0 = time.monotonic()

    for i in range(count):
        x, y = random.randint(1, 100), random.randint(1, 100)
        if fast_only:
            fast_async.delay(x, y)
        elif async_only:
            slow_add.delay(x, y)
        else:
            # Mix of task types: 50% add, 30% multiply, 20% slow_add
            r = random.random()
            if r < 0.5:
                add.delay(x, y)
            elif r < 0.8:
                multiply.delay(x, y)
            else:
                slow_add.delay(x, y)

    elapsed = time.monotonic() - t0
    rate = count / elapsed if elapsed > 0 else float("inf")
    print(f"Sent {count} tasks in {elapsed:.2f}s ({rate:.0f} tasks/s)")
    print("Watch progress at http://localhost:5555")


if __name__ == "__main__":
    main()
