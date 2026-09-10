"""Weekly refresh scheduling and NetID identity parsing."""
import datetime as dt

import pytest

from app.main import (
    REFRESH_HOUR,
    _CAMPUS_TZ,
    _seconds_until_next_refresh,
    caller_id,
)
from fastapi import HTTPException


def _at(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=_CAMPUS_TZ)


def test_refresh_targets_the_next_monday_at_4am():
    # Wednesday afternoon -> the following Monday.
    delay = _seconds_until_next_refresh(_at(2026, 9, 9, 14))
    fired = _at(2026, 9, 9, 14) + dt.timedelta(seconds=delay)
    assert fired.weekday() == 0 and fired.hour == REFRESH_HOUR
    assert fired.date() == dt.date(2026, 9, 14)


def test_monday_before_and_after_the_hour():
    # Monday 03:00 -> later the same morning, not a week out.
    fired = _at(2026, 9, 14, 3) + dt.timedelta(
        seconds=_seconds_until_next_refresh(_at(2026, 9, 14, 3)))
    assert fired == _at(2026, 9, 14, REFRESH_HOUR)

    # Monday 05:00 -> the job already ran; wait a full week rather than
    # firing immediately at startup.
    fired = _at(2026, 9, 14, 5) + dt.timedelta(
        seconds=_seconds_until_next_refresh(_at(2026, 9, 14, 5)))
    assert fired == _at(2026, 9, 21, REFRESH_HOUR)


def test_refresh_time_survives_the_daylight_saving_change():
    """Spanning the DST boundary must still land at 4am local, not 3am or 5am."""
    start = _at(2026, 10, 28, 12)          # Wednesday before the US change
    fired = start + dt.timedelta(seconds=_seconds_until_next_refresh(start))
    # Compare in campus time: the wall clock is what matters, not elapsed UTC.
    assert fired.astimezone(_CAMPUS_TZ).hour == REFRESH_HOUR


def test_netid_is_normalized_and_namespaced():
    assert caller_id("rw290") == "netid:rw290"
    assert caller_id("  RW290  ") == "netid:rw290"      # case/whitespace tolerant


def test_missing_netid_falls_back_to_the_shared_anonymous_log():
    assert caller_id(None) == "anonymous"
    assert caller_id("   ") == "anonymous"


@pytest.mark.parametrize("bad", ["9abc", "has space", "a", "x" * 21, "rw-290", "../etc"])
def test_malformed_netids_are_rejected(bad):
    with pytest.raises(HTTPException) as exc:
        caller_id(bad)
    assert exc.value.status_code == 422
