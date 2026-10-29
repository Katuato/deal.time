import re

ALLOWED_FREQS = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
ALLOWED_DAYS = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}
UNTIL_REGEX = re.compile(r"^\d{8}(T\d{6}Z)?$")


def build_rrule(
    freq: str,
    interval: int = 1,
    byday: list[str] | None = None,
    count: int | None = None,
    until: str | None = None,
) -> str:
    """
    Преобразует параметры периодичности в строку стандарта RRULE (RFC 5545).

    :param freq: Частота ('DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY')
    :param interval: Интервал повторения (по умолчанию 1)
    :param byday: Список дней недели, например ['MO', 'WE', 'FR']
    :param count: Количество повторений (>= 1)
    :param until: Дата окончания в формате 'YYYYMMDD', 'YYYY-MM-DD' или 'YYYYMMDDTHHMMSSZ'
    :return: Готовая строка RRULE для Google Calendar API
    """
    if not freq or not isinstance(freq, str) or freq.strip().upper() not in ALLOWED_FREQS:
        raise ValueError(f"Неподдерживаемая частота повторения: {freq}. Допустимые: {ALLOWED_FREQS}")

    if interval is not None and (not isinstance(interval, int) or isinstance(interval, bool) or interval < 1):
        raise ValueError("Интервал повторения (interval) должен быть целым числом >= 1")

    if count is not None and until is not None:
        raise ValueError("Параметры 'count' и 'until' взаимоисключающие по стандарту RFC 5545")

    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 1):
        raise ValueError("Количество повторений (count) должно быть целым числом >= 1")

    parts = [f"FREQ={freq.strip().upper()}"]

    if interval and interval > 1:
        parts.append(f"INTERVAL={interval}")

    if byday:
        if not isinstance(byday, list) or not all(isinstance(d, str) for d in byday):
            raise ValueError("Параметр 'byday' должен быть списком строк")
        # Убираем пробелы, приводим к верхнему регистру и удаляем дубликаты с сохранением порядка
        normalized_days = list(dict.fromkeys(day.strip().upper() for day in byday if day.strip()))
        invalid_days = [day for day in normalized_days if day not in ALLOWED_DAYS]
        if invalid_days:
            raise ValueError(f"Некорректные дни недели: {invalid_days}. Допустимые: {ALLOWED_DAYS}")
        if normalized_days:
            parts.append(f"BYDAY={','.join(normalized_days)}")

    if count is not None:
        parts.append(f"COUNT={count}")
    elif until is not None:
        # Убираем дефисы, если LLM прислала дату в ISO-формате '2026-12-31'
        clean_until = until.strip().upper().replace("-", "")
        if "T" not in clean_until and len(clean_until) == 8 and clean_until.isdigit():
            clean_until = f"{clean_until}T235959Z"
        if not UNTIL_REGEX.match(clean_until):
            raise ValueError(
                f"Некорректный формат даты окончания 'until': {until}. Ожидается 'YYYYMMDD' или 'YYYYMMDDTHHMMSSZ'"
            )
        parts.append(f"UNTIL={clean_until}")

    return "RRULE:" + ";".join(parts)
