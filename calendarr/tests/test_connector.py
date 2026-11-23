"""Тесты calendar_connector с замоканным get_calendar_service."""
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from calendarr import calendar_connector as cc


def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "err"
    return HttpError(resp, b"{}")


def _service(insert=None, get=None, update=None, delete=None):
    """Фейковый service: каждый events().X() возвращает объект с execute()."""
    svc = MagicMock()
    ev = svc.events.return_value
    for name, val in (("insert", insert), ("get", get), ("update", update), ("delete", delete)):
        req = MagicMock()
        if isinstance(val, BaseException):
            req.execute.side_effect = val
        else:
            req.execute.return_value = val if val is not None else {}
        getattr(ev, name).return_value = req
    return svc


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("tenacity.nap.time.sleep"):
        yield


def _data(**kw):
    d = {"summary": "Встреча", "start_time": "2026-11-15T15:00:00+03:00",
         "end_time": "2026-11-15T16:00:00+03:00"}
    d.update(kw)
    return d


async def _create(svc, data):
    with patch("calendarr.calendar_connector.get_calendar_service", AsyncMock(return_value=svc)):
        return await cc.create_event(1, MagicMock(), data)


def test_event_id_is_base32hex():
    eid = cc._build_idempotent_event_id(1, _data())
    assert re.fullmatch(r"[a-v0-9]{5,1024}", eid)


async def test_event_data_not_mutated():
    svc = _service(insert={"id": "x"})
    data = {"summary": "A", "start_time": "2026-11-15T15:00:00+03:00"}
    res = await _create(svc, data)
    assert res["status"] == "success"
    assert "end_time" not in data
    body = svc.events().insert.call_args.kwargs["body"]
    assert body["end"]["dateTime"].startswith("2026-11-15T16:00:00")


async def test_duration_minutes_fallback():
    svc = _service(insert={})
    await _create(svc, {"summary": "A", "start_time": "2026-11-15T15:00:00+03:00",
                        "duration_minutes": 30})
    body = svc.events().insert.call_args.kwargs["body"]
    assert body["end"]["dateTime"].startswith("2026-11-15T15:30:00")


@pytest.mark.parametrize("end", ["2026-11-15T15:00:00+03:00", "2026-11-15T14:00:00+03:00"])
async def test_end_not_after_start(end):
    svc = _service()
    res = await _create(svc, _data(end_time=end))
    assert res["error_type"] == "validation_error" and res["need_queue"] is False
    svc.events().insert.assert_not_called()


async def test_409_cancelled_restored():
    svc = _service(insert=_http_error(409), get={"status": "cancelled"},
                   update={"id": "abc", "htmlLink": "http://l"})
    res = await _create(svc, _data())
    assert res["status"] == "success" and res["duplicate"] is False
    assert res["html_link"] == "http://l"
    body = svc.events().update.call_args.kwargs["body"]
    assert body["status"] == "confirmed" and body["summary"] == "Встреча"


async def test_409_active_is_duplicate():
    svc = _service(insert=_http_error(409), get={"status": "confirmed", "htmlLink": "http://d"})
    res = await _create(svc, _data())
    assert res["status"] == "success" and res["duplicate"] is True
    assert res["html_link"] == "http://d"
    svc.events().update.assert_not_called()


async def test_conflict_reuses_persisted_event_id():
    saved_id = "b" * 32
    svc = _service(insert=_http_error(409), get={"status": "confirmed"})
    data = _data(google_event_id=saved_id)
    res = await _create(svc, data)
    assert res["duplicate"] is True and res["event_id"] == saved_id
    assert svc.events().insert.call_args.kwargs["body"]["id"] == saved_id
    assert svc.events().get.call_args.kwargs["eventId"] == saved_id


async def test_provider_error_body_not_logged_or_returned(caplog):
    error = _http_error(503)
    error.uri = "https://example.org/?token=private-credential"
    svc = _service(insert=error)
    result = await _create(svc, _data(summary="private-title"))
    assert result["need_queue"] is True
    assert "private-credential" not in result["message"]
    assert "private-credential" not in caplog.text
    assert "private-title" not in caplog.text


async def test_delete_404_is_success():
    svc = _service(delete=_http_error(404))
    with patch("calendarr.calendar_connector.get_calendar_service", AsyncMock(return_value=svc)):
        res = await cc.delete_event(1, MagicMock(), "evt")
    assert res == {"status": "success", "already_deleted": True}


async def test_unexpected_error_goes_to_queue():
    with patch("calendarr.calendar_connector.get_calendar_service",
               AsyncMock(side_effect=RuntimeError("db down"))):
        res = await cc.create_event(1, MagicMock(), _data())
    assert res["error_type"] == "internal_error" and res["need_queue"] is True


async def test_rrule_string_passthrough():
    svc = _service(insert={})
    await _create(svc, _data(rrule="FREQ=WEEKLY;BYDAY=WE",
                             recurrence={"freq": "DAILY"}))
    body = svc.events().insert.call_args.kwargs["body"]
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=WE"]


async def test_rrule_without_freq_is_validation_error():
    svc = _service()
    res = await _create(svc, _data(rrule="BYDAY=WE"))
    assert res["error_type"] == "validation_error"


async def test_location_passed_to_google():
    svc = _service(insert={"id": "x"})
    res = await _create(svc, _data(location=" Офис "))
    assert res["status"] == "success"
    assert svc.events.return_value.insert.call_args.kwargs["body"]["location"] == "Офис"
