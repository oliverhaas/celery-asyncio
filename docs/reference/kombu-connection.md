# Connection

`kombu.Connection` is the broker connection. It is an async context manager;
the synchronous form exists for sync callers such as Flower and is driven by a
long-lived background loop.

```python
async with Connection("redis://localhost:6379") as conn:
    channel = await conn.default_channel()
```

## Connection

::: kombu.connection.Connection
