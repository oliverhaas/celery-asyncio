"""Built-in transports - Pure asyncio version.

Currently supported transports:
- valkey/redis: Valkey/Redis using valkey.asyncio or redis.asyncio
- amqp: AMQP 0.9.1 via aio-pika
- memory: In-memory transport using asyncio.Queue
- filesystem: File-system based transport using asyncio.to_thread
"""
