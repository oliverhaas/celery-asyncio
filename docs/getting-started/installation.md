# Installation

## Requirements

- Python 3.14+
- Valkey 8+ or Redis 7+ or RabbitMQ 4+

## Install with uv

```console
uv add celery-asyncio
```

## Install with pip

```console
pip install celery-asyncio
```

## Transport extras

```console
# Valkey/Redis transport (recommended)
uv add celery-asyncio[valkey]
# or: uv add celery-asyncio[redis]

# AMQP transport (RabbitMQ)
uv add celery-asyncio[amqp]
```

## Optional extras

```console
# Django integration
uv add celery-asyncio[django]

# Serializers
uv add celery-asyncio[msgpack]
uv add celery-asyncio[yaml]
```
