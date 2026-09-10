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


def test_dish_audit_reports_stale_overrides_and_survives_a_bad_unit(monkeypatch):
    """The audit must finish even when a venue is unreachable, and must flag
    override ids that no longer match any live category."""
    import app.main as m

    monkeypatch.setattr(m, "_fetch_units", lambda: [
        {"id": "1", "name": "Freeman Café"},
        {"id": "2", "name": "Broken Venue"},
    ])

    def fake_unit_menus(unit_id, refresh=False):
        if unit_id == "2":
            raise RuntimeError("CBORD timeout")
        return {"unitId": unit_id, "periods": [], "directItems": {"categories": [
            {"categoryId": "1470", "header": "Freeman Café Soups", "items": []},
            {"categoryId": "1471", "header": "Freeman Café Salads", "items": []},
            {"categoryId": "1475", "header": "Freeman Café Salad Add Ons", "items": []},
            {"categoryId": "1476", "header": "Freeman Café Salad Dressings", "items": []},
            {"categoryId": "2413", "header": "Freeman Café Hot Entreés", "items": []},
            {"categoryId": "1474", "header": "Freeman Café Desserts", "items": []},
            {"categoryId": "1477", "header": "Freeman Café Sandwiches", "items": []},
        ]}}

    monkeypatch.setattr(m, "unit_menus", fake_unit_menus)
    monkeypatch.setattr(m.dishes, "load_overrides",
                        lambda *a, **k: {"1470": None, "9999": "Long Gone Dish"})

    report = m.audit_dish_grouping()

    # Six categories remain free after 1470 is pinned, one over the sweep
    # threshold — so the venue-wide prefix is rejected and reported.
    assert report["venuesAutoCorrected"] == ["Freeman Café"]
    # 9999 matches no live category; 1470 does, so only 9999 is stale.
    assert report["staleOverrideIds"] == ["9999"]
    # The unreachable venue is reported, not raised.
    assert len(report["unreachable"]) == 1
    assert "Broken Venue" in report["unreachable"][0]
    assert report["unitsAudited"] == 1
