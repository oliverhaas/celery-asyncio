import io
import logging
import pickle

from kombu.utils.div import emergency_dump_state


def test_emergency_dump_state_writes_a_pickle_and_reports_the_path():
    stderr = io.StringIO()

    persist = emergency_dump_state({"foo": "bar"}, stderr=stderr)

    assert persist in stderr.getvalue()
    with open(persist, "rb") as fh:
        assert pickle.load(fh) == {"foo": "bar"}


def test_emergency_dump_state_logs_the_path_when_no_stream_is_given(caplog):
    # Without an explicit stream the path used to go to sys.stderr, which in a
    # daemonised worker is nowhere.
    with caplog.at_level(logging.ERROR, logger="kombu.utils.div"):
        persist = emergency_dump_state({"foo": "bar"})

    assert persist in caplog.text


def test_emergency_dump_state_falls_back_to_pformat_for_unpicklable_state():
    stderr = io.StringIO()

    persist = emergency_dump_state({"fun": lambda: None}, stderr=stderr)

    assert "Cannot pickle state" in stderr.getvalue()
    with open(persist, "rb") as fh:
        assert b"fun" in fh.read()


def test_emergency_dump_state_logs_the_pickle_failure_when_no_stream_is_given(caplog):
    with caplog.at_level(logging.ERROR, logger="kombu.utils.div"):
        emergency_dump_state({"fun": lambda: None})

    assert "Cannot pickle state" in caplog.text
