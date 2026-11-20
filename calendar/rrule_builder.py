"""
Генератор RRULE (RFC 5545) для Google Calendar API.
Требования ТЗ: F-B-10, NF-29.

Правки относительно исходной версии:
  * исправлен парсинг `until` (ISO с 'T' и ':' раньше не проходил regex);
  * добавлен параметр `timezone`: `UNTIL` без явного Z трактуется в TZ пользователя
    и переводится в UTC (иначе последнее повторение могло «съезжать» на следующий день);
  * валидация частот/дней/интервалов сохранена без изменений.
"""
import re
from datetime import datetime, timezone as utc_timezone
from zoneinfo import ZoneInfo

ALLOWED_FREQS = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
ALLOWED_DAYS = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}

# Вход: "YYYY-MM-DD", "YYYYMMDD", "YYYY-MM-DDTHH:MM:SS[Z]", "YYYYMMDDTHHMMSSZ"
_UNTIL_INPUT_RE = re.compile(
    r"^(?P<y>\d{4})-?(?P<m>\d{2})-?(?P<d>\d{2})"
    r"(?:T(?P<hh>\d{2}):?(?P<mi>\d{2}):?(?P<ss>\d{2})(?P<z>Z)?)?$"
)
# Выход: строго "YYYYMMDD" или "YYYYMMDDTHHMMSSZ" (RFC 5545 §3.3.10)
_UNTIL_OUTPUT_RE = re.compile(r"^\d{8}(T\d{6}Z)?$")


def _normalize_until(until: str, tz_name: str) -> str:
    if not until or not isinstance(until, str):
        raise ValueError("Параметр 'until' должен быть непустой строкой")

    raw = until.strip().upper().replace(" ", "")
    if _UNTIL_OUTPUT_RE.match(raw):
        return raw

    m = _UNTIL_INPUT_RE.match(raw)
    if not m:
        raise ValueError(
            f"Некорректный формат даты окончания 'until': {until}. "
            f"Ожидается 'YYYY-MM-DD' или 'YYYY-MM-DDTHH:MM:SS[Z]'"
        )

    y, mo, d = int(m["y"]), int(m["m"]), int(m["d"])
    if m["hh"] is not None:
        hh, mi, ss = int(m["hh"]), int(m["mi"]), int(m["ss"])
        naive = datetime(y, mo, d, hh, mi, ss)
    else:
        naive = datetime(y, mo, d, 23, 59, 59)

    if m["z"]:
        aware_utc = naive.replace(tzinfo=utc_timezone.utc)
    else:
        aware_utc = naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(utc_timezone.utc)

    return aware_utc.strftime("%Y%m%dT%H%M%SZ")


def build_rrule(
    freq: str,
    interval: int = 1,
    byday: list[str] | None = None,
    count: int | None = None,
    until: str | None = None,
    timezone: str = "Europe/Moscow",
) -> str:
    """
    :param timezone: IANA-таймзона пользователя — используется только для `until` без Z.
    """
    if not freq or not isinstance(freq, str) or freq.strip().upper() not in ALLOWED_FREQS:
        raise ValueError(
            f"Неподдерживаемая частота повторения: {freq}. Допустимые: {ALLOWED_FREQS}"
        )

    if interval is not None and (
        not isinstance(interval, int) or isinstance(interval, bool) or interval < 1
    ):
        raise ValueError("Интервал повторения (interval) должен быть целым числом >= 1")

    if count is not None and until is not None:
        raise ValueError("Параметры 'count' и 'until' взаимоисключающие по стандарту RFC 5545")

    if count is not None and (
        not isinstance(count, int) or isinstance(count, bool) or count < 1
    ):
        raise ValueError("Количество повторений (count) должно быть целым числом >= 1")

    parts = [f"FREQ={freq.strip().upper()}"]

    if interval and interval > 1:
        parts.append(f"INTERVAL={interval}")

    if byday:
        if not isinstance(byday, list) or not all(isinstance(d, str) for d in byday):
            raise ValueError("Параметр 'byday' должен быть списком строк")
        normalized_days = list(dict.fromkeys(day.strip().upper() for day in byday if day.strip()))
        invalid_days = [day for day in normalized_days if day not in ALLOWED_DAYS]
        if invalid_days:
            raise ValueError(
                f"Некорректные дни недели: {invalid_days}. Допустимые: {ALLOWED_DAYS}"
            )
        if normalized_days:
            parts.append(f"BYDAY={','.join(normalized_days)}")

    if count is not None:
        parts.append(f"COUNT={count}")
    elif until is not None:
        parts.append(f"UNTIL={_normalize_until(until, timezone)}")

    return "RRULE:" + ";".join(parts)