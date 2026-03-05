import os

import pytest

from celery.utils.sysinfo import load_average


@pytest.mark.skipif(not hasattr(os, "getloadavg"), reason="Function os.getloadavg is not defined")
def test_load_average(patching):
    getloadavg = patching("os.getloadavg")
    getloadavg.return_value = 0.54736328125, 0.6357421875, 0.69921875
    l = load_average()
    assert l
    assert l == (0.55, 0.64, 0.7)
