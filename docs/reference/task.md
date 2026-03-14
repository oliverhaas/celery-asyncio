# Task

The `Task` class is the base for all celery tasks. Use the `@app.task` decorator
to create tasks.

## Task

::: celery.app.task.Task
    options:
      members:
        - name
        - max_retries
        - default_retry_delay
        - rate_limit
        - time_limit
        - soft_time_limit
        - ignore_result
        - typing
        - acks_late
        - reject_on_worker_lost
        - bind
        - apply_async
        - delay
        - apply
        - retry
        - reject
        - on_success
        - on_failure
        - on_retry
        - after_return
        - request
