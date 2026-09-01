# Producers and Consumers

Publishing and consuming run over a [`Connection`](kombu-connection.md).

```python
async with Connection("redis://localhost:6379") as conn:
    async with conn.Producer() as producer:
        await producer.publish({"hello": "world"}, routing_key="my_queue")
```

## Producer

::: kombu.messaging.Producer

## Consumer

::: kombu.messaging.Consumer

## Message

::: kombu.message.Message
