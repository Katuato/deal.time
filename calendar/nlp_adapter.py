from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Словарь дней недели для RRULE
DAYS_MAP = {
    "пн": "MO", "понедельник": "MO", "вт": "TU", "вторник": "TU",
    "ср": "WE", "среду": "WE", "среда": "WE", "чт": "TH", "четверг": "TH",
    "пт": "FR", "пятницу": "FR", "пятница": "FR", "сб": "SA", "субботу": "SA",
    "суббота": "SA", "вс": "SU", "воскресенье": "SU"
}


def parse_recurrence(rec_text: str) -> dict:
    """Превращает текст Анны ('каждую неделю пн') в словарь для build_rrule."""
    if not rec_text:
        return None

    rec_text = rec_text.lower()
    result = {"freq": "WEEKLY", "interval": 1}

    if "день" in rec_text or "ежедневно" in rec_text:
        result["freq"] = "DAILY"
    elif "месяц" in rec_text:
        result["freq"] = "MONTHLY"
    elif "год" in rec_text:
        result["freq"] = "YEARLY"

    for ru_day, en_day in DAYS_MAP.items():
        if ru_day in rec_text:
            result["byday"] = [en_day]
            break
    return result


def parse_datetime(nlp_date: str, nlp_time: str, tz_name: str) -> str:
    """Превращает 'завтра' и '15:00' в '2026-11-15T15:00:00+03:00'."""
    if not nlp_time:
        return None

    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    target_date = now.date()

    if nlp_date:
        nlp_date = nlp_date.lower()
        if "послезавтра" in nlp_date:
            target_date = now.date() + timedelta(days=2)
        elif "завтра" in nlp_date:
            target_date = now.date() + timedelta(days=1)

    h, m = map(int, nlp_time.split(":"))
    dt = datetime.combine(target_date, datetime.min.time().replace(hour=h, minute=m))
    return dt.replace(tzinfo=tz).isoformat()


async def handle_nlp_result(nlp_result: dict, user_id: int, storage, user_tz: str = "Europe/Moscow"):
    """Берет результат Анны, адаптирует и вызывает твой календарь."""
    if nlp_result.get("status") not in ("ok", "needs_clarification"):
        return None

    ev = nlp_result.get("event", {})
    if ev.get("intent") != "create":
        return None

    # Собираем описание с именами участников (т.к. Google API требует email, а их нет)
    desc = "Создано ботом ДЕЛУ.ВРЕМЯ"
    if ev.get('attendees'):
        desc = f"Участники: {', '.join(ev.get('attendees', []))}\n" + desc

    event_data = {
        "summary": ev.get("summary") or "Встреча",
        "timezone": ev.get("timezone") or user_tz,
        "start_time": parse_datetime(ev.get("date"), ev.get("time"), user_tz),
        "end_time": parse_datetime(ev.get("date"), ev.get("end_time"), user_tz),
        "recurrence": parse_recurrence(ev.get("recurrence")),
        "description": desc,
        "attendees": []
    }

    # Импорт внутри функции, чтобы избежать циклического импорта
    from calendar_connector import create_event
    return await create_event(user_id, storage, event_data)