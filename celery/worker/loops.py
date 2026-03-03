"""The consumer's async event loop.

In celery-asyncio, this is a thin wrapper around connection.drain_events().
The old Hub-based asynloop and blocking synloop are removed.
"""


async def asynloop(
    obj, connection, consumer, blueprint, qos=None, amqheartbeat=None, clock=None, amqheartbeat_rate=None, **kwargs
):
    """Async consumer event loop.

    Drains events from the broker connection using native asyncio.

    Called with arguments from Consumer.loop_args():
        obj: Consumer instance
        connection: broker connection
        consumer: task consumer (kombu Consumer)
        blueprint: consumer blueprint
        qos: QoS manager
        amqheartbeat: AMQP heartbeat interval
        clock: Lamport clock
        amqheartbeat_rate: heartbeat check rate
    """
    # Create the task message handler and register it on the consumer.
    # kombu callbacks receive (body, message) but celery's handler expects (message).
    on_task_received = obj.create_task_handler()
    consumer.register_callback(lambda body, message: on_task_received(message))
    await consumer.consume()

    # Notify that the consumer is ready
    obj.on_ready()

    while blueprint.state == 1:  # RUN
        try:
            await connection.drain_events(timeout=1.0)
        except TimeoutError:
            pass
        except OSError:
            break
