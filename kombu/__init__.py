"""Kombu - Pure asyncio messaging library for Python.

This is an asyncio-native version of Kombu supporting Redis, AMQP, Memory, and Filesystem transports.
All operations are async.

Example:
    async with Connection('redis://localhost') as conn:
        async with conn.Producer() as producer:
            await producer.publish({'hello': 'world'}, routing_key='my_queue')

        async with conn.SimpleQueue('my_queue') as queue:
            message = await queue.get(timeout=5)
            print(message.payload)
            await message.ack()
"""

import re
from collections import namedtuple
from typing import cast

__version__ = "6.0.0a5"
__author__ = "Oliver Haas"
__contact__ = "ohaas@e1plus.de"
__homepage__ = "https://oliverhaas.github.io/celery-asyncio/"
__docformat__ = "restructuredtext en"

# Version info
version_info_t = namedtuple(
    "version_info_t",
    (
        "major",
        "minor",
        "micro",
        "releaselevel",
        "serial",
    ),
)

_temp = cast("re.Match", re.match(r"(\d+)\.(\d+).(\d+)(.+)?", __version__)).groups()
VERSION = version_info = version_info_t(int(_temp[0]), int(_temp[1]), int(_temp[2]), _temp[3] or "", "")
del _temp

# Public API exports
from .common import Broadcast, eventloop, maybe_declare  # noqa: E402
from .connection import Connection  # noqa: E402
from .entity import Exchange, Queue, binding  # noqa: E402
from .message import Message  # noqa: E402
from .messaging import Consumer, Producer  # noqa: E402
from .mixins import ConsumerMixin  # noqa: E402
from .serialization import (  # noqa: E402
    disable_insecure_serializers,
    enable_insecure_serializers,
)
from .simple import SimpleBuffer, SimpleQueue  # noqa: E402

__all__ = (
    # Version
    "VERSION",
    "Broadcast",
    # Connection
    "Connection",
    "Consumer",
    # Mixins
    "ConsumerMixin",
    # Entities
    "Exchange",
    # Message
    "Message",
    # Messaging
    "Producer",
    "Queue",
    "SimpleBuffer",
    # Simple API
    "SimpleQueue",
    "binding",
    "disable_insecure_serializers",
    # Serialization
    "enable_insecure_serializers",
    # Utilities
    "eventloop",
    "maybe_declare",
    "version_info",
)
