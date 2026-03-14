"""Celery app using RabbitMQ (AMQP) broker with Redis result backend."""

from celery import Celery

app = Celery("rabbitmq_example")

app.config_from_object(
    {
        "broker_url": "amqp://guest:guest@localhost:5672//",
        "result_backend": "redis://localhost:6379/1",
        "include": ["tasks"],
    },
)
