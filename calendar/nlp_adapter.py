from datetime import datetime, timedelta


# --- Функция для разбора строки RRULE (Пункт 3) ---
def _parse_rrule_string(rrule_str: str) -> dict:
    """Разбирает строку 'FREQ=WEEKLY;INTERVAL=2;BYDAY=WE,FR' в словарь для build_rrule."""
    if not rrule_str:
        return None

    result = {}
    # Анна может передать как с префиксом, так и без
    clean_str = rrule_str.replace("RRULE:", "")

    for part in clean_str.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.upper()

        if key == "FREQ":
            result["freq"] = value
        elif key == "INTERVAL":
            result["interval"] = int(value)
        elif key == "BYDAY":
            result["byday"] = value.split(",")
        elif key == "COUNT":
            result["count"] = int(value)
        elif key == "UNTIL":
            result["until"] = value

    return result


# --- Функция для конвертации UTC-смещения в IANA (Пункт 6) ---
def _ensure_iana_timezone(tz_str: str, fallback_tz: str = "Europe/Moscow") -> str:
    """Превращает 'UTC+05:00' в 'Asia/Yekaterinburg'."""
    if not tz_str:
        return fallback_tz

    # Если это уже IANA (содержит слэш)
    if "/" in tz_str:
        return tz_str

    utc_offsets = {
        "UTC+02:00": "Europe/Kaliningrad",
        "UTC+03:00": "Europe/Moscow",
        "UTC+04:00": "Europe/Samara",
        "UTC+05:00": "Asia/Yekaterinburg",
        "UTC+06:00": "Asia/Omsk",
        "UTC+07:00": "Asia/Novosibirsk",
        "UTC+08:00": "Asia/Irkutsk",
        "UTC+09:00": "Asia/Yakutsk",
        "UTC+10:00": "Asia/Vladivostok",
    }
    return utc_offsets.get(tz_str, fallback_tz)


# --- Главная функция адаптера ---
def adapt_nlp_to_calendar(nlp_event: dict, user_tz: str = "Europe/Moscow") -> dict:
    """
    Принимает словарь от NLP-модуля, возвращает словарь для create_event.
    """
    # Пункт 7: Проверка на обязательные поля и intent
    if not nlp_event or nlp_event.get("intent") != "create":
        raise ValueError("Модуль NLP вернул не событие для создания (intent != create) или пустой ответ.")

    starts_at = nlp_event.get("starts_at")
    if not starts_at:
        raise ValueError("Модуль NLP не передал время начала (starts_at).")

    # Пункт 1: summary
    summary = nlp_event.get("summary") or "Встреча"

    # Пункт 2: Время начала и окончания (ИСПРАВЛЕНО)
    start_time = starts_at
    end_time = nlp_event.get("ends_at")

    if not end_time:
        try:
            # Парсим время начала, чтобы прибавить к нему минуты
            dt = datetime.fromisoformat(start_time)
            duration = nlp_event.get("duration_minutes")

            if duration:
                # Если duration_minutes есть, прибавляем его
                end_time = (dt + timedelta(minutes=int(duration))).isoformat()
            else:
                # Если duration_minutes нет, прибавляем 60 минут по умолчанию
                end_time = (dt + timedelta(minutes=60)).isoformat()
        except ValueError:
            # Если start_time пришел в кривом формате, оставляем None
            # (calendar_connector потом выдаст ошибку валидации)
            end_time = None

    # Пункт 3: RRULE (вызываем функцию парсинга строки)
    rrule_input = nlp_event.get("rrule") or nlp_event.get("recurrence")
    recurrence = _parse_rrule_string(rrule_input)

    # Пункт 4: Участники (в description)
    attendees = nlp_event.get("attendees", [])
    description = nlp_event.get("description", "")
    if attendees:
        names_str = ", ".join(attendees)
        if description:
            description += f"\nУчастники: {names_str}"
        else:
            description = f"Участники: {names_str}"
    if not description:
        description = "Создано ботом ДЕЛУ.ВРЕМЯ"

    # Пункт 5: location
    location = nlp_event.get("location")

    # Пункт 6: Timezone (вызываем функцию конвертации)
    timezone = _ensure_iana_timezone(nlp_event.get("timezone"), user_tz)

    # Собираем финальный словарь для calendar_connector.py
    return {
        "summary": summary,
        "start_time": start_time,
        "end_time": end_time,
        "recurrence": recurrence,
        "description": description,
        "location": location,
        "timezone": timezone,
        "attendees": []  # Пункт 4: Не передаем имена в email
    }