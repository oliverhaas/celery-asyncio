"""The two packages ship from one wheel, so their versions cannot drift."""

import tomllib
from pathlib import Path

import celery
import kombu


def test_kombu_version_matches_celery():
    assert kombu.__version__ == celery.__version__


def test_package_versions_match_pyproject():
    pyproject = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    declared = pyproject["project"]["version"]
    assert celery.__version__ == declared
    assert kombu.__version__ == declared
