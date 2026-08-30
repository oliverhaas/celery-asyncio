import time
from unittest.mock import patch

from celery.utils.scheduling import Entry, Timer, to_timestamp


class test_to_timestamp:
    def test_passes_through_numbers(self):
        assert to_timestamp(1234.5) == 1234.5

    def test_none_is_now(self):
        assert abs(to_timestamp(None) - time.time()) < 5


class test_Timer:
    def test_call_after_fires_once_due(self):
        timer = Timer()
        fired = []
        timer.call_after(0, fired.append, (1,))
        _, entry = next(timer)
        assert entry is not None
        timer.apply_entry(entry)
        assert fired == [1]

    def test_pending_entry_reports_its_delay(self):
        timer = Timer()
        timer.call_after(30, lambda: None)
        delay, entry = next(timer)
        assert entry is None
        assert 0 < delay <= timer.max_interval

    def test_empty_queue_reports_max_interval(self):
        timer = Timer(max_interval=7.0)
        assert next(timer) == (7.0, None)

    def test_cancelled_entry_is_dropped(self):
        timer = Timer()
        entry = timer.call_after(0, lambda: None)
        timer.cancel(entry)
        assert next(timer) == (0, None)
        assert timer.empty()

    def test_apply_entry_skips_a_cancelled_entry(self):
        fired = []
        timer = Timer()
        entry = Entry(eta=0, priority=0, fun=fired.append, args=(1,))
        entry.cancel()
        timer.apply_entry(entry)
        assert fired == []

    def test_apply_entry_routes_errors_to_on_error(self):
        seen = []
        timer = Timer(on_error=seen.append)

        def boom():
            raise ValueError("boom")

        timer.apply_entry(Entry(eta=0, priority=0, fun=boom))
        assert isinstance(seen[0], ValueError)

    def test_entries_come_back_in_eta_order(self):
        timer = Timer()
        timer.call_after(-1, lambda: None)
        timer.call_after(-3, lambda: None)
        timer.call_after(-2, lambda: None)
        etas = [next(timer)[1].eta for _ in range(3)]
        assert etas == sorted(etas)

    def test_call_repeatedly_reschedules_itself(self):
        timer = Timer()
        fired = []
        timer.call_repeatedly(-1, fired.append, (1,))
        for _ in range(3):
            _, entry = next(timer)
            timer.apply_entry(entry)
        assert fired == [1, 1, 1]
        assert len(timer) == 1

    def test_clear_and_stop_empty_the_queue(self):
        timer = Timer()
        timer.call_after(30, lambda: None)
        timer.stop()
        assert timer.empty()


class test_Timer_clock:
    """Relative delays live on the monotonic clock, so a wall-clock step
    cannot delay a pending entry or fire the whole queue at once."""

    def test_call_after_ignores_a_wall_clock_jump_forward(self):
        timer = Timer()
        timer.call_after(30, lambda: None)
        with patch("time.time", return_value=time.time() + 3600):
            _, entry = next(timer)
        assert entry is None, "a forward clock step fired a pending entry early"

    def test_call_after_ignores_a_wall_clock_jump_backward(self):
        timer = Timer()
        timer.call_after(-1, lambda: None)
        with patch("time.time", return_value=time.time() - 3600):
            _, entry = next(timer)
        assert entry is not None, "a backward clock step delayed a due entry"

    def test_call_at_takes_a_wall_clock_eta(self):
        timer = Timer()
        timer.call_at(time.time() - 1, lambda: None)
        _, entry = next(timer)
        assert entry is not None

    def test_call_at_eta_is_pinned_when_scheduled(self):
        timer = Timer()
        timer.call_at(time.time() + 30, lambda: None)
        with patch("time.time", return_value=time.time() + 3600):
            _, entry = next(timer)
        assert entry is None, "a clock step brought a scheduled ETA forward"

    def test_enter_after_ignores_a_wall_clock_jump(self):
        timer = Timer()
        timer.enter_after(30, Entry(eta=0, priority=0, fun=lambda: None))
        with patch("time.time", return_value=time.time() + 3600):
            _, entry = next(timer)
        assert entry is None
