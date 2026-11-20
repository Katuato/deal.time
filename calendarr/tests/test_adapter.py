from unittest.mock import AsyncMock

import pytest

from calendarr.tests import fake_db

fake_db.install()

from calendarr import nlp_adapter  # noqa: E402


def make_result(**ev_over):
    ev = {
        "intent": "create", "summary": "Созвон", "date": "в пятницу", "time": "15:00",
        "end_time": None, "duration_minutes": 45, "location": None,
        "recurrence": "раз в две недели по пятницам", "rrule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR",
        "timezone": None, "attendees": ["Иван", "Анна"],
        "starts_at": "2026-11-20T15:00:00+03:00", "ends_at": "2026-11-20T15:45:00+03:00",
    }
    ev.update(ev_over)
    return {"status": "ok", "event": ev, "missing": [], "clarify": None}


@pytest.fixture
def create_mock(monkeypatch):
    m = AsyncMock(return_value={"status": "success", "event_id": "g1"})
    monkeypatch.setattr(nlp_adapter, "create_event_or_enqueue", m)
    return m


async def test_mapping(create_mock):
    res = await nlp_adapter.handle_nlp_result(make_result(), 7, "storage", "Asia/Yekaterinburg",
                                              local_event_id=5)
    assert res["status"] == "success"
    args, kwargs = create_mock.call_args
    assert args[0] == 7 and args[1] == "storage"
    data = args[2]
    assert data["start_time"] == "2026-11-20T15:00:00+03:00"
    assert data["end_time"] == "2026-11-20T15:45:00+03:00"
    assert data["duration_minutes"] == 45
    assert data["rrule"] == "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR"
    assert data["timezone"] == "Asia/Yekaterinburg"  # ev.timezone пуст -> user_tz
    assert data["attendees"] == []
    assert "Иван, Анна" in data["description"]
    assert kwargs["local_event_id"] == 5


async def test_event_timezone_wins(create_mock):
    await nlp_adapter.handle_nlp_result(make_result(timezone="Asia/Yekaterinburg"), 1, None)
    assert create_mock.call_args.args[2]["timezone"] == "Asia/Yekaterinburg"


async def test_needs_clarification_no_call(create_mock):
    res = await nlp_adapter.handle_nlp_result(
        {"status": "needs_clarification", "event": {"intent": "create"},
         "missing": ["time"], "clarify": "Во сколько?"}, 1, None)
    assert res == {"status": "needs_clarification", "missing": ["time"], "question": "Во сколько?"}
    create_mock.assert_not_called()


async def test_missing_starts_at(create_mock):
    res = await nlp_adapter.handle_nlp_result(make_result(starts_at=None), 1, None)
    assert res["status"] == "needs_clarification" and res["missing"] == ["starts_at"]
    create_mock.assert_not_called()


@pytest.mark.parametrize("intent", ["move", "cancel"])
async def test_unsupported(create_mock, intent):
    res = await nlp_adapter.handle_nlp_result(make_result(intent=intent), 1, None)
    assert res == {"status": "unsupported", "intent": intent}
    create_mock.assert_not_called()


@pytest.mark.parametrize("status", ["no_event", "error"])
async def test_ignored(create_mock, status):
    res = await nlp_adapter.handle_nlp_result({"status": status, "event": None}, 1, None)
    assert res == {"status": "ignored"}
    create_mock.assert_not_called()
