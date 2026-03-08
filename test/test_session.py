# Tests for session window and refresh timing

from datetime import datetime

from pimm.utils.time import HKT, is_in_session, needs_refresh, seconds_until_session_end
from test.conftest import make_config


class TestSessionWindows:
    def test_in_session(self):
        config = make_config(["09:30-12:00", "13:00-16:00"])
        dt = datetime(2024, 1, 15, 10, 0, 0, tzinfo=HKT)
        assert is_in_session(config, dt) is True

    def test_between_sessions(self):
        config = make_config(["09:30-12:00", "13:00-16:00"])
        dt = datetime(2024, 1, 15, 12, 30, 0, tzinfo=HKT)
        assert is_in_session(config, dt) is False

    def test_before_session(self):
        config = make_config(["09:30-12:00"])
        dt = datetime(2024, 1, 15, 9, 0, 0, tzinfo=HKT)
        assert is_in_session(config, dt) is False

    def test_after_session(self):
        config = make_config(["09:30-12:00"])
        dt = datetime(2024, 1, 15, 16, 0, 0, tzinfo=HKT)
        assert is_in_session(config, dt) is False

    def test_session_boundary_start_inclusive(self):
        config = make_config(["09:30-12:00"])
        dt = datetime(2024, 1, 15, 9, 30, 0, tzinfo=HKT)
        assert is_in_session(config, dt) is True

    def test_session_boundary_end_exclusive(self):
        config = make_config(["09:30-12:00"])
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=HKT)
        assert is_in_session(config, dt) is False


class TestCountdown:
    def test_countdown_in_session(self):
        config = make_config(["09:30-12:00"])
        dt = datetime(2024, 1, 15, 11, 50, 0, tzinfo=HKT)
        remaining = seconds_until_session_end(config, dt)
        assert remaining is not None
        assert remaining == 600.0

    def test_countdown_not_in_session(self):
        config = make_config(["09:30-12:00"])
        dt = datetime(2024, 1, 15, 13, 0, 0, tzinfo=HKT)
        assert seconds_until_session_end(config, dt) is None


class TestRefreshTiming:
    def test_needs_refresh_no_prior_send(self):
        assert needs_refresh(None, 5, 15) is True

    def test_no_refresh_needed_recently_sent(self):
        now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=HKT)
        last = datetime(2024, 1, 15, 9, 58, 0, tzinfo=HKT)
        assert needs_refresh(last, 5, 15, now) is False

    def test_refresh_needed_approaching_expiry(self):
        now = datetime(2024, 1, 15, 10, 4, 50, tzinfo=HKT)
        last = datetime(2024, 1, 15, 10, 0, 0, tzinfo=HKT)
        assert needs_refresh(last, 5, 15, now) is True
