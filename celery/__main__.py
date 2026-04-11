# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""Entry-point for the :program:`celery` umbrella command."""

import sys

__all__ = ("main",)


def main() -> None:
    """Entrypoint to the ``celery`` umbrella command."""
    from celery.bin.celery import main as _main

    sys.exit(_main())


if __name__ == "__main__":  # pragma: no cover
    main()
