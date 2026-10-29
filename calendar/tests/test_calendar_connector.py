import sys
from pathlib import Path

# Гарантируем, что pytest видит модули в корневой папке проекта
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest.mock import MagicMock, patch
import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from calendar_connector import (
    create_event,
    delete_event,
    get_calendar_service,
    update_event_time,
)
from rrule_builder import build_rrule


class FakeDBTokenStorage:
    """Мок-хранилище БД для изолированного тестирования без реальной PostgreSQL."""

    def __init__(self, initial_tokens: dict[int, dict] | None = None):
        self.db = initial_tokens or {}
        self.saved_calls = 0

    def get_user_token(self, user_id: int) -> dict | None:
        return self.db.get(user_id)

    def save_user_token(self, user_id: int, token_data: dict) -> None:
        self.db[user_id] = token_data
        self.saved_calls += 1


def make_http_error(status: int, reason: str = "Error") -> HttpError:
    """Вспомогательная фабрика объектов HttpError библиотеки googleapiclient."""
    resp = Response({"status": status, "reason": reason})
    return HttpError(resp=resp, content=b'{"error": {"message": "Mocked error"}}')


def test_missing_token_in_db_returns_auth_error():
    """Если токена пользователя нет в БД, возвращается статус ошибки auth_error."""
    storage = FakeDBTokenStorage()
    event_data = {
        "summary": "Тестовая встреча",
        "start_time": "2026-11-01T10:00:00",
        "end_time": "2026-11-01T11:00:00",
    }

    res = create_event(user_id=999, token_storage=storage, event_data=event_data)

    assert res["status"] == "error"
    assert res["error_type"] == "auth_error"
    assert res["need_queue"] is False


@patch("calendar_connector.build")
@patch("calendar_connector.Credentials.from_authorized_user_info")
def test_expired_token_refreshes_and_saves_to_db(mock_from_info, mock_build):
    """Если токен истёк, он должен обновиться через Request и сохраниться обратно в БД."""
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "valid_refresh_token"
    mock_creds.to_json.return_value = '{"token": "new_access_token"}'
    mock_from_info.return_value = mock_creds

    storage = FakeDBTokenStorage({101: {"token": "old_token"}})
    get_calendar_service(user_id=101, token_storage=storage)

    mock_creds.refresh.assert_called_once()
    assert storage.saved_calls == 1
    assert storage.db[101] == {"token": "new_access_token"}


@patch("calendar_connector.get_calendar_service")
def test_retry_on_503_and_custom_calendar_id_success(mock_get_service):
    """При двух сбоях 503 подряд и успехе на 3-й попытке событие создаётся в кастомном calendar_id."""
    mock_service = MagicMock()
    mock_insert_req = MagicMock()
    mock_insert_req.execute.side_effect = [
        make_http_error(503, "Service Unavailable"),
        make_http_error(503, "Service Unavailable"),
        {"id": "evt_777", "htmlLink": "https://calendar.google.com/event?eid=777"},
    ]
    mock_service.events.return_value.insert.return_value = mock_insert_req
    mock_get_service.return_value = mock_service

    storage = FakeDBTokenStorage()
    event_data = {
        "summary": "Код-ревью",
        "start_time": "2026-11-02T15:00:00",
        "end_time": "2026-11-02T16:00:00",
    }

    res = create_event(
        user_id=1,
        token_storage=storage,
        event_data=event_data,
        calendar_id="work_team_calendar@group.calendar.google.com",
    )

    assert res["status"] == "success"
    assert res["event_id"] == "evt_777"
    assert res["calendar_id"] == "work_team_calendar@group.calendar.google.com"
    assert mock_insert_req.execute.call_count == 3


@patch("calendar_connector.get_calendar_service")
def test_total_api_outage_flags_event_for_queue(mock_get_service):
    """Если все 3 попытки завершились ошибкой 500, возвращается need_queue=True."""
    mock_service = MagicMock()
    mock_insert_req = MagicMock()
    mock_insert_req.execute.side_effect = make_http_error(500, "Internal Server Error")
    mock_service.events.return_value.insert.return_value = mock_insert_req
    mock_get_service.return_value = mock_service

    storage = FakeDBTokenStorage()
    event_data = {
        "summary": "Стендап",
        "start_time": "2026-11-03T12:00:00",
        "end_time": "2026-11-03T12:30:00",
    }

    res = create_event(user_id=1, token_storage=storage, event_data=event_data)

    assert res["status"] == "error"
    assert res["error_type"] == "api_unavailable"
    assert res["need_queue"] is True
    assert mock_insert_req.execute.call_count == 3


@patch("calendar_connector.get_calendar_service")
def test_404_error_does_not_retry(mock_get_service):
    """Клиентская ошибка 404 не должна вызывать повторные попытки (call_count == 1)."""
    mock_service = MagicMock()
    mock_patch_req = MagicMock()
    mock_patch_req.execute.side_effect = make_http_error(404, "Not Found")
    mock_service.events.return_value.patch.return_value = mock_patch_req
    mock_get_service.return_value = mock_service

    res = update_event_time(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_id="non_existent_id",
        new_start_time="2026-11-03T14:00:00",
        new_end_time="2026-11-03T15:00:00",
        calendar_id="custom_cal_id",
    )

    assert res["status"] == "error"
    assert res["error_type"] == "api_error"
    assert res["need_queue"] is False
    assert mock_patch_req.execute.call_count == 1


@patch("calendar_connector.get_calendar_service")
def test_delete_event_403_forbidden(mock_get_service):
    """При попытке удалить событие без прав доступа (HttpError 403) ошибка перехватывается сразу."""
    mock_service = MagicMock()
    mock_delete_req = MagicMock()
    mock_delete_req.execute.side_effect = make_http_error(403, "Forbidden")
    mock_service.events.return_value.delete.return_value = mock_delete_req
    mock_get_service.return_value = mock_service

    res = delete_event(
        user_id=1,
        token_storage=FakeDBTokenStorage(),
        event_id="locked_event_id",
        calendar_id="shared_cal_id",
    )

    assert res["status"] == "error"
    assert res["error_type"] == "api_error"
    assert res["need_queue"] is False
    assert mock_delete_req.execute.call_count == 1


def test_rrule_validation_errors():
    """Проверка защиты генератора RRULE от некорректных параметров."""
    with pytest.raises(ValueError, match="Неподдерживаемая частота"):
        build_rrule(freq="HOURLY")

    with pytest.raises(ValueError, match="взаимоисключающие"):
        build_rrule(freq="WEEKLY", count=5, until="20261231")

    with pytest.raises(ValueError, match="Некорректные дни недели"):
        build_rrule(freq="WEEKLY", byday=["MO", "MONDAY"])
