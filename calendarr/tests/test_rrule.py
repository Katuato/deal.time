import pytest

from calendarr.rrule_builder import build_rrule


def test_simple_weekly():
    assert build_rrule("weekly", byday=["we"]) == "RRULE:FREQ=WEEKLY;BYDAY=WE"


def test_interval_and_multiple_days():
    assert build_rrule("WEEKLY", interval=2, byday=["MO", "FR", "mo"]) == (
        "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,FR"
    )


def test_count():
    assert build_rrule("DAILY", count=5) == "RRULE:FREQ=DAILY;COUNT=5"


def test_until_converted_from_user_tz_to_utc():
    # 2026-12-31 23:59:59 МСК -> 20:59:59 UTC
    r = build_rrule("DAILY", until="2026-12-31", timezone="Europe/Moscow")
    assert r == "RRULE:FREQ=DAILY;UNTIL=20261231T205959Z"


def test_until_other_tz_with_time():
    r = build_rrule("DAILY", until="2026-06-01T10:00:00", timezone="Asia/Yekaterinburg")
    assert r.endswith("UNTIL=20260601T050000Z")


def test_until_with_z_kept():
    r = build_rrule("DAILY", until="2026-06-01T10:00:00Z", timezone="Asia/Yekaterinburg")
    assert r.endswith("UNTIL=20260601T100000Z")


def test_until_rfc_format_passthrough():
    assert build_rrule("DAILY", until="20261231").endswith("UNTIL=20261231")


def test_count_and_until_exclusive():
    with pytest.raises(ValueError):
        build_rrule("DAILY", count=3, until="2026-12-31")


@pytest.mark.parametrize("days", [["XX"], ["MO", "FUNDAY"]])
def test_invalid_days(days):
    with pytest.raises(ValueError):
        build_rrule("WEEKLY", byday=days)


@pytest.mark.parametrize("kwargs", [
    {"freq": "HOURLY"}, {"freq": ""}, {"freq": "DAILY", "interval": 0},
    {"freq": "DAILY", "count": 0}, {"freq": "DAILY", "until": "завтра"},
])
def test_invalid_params(kwargs):
    with pytest.raises(ValueError):
        build_rrule(**kwargs)
