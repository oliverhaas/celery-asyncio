# Schedules

Schedule classes for periodic tasks (used with celery beat).

## crontab

::: celery.schedules.crontab
    options:
      members:
        - __init__
        - is_due
        - remaining_estimate
        - now

## schedule

::: celery.schedules.schedule
    options:
      members:
        - __init__
        - is_due
        - remaining_estimate

## solar

::: celery.schedules.solar
    options:
      members:
        - __init__
        - is_due
        - remaining_estimate
