"""Distributed Task Queue."""
# :copyright: (c) 2017-2026 Asif Saif Uddin, celery core and individual
#                 contributors, All rights reserved.
# :copyright: (c) 2015-2016 Ask Solem.  All rights reserved.
# :copyright: (c) 2012-2014 GoPivotal, Inc., All rights reserved.
# :copyright: (c) 2009 - 2012 Ask Solem and individual contributors,
#                 All rights reserved.
# :license:   BSD (3 Clause), see LICENSE for more details.

import re
from collections import namedtuple

# Lazy loading
from . import local

SERIES = "asyncio"

__version__ = "6.0.0a2"
__author__ = "Oliver Haas"
__contact__ = "ohaas@e1plus.de"
__homepage__ = "https://oliverhaas.github.io/celery-asyncio/"
__docformat__ = "restructuredtext"
__keywords__ = "task job queue distributed messaging actor"

# -eof meta-

__all__ = (
    "Celery",
    "Signature",
    "bugreport",
    "shared_task",
    "Task",
    "current_app",
    "current_task",
    "maybe_signature",
    "chain",
    "chord",
    "chunks",
    "group",
    "signature",
    "xmap",
    "xstarmap",
    "uuid",
)

VERSION_BANNER = f"{__version__} ({SERIES})"

version_info_t = namedtuple(
    "version_info_t",
    (
        "major",
        "minor",
        "micro",
        "releaselevel",
        "serial",
    ),
)

# bumpversion can only search for {current_version}
# so we have to parse the version here.
_temp = re.match(r"(\d+)\.(\d+)\.(\d+)(.+)?", __version__).groups()
VERSION = version_info = version_info_t(int(_temp[0]), int(_temp[1]), int(_temp[2]), _temp[3] or "", "")
del _temp
del re

# This is never executed, but tricks static analyzers (PyDev, PyCharm,
# pylint, etc.) into knowing the types of these symbols, and what
# they contain.
STATICA_HACK = True
globals()["kcah_acitats"[::-1].upper()] = False
if STATICA_HACK:  # pragma: no cover
    from celery._state import current_app, current_task
    from celery.app import shared_task
    from celery.app.base import Celery
    from celery.app.task import Task
    from celery.app.utils import bugreport
    from celery.canvas import Signature, chain, chord, chunks, group, maybe_signature, signature, xmap, xstarmap
    from celery.utils import uuid


# this just creates a new module, that imports stuff on first attribute
# access.  This makes the library faster to use.
old_module, new_module = local.recreate_module(  # pragma: no cover
    __name__,
    by_module={
        "celery.app": ["Celery", "bugreport", "shared_task"],
        "celery.app.task": ["Task"],
        "celery._state": ["current_app", "current_task"],
        "celery.canvas": [
            "Signature",
            "chain",
            "chord",
            "chunks",
            "group",
            "signature",
            "maybe_signature",
            "xmap",
            "xstarmap",
        ],
        "celery.utils": ["uuid"],
    },
    __package__="celery",
    __file__=__file__,
    __path__=__path__,
    __doc__=__doc__,
    __version__=__version__,
    __author__=__author__,
    __contact__=__contact__,
    __homepage__=__homepage__,
    __docformat__=__docformat__,
    local=local,
    VERSION=VERSION,
    SERIES=SERIES,
    VERSION_BANNER=VERSION_BANNER,
    version_info_t=version_info_t,
    version_info=version_info,
)
