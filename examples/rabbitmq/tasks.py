"""Example tasks demonstrating celery-asyncio with RabbitMQ."""

import asyncio
import random

from celeryapp import app


@app.task
def add(x, y):
    return x + y


@app.task
def multiply(x, y):
    return x * y


@app.task
async def slow_add(x, y):
    """Async task - runs natively on the asyncio event loop."""
    await asyncio.sleep(1)
    return x + y


@app.task
async def fast_async(x, y):
    """Lightweight async task - sleeps briefly, ideal for concurrency testing."""
    await asyncio.sleep(0.01)
    return x + y


@app.task
async def sleep_task(duration=0.1):
    """Async task with configurable sleep duration for concurrency testing."""
    await asyncio.sleep(duration)
    return duration


@app.task(bind=True, max_retries=3)
def flaky_task(self):
    """Task that fails randomly to demonstrate retry behaviour."""
    if random.random() < 0.5:
        raise self.retry(countdown=1)
    return "succeeded after retries"
