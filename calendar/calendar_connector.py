import os.path
import logging
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, stop_after_attempt, wait_exponential

from rrule_builder import build_rrule

# 1. Настраиваем логирование (запись всех шагов с датой и временем по ТЗ)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# 2. Указываем, что боту нужен доступ к событиям календаря
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def get_calendar_service():
    """
    Проверяет наличие пропуска (token.json). Если его нет — открывает браузер
    для входа в Google и возвращает готовое подключение к календарю.
    """
    creds = None

    # Если мы уже входили раньше, берём сохранённый пропуск из файла token.json
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    # Если пропуска нет или он устарел — запускаем вход
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())  # Обновляем устаревший пропуск автоматически
        else:
            # Открываем окно браузера, используя скачанный файл credentials.json
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)

        # Сохраняем полученный пропуск в файл token.json на будущее
        with open("token.json", "w") as token_file:
            token_file.write(creds.to_json())

    # Создаём и возвращаем объект управления Google Calendar API v3
    return build("calendar", "v3", credentials=creds)

#2
def create_simple_event(event_data: dict) -> dict:
    """
    Создаёт разовое событие в Google Календаре.
    """
    service = get_calendar_service()

    # Берём часовой пояс из данных (если не указан — ставим Москву по умолчанию)
    timezone = event_data.get("timezone", "Europe/Moscow")

    # Собираем карточку события для отправки в Google
    event_body = {
        "summary": event_data["summary"],  # Название встречи
        "description": event_data.get("description", "Создано ботом ДЕЛУ.ВРЕМЯ"),
        "start": {
            "dateTime": event_data["start_time"],
            "timeZone": timezone,
        },
        "end": {
            "dateTime": event_data["end_time"],
            "timeZone": timezone,
        },
    }

    logging.info(f"Создаём разовую встречу: {event_data['summary']}")

    # Отправляем команду insert в основной календарь ('primary')
    created_event = service.events().insert(calendarId="primary", body=event_body).execute()

    return {
        "status": "success",
        "event_id": created_event.get("id"),  # Уникальный номер встречи
        "html_link": created_event.get("htmlLink")  # Прямая ссылка на встречу
    }



#3
def create_event(event_data: dict) -> dict:
    """
    Универсальная функция: создаёт как разовые, так и повторяющиеся события (через RRULE).
    """
    service = get_calendar_service()
    timezone = event_data.get("timezone", "Europe/Moscow")

    event_body = {
        "summary": event_data["summary"],
        "description": event_data.get("description", "Создано ботом ДЕЛУ.ВРЕМЯ"),
        "start": {"dateTime": event_data["start_time"], "timeZone": timezone},
        "end": {"dateTime": event_data["end_time"], "timeZone": timezone},
    }

    # 1. Если переданы email участников — добавляем их во встречу
    if event_data.get("attendees"):
        event_body["attendees"] = [{"email": mail} for mail in event_data["attendees"]]

    # 2. Если в данных есть блок 'recurrence' — строим правило RRULE
    if event_data.get("recurrence"):
        rec = event_data["recurrence"]
        rrule_string = build_rrule(
            freq=rec["freq"],
            interval=rec.get("interval", 1),
            byday=rec.get("byday"),
            count=rec.get("count"),
            until=rec.get("until")
        )
        # Прикрепляем строчку RRULE к событию
        event_body["recurrence"] = [rrule_string]
        logging.info(f"Сформировано правило повторения: {rrule_string}")

    created_event = service.events().insert(calendarId="primary", body=event_body).execute()

    return {
        "status": "success",
        "event_id": created_event.get("id"),
        "html_link": created_event.get("htmlLink")
    }

#4
# Эта обёртка автоматически повторит отправку до 3 раз (через 1, 2 и 4 секунды), если случится сбой
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5), reraise=True)
def _send_to_google(service, event_body: dict) -> dict:
    """Отправляет событие в Google с защитой от кратковременных сбоев сети (NF-07)."""
    return service.events().insert(calendarId="primary", body=event_body).execute()

#5
def update_event_time(event_id: str, new_start_time: str, new_end_time: str, timezone: str = "Europe/Moscow") -> dict:
    """Переносит существующую встречу на новое время (F-B-17, F-M-11)."""
    try:
        service = get_calendar_service()
        patch_body = {
            "start": {"dateTime": new_start_time, "timeZone": timezone},
            "end": {"dateTime": new_end_time, "timeZone": timezone}
        }
        updated = service.events().patch(calendarId="primary", eventId=event_id, body=patch_body).execute()
        logging.info(f"Время события {event_id} успешно изменено.")
        return {"status": "success", "event_id": updated.get("id")}
    except Exception as error:
        logging.error(f"Ошибка при переносе события {event_id}: {error}")
        return {"status": "error", "message": str(error)}


def delete_event(event_id: str) -> dict:
    """Удаляет разовое событие или всю серию повторений по его ID (F-B-16, F-B-18, F-M-10, F-M-12)."""
    try:
        service = get_calendar_service()
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        logging.info(f"Событие {event_id} успешно удалено.")
        return {"status": "success"}
    except Exception as error:
        logging.error(f"Ошибка при удалении события {event_id}: {error}")
        return {"status": "error", "message": str(error)}


# --- ПОЛНАЯ ПРОВЕРКА ВСЕХ ФУНКЦИЙ ---
if __name__ == "__main__":
    # 1. Тестируем создание повторяющейся встречи (каждый понедельник с 16 до 17, 4 раза)
    mock_nlp_input = {
        "summary": "Созвон по проекту ДЕЛУ.ВРЕМЯ",
        "start_time": "2026-10-26T16:00:00",
        "end_time": "2026-10-26T17:00:00",
        "timezone": "Europe/Moscow",
        "recurrence": {
            "freq": "WEEKLY",
            "byday": ["MO"],
            "count": 4
        }
    }

    create_res = create_event(mock_nlp_input)
    print("1. Результат создания:", create_res)

    # 2. Если встреча создалась успешно, попробуем перенести первую дату на час позже (с 17 до 18)
    if create_res["status"] == "success":
        event_id = create_res["event_id"]
        update_res = update_event_time(
            event_id=event_id,
            new_start_time="2026-10-26T17:00:00",
            new_end_time="2026-10-26T18:00:00"
        )
        print("2. Результат переноса на час позже:", update_res)