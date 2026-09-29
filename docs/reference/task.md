# Task

The `Task` class is the base for all celery tasks. Use the `@app.task` decorator
to create tasks.

## Task names

A task is registered under its name, which defaults to the module and name of
the decorated function, such as `tasks.add`. `@app.task(name="...")` sets it
explicitly.

Two different callables in one app can end up with the same name, such as two
closures made by one factory, or two functions of the same name in one module.
The second registration issues a `DuplicateTaskNameWarning`, once per name, that
names both callables and points at the line that registered the second. The
decorator keeps the first callable and returns its task, so calls meant for the
second run the first, while `app.register_task` replaces the task with the new
one. Registering the same function again does not warn, and neither does a name
held by a task of another app, as in a registry shared through
`Celery(tasks=...)`.

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
