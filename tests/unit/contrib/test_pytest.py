import pytest

pytest_plugins = ["pytester"]

try:
    pytest.fail()
except BaseException as e:
    Failed = type(e)


def test_pytest_celery_marker_registration(testdir):
    """Verify that using the 'celery' marker does not result in a warning"""
    testdir.makepyfile(
        """
        import pytest
        @pytest.mark.celery(foo="bar")
        def test_noop():
            pass
        """
    )

    # The plugin is named by module path: this distribution declares no
    # pytest11 entry point, so there is no short name for pytest to resolve.
    result = testdir.runpytest("-q", "-p", "celery.contrib.pytest")
    with pytest.raises((ValueError, Failed)):
        result.stdout.fnmatch_lines_random("*PytestUnknownMarkWarning: Unknown pytest.mark.celery*")
