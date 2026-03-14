# Signals

Celery uses signals to allow decoupled applications to receive
notifications when actions occur elsewhere.

## Task signals

::: celery.signals
    options:
      members:
        - before_task_publish
        - after_task_publish
        - task_received
        - task_prerun
        - task_postrun
        - task_success
        - task_failure
        - task_retry
        - task_revoked
        - task_rejected
        - task_unknown
        - task_internal_error
      show_root_heading: false

## Worker signals

::: celery.signals
    options:
      members:
        - worker_init
        - worker_ready
        - worker_shutdown
        - worker_shutting_down
        - celeryd_init
        - celeryd_after_setup
      show_root_heading: false

## Beat signals

::: celery.signals
    options:
      members:
        - beat_init
        - beat_embedded_init
        - heartbeat_sent
      show_root_heading: false

## Logging signals

::: celery.signals
    options:
      members:
        - setup_logging
        - after_setup_logger
        - after_setup_task_logger
      show_root_heading: false
