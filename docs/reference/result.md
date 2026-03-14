# Results

Classes for querying task results.

## AsyncResult

::: celery.result.AsyncResult
    options:
      members:
        - __init__
        - id
        - get
        - ready
        - successful
        - failed
        - forget
        - revoke
        - state
        - status
        - result
        - traceback
        - info

## GroupResult

::: celery.result.GroupResult
    options:
      members:
        - __init__
        - results
        - get
        - ready
        - successful
        - failed
        - join
        - join_native

## ResultSet

::: celery.result.ResultSet
    options:
      members:
        - results
        - get
        - ready
        - successful
        - failed
        - join

## EagerResult

::: celery.result.EagerResult
