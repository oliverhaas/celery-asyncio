# Changelog

## v6.0.0a2 (in development)

Initial alpha of celery-asyncio.

### What works

- Async worker with hybrid asyncio + thread pool
- `async def` and regular `def` tasks in the same worker
- Redis transport (with sorted-set priority queues, Lua scripts, fanout)
- AMQP transport (via aio-pika, RabbitMQ)
- Full CLI (`celery worker`, `celery inspect`, `celery control`, `celery result`)
- Task events and Celery Flower monitoring
- Worker restart (max tasks, max memory, stuck threads)
- Task timeouts (soft and hard, async and sync)
- Django 6.0 Tasks support via [django-tasks-celery](https://github.com/oliverhaas/django-tasks-celery)
- Delayed/scheduled tasks (countdown, eta)
- Task priority
- Task retries

### What's not yet tested

- Multi-worker deployments
- Rate limiting and autoscaling

### Breaking changes from upstream Celery

- Requires Python 3.14+
- Requires kombu-asyncio (not upstream kombu)
- Removed eventlet, gevent, and prefork pool backends
- Removed billiard dependency
- Default pool is `asyncio` (not `prefork`)
- Bootsteps are async (`async def start/stop`)
