# Testing

`celery.contrib.pytest` ships fixtures that hand a test a configured app and,
when it needs one, a worker running in a thread. The distribution does not
register the plugin through an entry point, so pytest has to be told about it.
Put this in the project's root `conftest.py`:

```python
pytest_plugins = ("celery.contrib.pytest",)
```

The `pytest` extra pulls in pytest itself:

```console
pip install "celery-asyncio[pytest]"
```

## Configuring the test app

`celery_app` is a `Celery` instance built for one test. Override `celery_config`
to say which broker and result backend it uses:

```python
import pytest

pytest_plugins = ("celery.contrib.pytest",)


@pytest.fixture(scope="session")
def celery_config():
    return {
        "broker_url": "memory://",
        "result_backend": "cache+memory://",
    }
```

`memory://` keeps the whole round trip inside the process, which is enough for
most task tests. Point `broker_url` at `redis://` or `amqp://` when the test is
about transport behaviour.

## Running a task in a worker

`celery_worker` starts a worker in a thread on the shared event loop and stops
it when the test returns. A task registered after the worker started is not in
its strategy table, so call `reload()` before dispatching:

```python
import pytest


@pytest.fixture
def add(celery_app):
    @celery_app.task(name="add")
    async def add(x, y):
        return x + y

    return add


def test_add(celery_worker, add):
    celery_worker.reload()
    assert add.delay(2, 3).get(timeout=10) == 5
```

Without the `reload()` the worker answers with
`Received unregistered task of type 'add'` and the result never arrives.

For tasks that live in a module, name the module in `celery_includes` instead.
The fixture imports it before the worker starts, so no `reload()` is needed:

```python
@pytest.fixture(scope="session")
def celery_includes():
    return ["myapp.tasks"]
```

## Async tasks

Calling an `async def` task directly returns a coroutine, so a test that
exercises the body without a broker has to await it:

```python
async def test_add_body(add):
    assert await add(2, 3) == 5
```

Through a worker there is no difference between an async and a sync task:
`delay()` and `apply_async()` return an `AsyncResult` for both. `adelay()` and
`aapply_async()` are the async-native equivalents for when the test is itself a
coroutine.

## One worker for the whole session

Starting and stopping a worker for every test adds up. The session-scoped
`celery_session_app` and `celery_session_worker` start one worker for the whole
run instead. They read the same `celery_config`, `celery_includes` and
`celery_worker_parameters` fixtures, all of which are session-scoped already.
Tasks have to be registered on `celery_session_app`, not on `celery_app`: the
two are different instances.

## Available fixtures

| Fixture | Scope | What it does |
|---------|-------|--------------|
| `celery_app` | function | A `Celery` instance configured from `celery_config` |
| `celery_worker` | function | A worker on that app, started in a thread |
| `celery_session_app` | session | One app for the whole run |
| `celery_session_worker` | session | One worker for the whole run |
| `celery_config` | session | Override to configure the app; returns `{}` by default |
| `celery_parameters` | session | Override to change the `Celery()` constructor arguments |
| `celery_worker_parameters` | session | Override to change the `WorkController` arguments, for example the queues to consume |
| `celery_includes` | session | Override to return module names the worker imports at startup |
| `celery_class_tasks` | session | Override to return class-based tasks to register |
| `celery_enable_logging` | session | Override to `True` to let the app configure logging |
| `celery_worker_pool` | session | The pool the test worker runs; `"asyncio"` |
| `depends_on_current_app` | function | Sets the test app as the current app |

Setting the environment variable `NO_WORKER` makes `celery_worker` and
`celery_session_worker` yield nothing instead of starting a worker.

## Without the fixtures

`celery.contrib.testing.worker.start_worker` is the context manager the fixtures
are built on, for tests that need to control when the worker runs:

```python
from celery.contrib.testing.worker import start_worker

with start_worker(app, perform_ping_check=False):
    assert add.delay(2, 3).get(timeout=10) == 5
```
