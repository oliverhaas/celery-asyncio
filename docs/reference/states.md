# Task States

Constants and sets for task state management.

## State constants

| Constant | Value | Description |
|----------|-------|-------------|
| `PENDING` | `"PENDING"` | Task is waiting for execution or unknown |
| `RECEIVED` | `"RECEIVED"` | Task received by a worker |
| `STARTED` | `"STARTED"` | Task has been started |
| `SUCCESS` | `"SUCCESS"` | Task executed successfully |
| `FAILURE` | `"FAILURE"` | Task execution failed |
| `REVOKED` | `"REVOKED"` | Task has been revoked |
| `RETRY` | `"RETRY"` | Task is being retried |
| `REJECTED` | `"REJECTED"` | Task was rejected by a worker |
| `IGNORED` | `"IGNORED"` | Task result was ignored |

## State sets

| Set | States | Description |
|-----|--------|-------------|
| `READY_STATES` | SUCCESS, FAILURE, REVOKED | Task has finished executing |
| `UNREADY_STATES` | PENDING, RECEIVED, STARTED, RETRY, REJECTED | Task is not yet done |
| `EXCEPTION_STATES` | FAILURE, RETRY, REVOKED | Task raised an exception |
| `PROPAGATE_STATES` | FAILURE, REVOKED | Exceptions that should propagate to caller |

## Module reference

::: celery.states
    options:
      members:
        - state
        - precedence
        - PENDING
        - RECEIVED
        - STARTED
        - SUCCESS
        - FAILURE
        - REVOKED
        - RETRY
        - REJECTED
        - IGNORED
        - READY_STATES
        - UNREADY_STATES
        - EXCEPTION_STATES
        - PROPAGATE_STATES
        - ALL_STATES
      show_root_heading: false
