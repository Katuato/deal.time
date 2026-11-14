import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import dateparser

from calendar_connector import update_event_time, delete_event

logger = logging.getLogger(__name__)


def _get_duration_minutes(event_row) -> int:
    """Пункт: Длительность сохраняется. Вычисляем её из БД или ставим 60 минут."""
    if hasattr(event_row, 'ends_at') and event_row.ends_at:
        return int((event_row.ends_at - event_row.starts_at).total_seconds() / 60)
    elif hasattr(event_row, 'duration') and event_row.duration:
        if isinstance(event_row.duration, timedelta):
            return int(event_row.duration.total_seconds() / 60)
        return int(event_row.duration)
    return 60


def _resolve_new_time(changes: dict, current_start: datetime, user_tz: str) -> datetime:
    """Пункты 1, 2, 3: Вычисляет новое время начала на основе правил NLP."""
    tz = ZoneInfo(user_tz)
    now = datetime.now(tz)

    # 1) shift_minutes - сдвиг от текущего начала
    if "shift_minutes" in changes and changes["shift_minutes"] is not None:
        return current_start + timedelta(minutes=changes["shift_minutes"])

    # 3) in_minutes - через N минут от текущего момента
    elif "in_minutes" in changes and changes["in_minutes"] is not None:
        return now + timedelta(minutes=changes["in_minutes"])

    # 2) date + time - новое начало
    elif "date" in changes and "time" in changes:
        date_str = changes["date"]
        time_str = changes["time"]

        # Используем dateparser, чтобы превратить "завтра" или "в пятницу" в реальную дату
        parsed = dateparser.parse(
            f"{date_str} {time_str}",
            languages=['ru'],
            settings={'RELATIVE_BASE': now, 'PREFER_DATES_FROM': 'future'}
        )
        if not parsed:
            raise ValueError(f"Не удалось распознать новую дату и время: {date_str} {time_str}")

        return parsed.replace(tzinfo=tz)

    else:
        raise ValueError("В payload нет данных для вычисления нового времени (shift_minutes, in_minutes или date+time)")


def _get_target_event_id(payload: dict, event_row) -> str:
    """Пункт: Если series=True, берем ID серии, иначе вычисляем ID одного вхождения."""
    google_event_id = getattr(event_row, 'google_event_id', None)
    if not google_event_id:
        raise ValueError("У события в БД нет google_event_id")

    # Если событие НЕ повторяющееся, ID у него одно
    if not getattr(event_row, 'rrule', None):
        return google_event_id

    # Если это серия, смотрим флаг series
    # (Внимание: в текущем коде Анны to_operation_payload НЕ передает series.
    # Нужно попросить её добавить его в payload, либо по умолчанию считаем False)
    series = payload.get("series", False) or payload.get("changes", {}).get("series", False)

    if series:
        return google_event_id

    # Если series=False, генерируем ID одного вхождения: {base_id}_{YYYYMMDDTHHMMSSZ}
    original_start = event_row.starts_at
    if original_start.tzinfo is None:
        # Если в БД время без пояса, принудительно вешаем пояс
        original_start = original_start.replace(tzinfo=ZoneInfo("Europe/Moscow"))

    utc_start = original_start.astimezone(timezone.utc)
    instance_id = f"{google_event_id}_{utc_start.strftime('%Y%m%dT%H%M%SZ')}"
    return instance_id


async def apply_operation(payload: dict, event_row, token_storage, user_tz: str = "Europe/Moscow") -> dict:
    """
    Применяет операцию переноса или отмены к событию.
    payload: результат to_operation_payload() из NLP
    event_row: объект события из БД
    """
    if not payload:
        raise ValueError("Пустой payload")

    intent = payload.get("intent")
    if intent not in ("move", "cancel"):
        raise ValueError(f"Неподдерживаемый intent: {intent}")

    # Получаем правильный ID (всей серии или одного вхождения)
    target_id = _get_target_event_id(payload, event_row)

    user_id = getattr(event_row, 'user_id', None)
    if not user_id:
        raise ValueError("У события в БД нет user_id")

    # ОТМЕНА
    if intent == "cancel":
        logger.info(f"Отмена события {target_id} (user_id={user_id})")
        return await delete_event(user_id, token_storage, target_id)

    # ПЕРЕНОС
    elif intent == "move":
        changes = payload.get("changes", {})
        duration = _get_duration_minutes(event_row)

        current_start = event_row.starts_at
        if current_start.tzinfo is None:
            current_start = current_start.replace(tzinfo=ZoneInfo(user_tz))

        new_start = _resolve_new_time(changes, current_start, user_tz)
        new_end = new_start + timedelta(minutes=duration)

        logger.info(f"Перенос события {target_id} на {new_start.isoformat()} (user_id={user_id})")
        return await update_event_time(
            user_id=user_id,
            token_storage=token_storage,
            event_id=target_id,
            new_start_time=new_start.isoformat(),
            new_end_time=new_end.isoformat(),
            timezone=user_tz
        )