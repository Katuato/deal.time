# -*- coding: utf-8 -*-
"""NLP-модуль бота ДЕЛУ.ВРЕМЯ.

Модель RAGU-lm определяет смысл сообщения: intent (create/move/cancel/None), тему,
место и участников. Дату, время, длительность и повторы считают правила, календарную
дату - dateparser. На выходе готовые starts_at/ends_at/rrule для таблицы events.

Часовой пояс берется из users.timezone (параметр tz). Если в тексте пояс назван явно
("по мск", "по екб", "мск+2"), используется он.

Зависимости: pydantic, dateparser, torch, transformers.
Проверка правил без модели: python nlp_lite.py --selftest
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import re
import threading
import time as _time
from datetime import datetime, timedelta, timezone as _tz
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator
from config import logger as log

MODEL_NAME = "RaguTeam/RAGU-lm"
DEFAULT_TZ = "Europe/Moscow"
MAX_TEXT_LEN = 1500
MAX_CTX_LEN = 300
MAX_NEW_TOKENS = 64

MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
MONTHS_RE = "|".join(MONTHS_GEN)
PERIOD_RE = "утра|дня|вечера|ночи"


def alt(words) -> str:
    return "|".join(sorted(words, key=len, reverse=True))


def low(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


class Event(BaseModel):
    intent: Optional[str] = None          # create / move / cancel / None
    summary: Optional[str] = None
    date: Optional[str] = None            # "завтра", "в пятницу", "25 ноября"
    time: Optional[str] = None            # HH:MM
    end_time: Optional[str] = None
    duration_minutes: Optional[int] = None
    in_minutes: Optional[int] = None      # "через час" = 60
    shift_minutes: Optional[int] = None   # "на час позже" = 60, "на 20 минут раньше" = -20
    location: Optional[str] = None
    recurrence: Optional[str] = None      # исходная фраза, например "по средам"
    rrule: Optional[str] = None           # RFC 5545, например FREQ=WEEKLY;BYDAY=WE
    series: bool = False                  # отмена/перенос всей серии
    timezone: Optional[str] = None        # только если назван в тексте
    time_options: List[str] = Field(default_factory=list)
    time_ambiguous: bool = False          # "в 3" - 03:00 или 15:00
    attendees: List[str] = Field(default_factory=list)
    starts_at: Optional[str] = None       # ISO 8601 с поясом
    ends_at: Optional[str] = None

    @field_validator("intent", mode="before")
    @classmethod
    def check_intent(cls, v):
        return v if v in ("create", "move", "cancel") else None

    @field_validator("time", "end_time", mode="before")
    @classmethod
    def check_hhmm(cls, v):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", v) if isinstance(v, str) else None
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return None
        return "%02d:%s" % (int(m.group(1)), m.group(2))

    @field_validator("summary", "date", "location", "recurrence", "timezone", mode="before")
    @classmethod
    def check_text(cls, v):
        if not isinstance(v, str) or v.strip().lower() in ("", "null", "none"):
            return None
        return v.strip()

    @field_validator("duration_minutes", "in_minutes", "shift_minutes", mode="before")
    @classmethod
    def check_int(cls, v):
        try:
            n = int(v)
        except (TypeError, ValueError, OverflowError):
            return None
        return n if 0 < abs(n) <= 7 * 24 * 60 else None

    @field_validator("attendees", mode="before")
    @classmethod
    def check_names(cls, v):
        if not isinstance(v, list):
            return []
        return [x.strip() for x in v if isinstance(x, str) and x.strip()]


ExtractedEvent = Event   # старое имя, используется в eval_nlp.py


# ---------------------------------------------------------------- нормализация текста

SLANG = {
    "седня": "сегодня", "сёдня": "сегодня", "сення": "сегодня", "севодня": "сегодня", "сеня": "сегодня",
    "завтро": "завтра", "послезавтро": "послезавтра", "птн": "пт", "пнд": "пн", "втр": "вт", "срд": "ср",
    "чтв": "чт", "сбт": "сб", "лан": "ладно", "лады": "ладно", "кароч": "короче", "щас": "сейчас",
}
TYPO_TARGETS = ["сегодня", "завтра", "послезавтра", "понедельник", "вторник", "среду", "четверг",
                "пятницу", "субботу", "воскресенье", "следующий", "следующую", "вечера", "утра"] + MONTHS_GEN
# настоящие слова, похожие на слова из TYPO_TARGETS
NOT_TYPO = ("завтрак", "вчера", "позавчера", "вечер", "утр", "суббот", "пятниц", "четверг", "вторник",
            "понедельник", "воскресень", "сред", "следующ", "сегодняшн", "завтрашн")

HOUR_WORDS = {"час": 1, "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
              "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
              "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
              "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20}
SMALL_HOURS = {k: v for k, v in HOUR_WORDS.items() if v <= 12}
MINUTE_WORDS = {"ноль ноль": 0, "ровно": 0, "пятнадцать": 15, "тридцать": 30, "сорок пять": 45}
ORDINALS = {"первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "пятого": 5, "шестого": 6,
            "седьмого": 7, "восьмого": 8, "девятого": 9, "десятого": 10, "одиннадцатого": 11,
            "двенадцатого": 12}
DATIVE_HOURS = {"двум": 2, "трем": 3, "четырем": 4, "пяти": 5, "шести": 6, "семи": 7, "восьми": 8,
                "девяти": 9, "десяти": 10, "одиннадцати": 11, "двенадцати": 12}


def fix_typo(word: str) -> str:
    w = low(word)
    if len(w) < 5 or w in TYPO_TARGETS or w.startswith(NOT_TYPO):
        return word
    best = difflib.get_close_matches(w, TYPO_TARGETS, n=1, cutoff=0.86)
    return best[0] if best else word


def apply_period(h: int, period: Optional[str]) -> int:
    if not period or h > 12:
        return h
    period = period.lower()
    if period == "утра":
        return 0 if h == 12 else h
    if period == "ночи":
        if h == 12:
            return 0
        return h + 12 if h >= 6 else h
    if period == "дня":
        return h + 12 if h <= 6 else h
    return h + 12 if h < 12 else h


def _word_time(m):
    h = HOUR_WORDS[m.group(2).lower()]
    mi = MINUTE_WORDS[re.sub(r"\s+", " ", m.group(3).lower())]
    return "%s %d:%02d" % (m.group(1), h, mi)


def _quarter(m):
    h = apply_period(ORDINALS[m.group(1).lower()] - 1 or 12, m.group(2))
    return "%02d:15" % h


def _without(m):
    h = apply_period(SMALL_HOURS[m.group(1).lower()] - 1 or 12, m.group(2))
    return "%02d:45" % h


RE_WORD_TIME = re.compile(r"\b(в|к)\s+(%s)\s+(%s)\b(?!\s+(?:минут|час))" % (alt(HOUR_WORDS), alt(MINUTE_WORDS)), re.I)
RE_QUARTER = re.compile(r"\bчетверть\s+(%s)(?:\s+(%s))?\b" % (alt(ORDINALS), PERIOD_RE), re.I)
RE_WITHOUT = re.compile(r"\bбез\s+(?:пятнадцати|четверти)\s+(%s)(?:\s+(%s))?\b" % (alt(SMALL_HOURS), PERIOD_RE), re.I)


def normalize_text(text: str) -> str:
    """Опечатки, сленг и разные записи времени: "седня в15:00" -> "сегодня в 15:00"."""
    s = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]+", " ", text or "")
    s = re.sub(r"[А-Яа-яЁё]+", lambda m: SLANG.get(m.group(0).lower(), m.group(0)), s)
    s = re.sub(r"[А-Яа-яЁё]+", lambda m: fix_typo(m.group(0)), s)
    s = s.replace("ё", "е").replace("Ё", "Е")
    s = re.sub(r"(?i)\b(в|к|с|до)(\d{1,2}[:.]\d{2})", r"\1 \2", s)
    s = re.sub(r"(?i)\b(в|к|с|до)\s+([01]?\d|2[0-3])\s+([0-5]\d)\b"
               r"(?!\s*(?:минут|мин|человек|руб|%|раз|лет|числ|" + MONTHS_RE + "))", r"\1 \2:\3", s)
    s = re.sub(r"(?i)\b(в|к|с|до)\s+([01]?\d|2[0-3])\s*ч\b\.?", r"\1 \2:00", s)
    s = re.sub(r"(?i)\bв\s+полдень\b", "в 12:00", s)
    s = re.sub(r"(?i)\bв\s+полночь\b", "в 00:00", s)
    s = RE_WORD_TIME.sub(_word_time, s)
    s = RE_QUARTER.sub(_quarter, s)
    s = RE_WITHOUT.sub(_without, s)
    # "сб 14:00 футбол" -> "в сб 14:00 футбол"
    s = re.sub(r"(?i)(^|[\s,])(пн|вт|ср|чт|пт|сб|вс)(?=\s+(?:в\s+)?\d)", lambda m: m.group(1) + "в " + m.group(2).lower(), s)
    s = re.sub(r"(?i)^(пн|вт|ср|чт|пт|сб|вс)\b", lambda m: "в " + m.group(1).lower(), s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


# ---------------------------------------------------------------- время

NOT_TIME = r"(?!\s*(?:руб|₽|%|кг|км|мин|человек|раз|лет|года|класс|штук|числ|час))"
TIME_PATTERNS = [
    ("hhmm", re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?!\d)(?:\s+(%s))?" % PERIOD_RE)),
    ("hhmm", re.compile(r"\b(?:в|к|с|до|около)\s+([01]?\d|2[0-3])[.,]([0-5]\d)(?![\d:])(?:\s+(%s))?%s" % (PERIOD_RE, NOT_TIME))),
    # "завтра 15.00"; минуты до 12 не берем, это может быть дата 15.11
    ("hhmm", re.compile(r"(?<![\d.,:/-])([01]?\d|2[0-3])\.(00|1[3-9]|[2-5]\d)(?![\d:.])(?:\s+(%s))?%s" % (PERIOD_RE, NOT_TIME))),
    ("half", re.compile(r"\b(?:пол\s*-?\s*|половин\w*\s+)(%s)(?:\s+(%s))?\b" % (alt(ORDINALS), PERIOD_RE))),
    ("period", re.compile(r"\b(\d{1,2}|%s)\s*(?:час\w*\s+)?(%s)\b" % (alt(SMALL_HOURS), PERIOD_RE))),
    ("dative", re.compile(r"\bк\s+(%s)(?:\s+час\w*)?(?:\s+(%s))?\b" % (alt(DATIVE_HOURS), PERIOD_RE))),
    ("hours", re.compile(r"\bв\s+(\d{1,2})\s+час(?:ов|а)?\b(?!\s+(?:%s))" % PERIOD_RE)),
    ("range", re.compile(r"\bс\s+(\d{1,2})\s+до\s+(\d{1,2})(?![\d:])(?:\s+(%s))?%s" % (PERIOD_RE, NOT_TIME))),
]
# "в 3", "к 9", "в пять" без утра/вечера
RE_BARE_HOUR = re.compile(
    r"\b(?:в|к)\s+(\d{1,2}|%s)\b(?![:.,]\d)(?!\s*(?:час|%s|%s))"
    r"(?!\s*(?:человек|раз|лет|года|минут|%%|руб|штук|класс|дн|недел|месяц|групп|кабинет|комнат|этаж|подъезд))"
    % (alt(SMALL_HOURS), PERIOD_RE, MONTHS_RE))
# слова перед старым временем: "вместо 15:00", "не в 10", "было в 9"
RE_OLD = re.compile(r"(?:\bвместо|\bа\s+не|\bбыло|\bбыла|\bне\s+в|\bне\s+на)\s*(?:в\s+|на\s+)?$")
RE_EVENING = re.compile(r"ужин|кино|бар\b|вечерин|концерт|клуб|тус|пив|театр")


def find_times(text: str) -> List[Tuple[int, str, bool]]:
    """Явно названное время: список (позиция, "HH:MM", старое ли)."""
    t = low(text)
    found = []
    for kind, rx in TIME_PATTERNS:
        for m in rx.finditer(t):
            g = m.groups()
            if kind == "hhmm":
                values = [(apply_period(int(g[0]), g[2]), int(g[1]))]
            elif kind == "half":
                h = ORDINALS[g[0]] - 1 or 12
                values = [(apply_period(h, g[1]) if g[1] else (h + 12 if h <= 6 else h), 30)]
            elif kind == "period":
                h = int(g[0]) if g[0].isdigit() else SMALL_HOURS[g[0]]
                values = [(apply_period(h, g[1]), 0)] if h <= 23 else []
            elif kind == "dative":
                h = DATIVE_HOURS[g[0]]
                values = [(apply_period(h, g[1]) if g[1] else (h + 12 if h <= 6 else h), 0)]
            elif kind == "hours":
                values = [(int(g[0]), 0)] if int(g[0]) <= 23 else []
            else:
                a, b = int(g[0]), int(g[1])
                values = [(apply_period(a, g[2]), 0), (apply_period(b, g[2]), 0)] if a < b <= 23 else []
            old = bool(RE_OLD.search(t[max(0, m.start() - 14):m.start()]))
            for i, (h, mi) in enumerate(values):
                found.append((m.start() + i, "%02d:%02d" % (h, mi), old))
    found.sort()
    result = []
    for item in found:
        if not result or result[-1][1] != item[1]:
            result.append(item)
    return result


def find_bare_hours(text: str) -> List[Tuple[int, int, bool]]:
    """Час без утра/вечера: список (позиция, час, старое ли)."""
    t = low(text)
    out = []
    for m in RE_BARE_HOUR.finditer(t):
        tok = m.group(1)
        h = int(tok) if tok.isdigit() else SMALL_HOURS[tok]
        if 1 <= h <= 23:
            out.append((m.start(), h, bool(RE_OLD.search(t[max(0, m.start() - 14):m.start() + 2]))))
    return out


def guess_hour(h: int, text: str) -> str:
    """"в 3" обычно 15:00, "ужин в 8" - 20:00."""
    evening = h <= 6 or (h <= 11 and RE_EVENING.search(low(text)))
    return "%02d:00" % (h + 12 if evening and h < 12 else h)


# ---------------------------------------------------------------- дата

WEEKDAYS = {"понедельник": 0, "вторник": 1, "среду": 2, "среда": 2, "четверг": 3, "пятницу": 4,
            "пятница": 4, "субботу": 5, "суббота": 5, "воскресенье": 6}
WD_SHORT = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
WD_ACC = ["в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье"]
WD_NEXT = ["в следующий понедельник", "в следующий вторник", "в следующую среду", "в следующий четверг",
           "в следующую пятницу", "в следующую субботу", "в следующее воскресенье"]
WD_NAMES = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
DATE_PATTERNS = [
    ("next", re.compile(r"\b(?:в|во|на)\s+след(?:ующ\w*|\.)?\s*(%s)\b" % alt(WEEKDAYS))),
    ("next", re.compile(r"\bна\s+следующ\w+\s+недел\w+\s+(?:в|во)\s+(%s)\b" % alt(WEEKDAYS))),
    ("rel", re.compile(r"\b(послезавтра|завтра|сегодня)\b")),
    ("wd", re.compile(r"\b(%s)\b" % alt(WEEKDAYS))),
    ("short", re.compile(r"\b(?:в|во|на)\s+(пн|вт|ср|чт|пт|сб|вс)\b")),
    ("day", re.compile(r"\b(\d{1,2})(?:-?го)?\s+(числа|%s)\b" % MONTHS_RE)),
    ("day", re.compile(r"\b(%s)\s+(числа|%s)\b" % (alt(ORDINALS), MONTHS_RE))),
    ("num", re.compile(r"(?<![\d.:])(0?[1-9]|[12]\d|3[01])[./](0?[1-9]|1[0-2])(?:[./](\d{2,4}))?(?![\d:])"
                       r"(?!\s*(?:час|раз|кг|км|л\b|%|руб|мин))")),
    ("in", re.compile(r"\bчерез\s+(?:(\d+|два|две|три)\s+)?(дн\w+|день|недел\w+)\b")),
]


def find_date(text: str) -> Optional[str]:
    """Дата в одном виде: "завтра", "в пятницу", "в следующую среду", "25 ноября", "5 числа"."""
    t = low(text)
    for kind, rx in DATE_PATTERNS:
        for m in rx.finditer(t):
            if kind == "next":
                return WD_NEXT[WEEKDAYS[m.group(1)]]
            if kind == "rel":
                return m.group(1)
            if kind == "wd":
                if re.search(r"(?:прошл\w*|по)\s*$", t[:m.start()]):
                    continue
                return WD_ACC[WEEKDAYS[m.group(1)]]
            if kind == "short":
                return WD_ACC[WD_SHORT[m.group(1)]]
            if kind == "day":
                d = int(m.group(1)) if m.group(1).isdigit() else ORDINALS[m.group(1)]
                if 1 <= d <= 31:
                    year = re.match(r"\s+(\d{4})\b", t[m.end():])
                    return "%d %s%s" % (d, m.group(2), " " + year.group(1) if year else "")
                continue
            if kind == "num":
                if re.match(r"\s*(?:утра|вечера|дня|ночи)", t[m.end():]):
                    continue
                return m.group(0)
            return m.group(0)
    return None


def resolve_date(phrase: Optional[str], now: datetime) -> Optional[datetime]:
    if not phrase:
        return None
    import dateparser
    base = now.replace(tzinfo=None)
    p = low(phrase)
    m = re.match(r"(\d{1,2}) числа", p)
    if m:
        y, mo, d = base.year, base.month, int(m.group(1))
        if d < base.day:
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        try:
            return base.replace(year=y, month=mo, day=d, hour=0, minute=0)
        except ValueError:
            return None
    is_next = "следующ" in p
    p = re.sub(r"следующ\w+\s+", "", p)
    dt = dateparser.parse(p, languages=["ru"], settings={"RELATIVE_BASE": base, "PREFER_DATES_FROM": "future"})
    # dateparser не знает "следующую пятницу": берем пятницу следующей недели
    if dt and is_next and dt.isocalendar()[1] == base.isocalendar()[1]:
        dt += timedelta(days=7)
    return dt


# ---------------------------------------------------------------- длительность, сдвиг, повторы, пояс

AMOUNT = (r"(полчас\w*|полтора\s+часа|час(?:ик|ок)?(?!\w)|\d{1,3}\s*(?:минут\w*|мин\b)"
          r"|(?:\d{1,2}|два|три|четыре|пять)\s+час(?:а|ов)?\b)")
RE_DURATION = re.compile(r"\bна\s+%s(?!\s+(?:позже|раньше|вперед|назад))" % AMOUNT)
RE_OFFSET = re.compile(r"\bчерез\s+%s" % AMOUNT)
RE_OFFSET_REV = re.compile(r"\b(минут\w*|час\w*)\s+через\s+(\d{1,3})\b")
RE_SHIFT = re.compile(r"\bна\s+%s\s+(позже|раньше|вперед|назад)\b" % AMOUNT)
RE_LATE_BY = re.compile(r"\b(?:опоздаю|задержусь|опаздываю|задерживаюсь)\s+(?:минут\s+на\s+(\d{1,3})|на\s+%s)" % AMOUNT)


def amount_minutes(p: str) -> int:
    p = re.sub(r"\s+", " ", p.strip())
    if p.startswith("полчас"):
        return 30
    if p.startswith("полтора"):
        return 90
    if p.startswith("час"):
        return 60
    m = re.match(r"(\d+)\s*мин", p)
    if m:
        return int(m.group(1))
    first = p.split()[0]
    return (int(first) if first.isdigit() else {"два": 2, "три": 3, "четыре": 4, "пять": 5}[first]) * 60


def find_duration(text: str) -> Optional[int]:
    m = RE_DURATION.search(low(text))
    return amount_minutes(m.group(1)) if m else None


def find_offset(text: str) -> Optional[int]:
    t = low(text)
    m = RE_OFFSET.search(t)
    if m:
        return amount_minutes(m.group(1))
    m = RE_OFFSET_REV.search(t)
    if m:
        return int(m.group(2)) * (60 if m.group(1).startswith("час") else 1)
    return None


def find_shift(text: str) -> Optional[int]:
    t = low(text)
    m = RE_SHIFT.search(t)
    if m:
        sign = 1 if m.group(2) in ("позже", "вперед") else -1
        return sign * amount_minutes(m.group(1))
    m = RE_LATE_BY.search(t)
    if m:
        return int(m.group(1)) if m.group(1) else amount_minutes(m.group(2))
    return None


BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
WD_STEMS = ["понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень"]
RE_REPEAT = re.compile(
    r"\bпо\s+(понедельник|вторник|сред|четверг|пятниц|суббот|воскресень|будн|выходн|утрам|вечерам)\w*"
    r"|\bкажд(?:ый|ую|ое|ые)\s+(?:(\d+|две|два|три)\s+)?(день|дня|дней|недел\w*|месяц\w*|год\w*|понедельник"
    r"|вторник|сред\w|четверг|пятниц\w|суббот\w|воскресень\w|утро|вечер|будн\w+|выходн\w+)"
    r"|\b(ежедневно|еженедельно|ежемесячно|ежегодно)\b"
    r"|\bраз\s+в\s+(две\s+недели|день|неделю|месяц|год)\b")


def find_recurrence(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Повтор: (фраза, RRULE). "по средам" -> FREQ=WEEKLY;BYDAY=WE."""
    t = low(text)
    m = RE_REPEAT.search(t)
    if not m:
        return None, None
    phrase = m.group(0)
    unit = next((x for x in (m.group(1), m.group(3), m.group(4), m.group(5)) if x), "")
    n_raw = m.group(2) or ""
    interval = int(n_raw) if n_raw.isdigit() else {"две": 2, "два": 2, "три": 3}.get(n_raw, 1)
    if unit == "две недели":
        interval = 2

    days = []
    if re.search(r"будн", t):
        days = [0, 1, 2, 3, 4]
    elif re.search(r"выходн", t):
        days = [5, 6]
    else:
        for i, stem in enumerate(WD_STEMS):
            if re.search(r"\b" + stem, t):
                days.append(i)
        for short, i in WD_SHORT.items():
            if re.search(r"\b(?:в|во|по)\s+" + short + r"\b", t) and i not in days:
                days.append(i)

    if unit.startswith(("месяц", "ежемес")):
        rule = "FREQ=MONTHLY"
    elif unit.startswith(("год", "ежегод")):
        rule = "FREQ=YEARLY"
    elif days or unit.startswith(("недел", "еженед")) or unit == "две недели":
        rule = "FREQ=WEEKLY"
    else:
        rule = "FREQ=DAILY"
    if interval > 1:
        rule += ";INTERVAL=%d" % interval
    if days and rule.startswith("FREQ=WEEKLY"):
        rule += ";BYDAY=" + ",".join(BYDAY[i] for i in sorted(days))
        if unit.startswith("недел") and len(days) == 1:
            phrase += " " + list(WD_SHORT)[days[0]]   # "каждую неделю пн"
    return phrase, rule


TZ_NAMES = {"мск": "Europe/Moscow", "москв": "Europe/Moscow", "питер": "Europe/Moscow", "спб": "Europe/Moscow",
            "екб": "Asia/Yekaterinburg", "екатеринбург": "Asia/Yekaterinburg", "новосиб": "Asia/Novosibirsk",
            "калининград": "Europe/Kaliningrad", "самар": "Europe/Samara", "владивосток": "Asia/Vladivostok"}


def find_timezone(text: str) -> Optional[str]:
    t = low(text)
    m = re.search(r"\b(мск|utc|gmt)\s*([+-])\s*(\d{1,2})\b", t)
    if m:
        off = int(m.group(3)) * (1 if m.group(2) == "+" else -1) + (3 if m.group(1) == "мск" else 0)
        return "UTC%+03d:00" % off
    m = re.search(r"\bпо\s+(?:времени\s+)?(%s)\w*" % alt(TZ_NAMES), t) or re.search(r"\b(мск)\b", t)
    return TZ_NAMES[m.group(1)] if m else None


def get_zone(name: Optional[str]):
    if not name:
        return None
    m = re.fullmatch(r"UTC([+-]\d{2}):00", name)
    if m:
        return _tz(timedelta(hours=int(m.group(1))))
    try:
        return ZoneInfo(name)
    except Exception:
        return None


# ---------------------------------------------------------------- намерение

RE_CANCEL = re.compile(r"\bотмен\w*|\bотбой\b|\bне\s+будет\b")
RE_CANCEL_NEG = re.compile(r"\b(?:не|ни)\s+(?:\w+\s+){0,2}отмен\w*|\bотмен\w*\s+не\b")
RE_NOT_COMING = re.compile(
    r"\bне\s+(?:смогу|могу|получится|успею|выйдет)\s+(?:прийти|приехать|подъехать|быть|пойти|поехать"
    r"|встретиться|созвониться)\b|\bне\s+(?:приду|приеду|подъеду|пойду|поеду)\b|\bминус\s+я\b")
RE_SOFT_CANCEL = re.compile(r"\bне\s+(?:получится|смогу|выйдет|успею)\s*(?:[,.!)]|$|\s+(?:сегодня|завтра|в|к|на))"
                            r"|\bникак\b(?!\s+не)")
RE_APOLOGY = re.compile(r"\b(?:сорян|сори|сорри|извини\w*|прости\w*|увы|к\s+сожалению|блин)\b")
RE_SERIES = re.compile(r"\bсери[юяи]\b|\bвсе\s+(?:встречи|созвоны|занятия|тренировки)\b")
RE_MOVE = re.compile(r"перенес|перенос|сдвин|сдвиг|опозд|опазд|задерж|подвин|отлож|перекин")
RE_NOT_BUT = re.compile(r"\bне\s+(?:в|на|к)\s+[^,.!?]{1,15}?[,\s]+а\s+(?:в|на|к)\s+\S")
RE_PAST = re.compile(
    r"\b(?:вчера|позавчера)\b|\bв\s+прошл\w+\b|\bспасибо\s+за\b"
    r"|\b(?:был|была|было|были|прошл[аои]|прошел|звонил\w*|ходил\w*|ездил\w*|сидел\w*|посидел\w*|смотрел\w*"
    r"|играл\w*|начал\w*|закончил\w*|опоздали|встречались|виделись|прибыл\w*|приехал\w*|пришел|пришла"
    r"|пришли|ушел|ушла|уехал\w*|отметили|получил\w*)\b")
RE_FUTURE = re.compile(r"\b(?:завтра|послезавтра|давай\w*|будем|буду|будет|каждый|каждую|следующ\w*|пойдем"
                       r"|встретимся|созвонимся|напомни)\b")
# справка о расписании: "магазин работает до 22:00", "поезд отправляется в 14:20"
RE_INFO = re.compile(r"\b(?:работает|работают|открывается|закрывается|открыт|закрыт|прибывает|отправляется"
                     r"|длится|идет\s+\d+\s+час\w*|начинается\s+в\s+\d+\s+(?:утра|вечера)?\s*(?:по\s+будням|каждый))\b")
RE_AGREE = re.compile(r"\b(?:давай|встрет|созвон|позвон|поедем|пойдем|зайдем|запиши|напомни|поставь|запланир"
                      r"|назнач|пересеч|встреч|соберемся|сходим|сходить|погуляем|поужинаем|пообедаем|посидим|увидимся|пойдешь|придешь|идешь|погнали|сможешь|хочешь|перенес)\w*"
                      r"|\bго\b|\bкто\s+(?:в|на|со?)\b")
RE_CONFIRM = re.compile(r"^(?:ок|окей|оки|ok|да|ага|угу|давай|давайте|договорились|идет|подходит|го|норм|хорошо"
                        r"|ладно|супер|отлично|принято|заметано|согласен|согласна|можно|конечно|буду)$")
RE_ACTION = re.compile(r"\b(?:давай\w*|встретимся|созвон\w*|позвоню|приду|запиши|напомни|поставь|создай)\b")
FILLER = set("ок окей да ага давай давайте го тогда лучше можно может или а и ну так вот уже только часов часа "
             "в во на к с со до по у около ровно примерно где то это утра дня вечера ночи".split())


def has_when(text: str) -> bool:
    return bool(find_times(text) or find_date(text) or find_recurrence(text)[0] or find_bare_hours(text)
                or find_offset(text))


def is_confirmation(text: str) -> bool:
    words = re.findall(r"[а-яa-z]+", low(text))
    return 0 < len(words) <= 3 and all(RE_CONFIRM.match(w) for w in words)


def is_fragment(text: str) -> bool:
    """Кусок разбитого сообщения: только когда/где, без действия ("завтра", "в 15:00 у метро")."""
    return has_when(text) and not RE_ACTION.search(low(text))


def has_topic(text: str) -> bool:
    t = re.sub(r"\d+[:.]?\d*", " ", low(text))
    t = re.sub(r"\b(?:сегодня|завтра|послезавтра|%s|пн|вт|ср|чт|пт|сб|вс|%s|числа)\b" % (alt(WEEKDAYS), MONTHS_RE), " ", t)
    return any(w not in FILLER for w in re.findall(r"[а-яa-z]+", t))


def context_agrees(ctx: str) -> bool:
    """В предыдущих сообщениях есть предложение встретиться."""
    t = low(ctx)
    if not t or (RE_PAST.search(t) and not RE_FUTURE.search(t)):
        return False
    return bool(RE_AGREE.search(t)) or ("?" in t and has_when(ctx))


RE_WH_QUESTION = re.compile(r"^\W*(?:а\s+)?(?:кто|что|где|куда|зачем|почему|какой|какая|какие|сколько|чей|чья)\b")



def operation_is_negated(text: str) -> bool:
    """Block a denied operation, but preserve a separate positive operation."""
    clauses = re.split(r"[,;!?]|\b(?:но|а)\b", low(text))
    denied = False
    affirmative = False
    for clause in clauses:
        negative_move = re.search(
            r"\bне\s+(?:(?:надо|нужно|стоит|следует)\s+)?"
            r"(?:перенос\w*|перенес\w*|сдвиг\w*|сдвин\w*|подвин\w*|отлож\w*|отклад\w*)\b",
            clause,
        )
        pending_cancel = re.search(
            r"\bотмен(?:а|у|ы|е)\b[^,;!?]{0,80}\bне\s+"
            r"(?:подтвержд\w*|согласов\w*|одобря\w*)\b", clause,
        ) or re.search(
            r"\bне\s+(?:подтвержд\w*|согласов\w*|одобря\w*)"
            r"[^,;!?]{0,80}\bотмен\w*", clause,
        )
        negative_cancel = RE_CANCEL_NEG.search(clause)
        if negative_move or pending_cancel or negative_cancel:
            denied = True
        elif RE_MOVE.search(clause) or RE_CANCEL.search(clause):
            affirmative = True
    return denied and not affirmative


def decide_intent(llm_intent: Optional[str], text: str, ctx: str) -> Optional[str]:
    t = low(text)
    if operation_is_negated(t):
        return None
    if RE_PAST.search(t) and not RE_FUTURE.search(t):
        return None
    if RE_INFO.search(t) and not RE_AGREE.search(t):
        return None
    cancel = bool(RE_CANCEL.search(t)) and not RE_CANCEL_NEG.search(t)
    soft_cancel = (bool(RE_SOFT_CANCEL.search(t)) and not t.rstrip().endswith("?")
                   and bool(RE_APOLOGY.search(t) or find_date(t) or context_agrees(ctx)))
    times = find_times(text)
    move = bool(RE_MOVE.search(t) or find_shift(text) or RE_NOT_BUT.search(t) or any(old for _, _, old in times))
    if cancel or soft_cancel or RE_NOT_COMING.search(t):
        # "отмени, перенесем на 17:00" - это перенос
        if RE_MOVE.search(t) and any(not old for _, _, old in times):
            return "move"
        return "cancel"
    if move:
        return "move"
    if is_confirmation(text) and RE_MOVE.search(low(ctx)) and context_agrees(ctx) and has_when(ctx):
        return "move"   # "окей" на "перенесем на пятницу?"
    if llm_intent in ("move", "cancel"):
        # удалять или двигать событие только по ответу модели нельзя
        llm_intent = None
        own_when = bool(find_date(text) and (times or find_bare_hours(text)) and not t.rstrip().endswith("?"))
        if RE_AGREE.search(t) or own_when or ((is_confirmation(text) or is_fragment(text)) and context_agrees(ctx)):
            llm_intent = "create"
    if llm_intent is None:
        if find_offset(text) and not re.search(r"\b(?:будет|начнется|закончится|пройдет)\b", t):
            return "create"
        if (is_confirmation(text) or is_fragment(text)) and context_agrees(ctx) and has_when(ctx + " " + text):
            # "ок" на "перенесем на пятницу?" - согласие на перенос
            return "move" if is_confirmation(text) and RE_MOVE.search(low(ctx)) else "create"
        return None
    if RE_WH_QUESTION.search(t) and t.rstrip().endswith("?") and not has_when(text):
        return None   # "кто будет на созвоне?"
    if re.search(r"\bдо\s+(?:встречи|связи|скорого)\b", t) and not has_when(text):
        return None   # "спасибо, до встречи"
    if ctx and is_fragment(text) and not context_agrees(ctx) and not has_topic(text):
        return None   # "в 15:00" после "как дела?"
    return llm_intent


# ---------------------------------------------------------------- сборка события

DEFAULT_SUMMARY = {"create": "Встреча", "move": "Перенос встречи", "cancel": "Встреча"}


def clean_summary(s: Optional[str]) -> Optional[str]:
    """Убирает из темы дату и время: "Ужин в субботу в 19:00" -> "Ужин"."""
    if not s:
        return None
    s = re.sub(r"(?i)\s*\b(?:в|во|на|к|с|до)?\s*(?:\d{1,2}[:.]\d{2}|\d{1,2}\s+(?:%s))\b" % MONTHS_RE, " ", s)
    s = re.sub(r"(?i)\s*\b(?:в|во|на)?\s*(?:сегодня|завтра|послезавтра|%s)\b" % alt(WEEKDAYS), " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.-")
    if not s or re.search(r"(?:ть|ться|тся)$", low(s)) or low(s) in ("встретимся", "идем", "пойдем", "давай"):
        return None
    return s[0].upper() + s[1:]


def postprocess(text: str, data: dict, context: Optional[List[str]] = None,
                now: Optional[datetime] = None, tz: str = DEFAULT_TZ) -> Event:
    """Собирает событие: смысл из ответа модели (data), значения из текста."""
    text = normalize_text(text)
    ctx_list = [normalize_text(c) for c in (context or []) if isinstance(c, str)][-3:]
    ctx = " ".join(ctx_list)
    full = (ctx + " " + text).strip()
    llm = Event.model_validate({k: v for k, v in (data or {}).items() if k in Event.model_fields})
    ev = Event(intent=decide_intent(llm.intent, text, ctx))
    if ev.intent is None:
        return ev
    t = low(text)
    ev.summary = clean_summary(llm.summary) or DEFAULT_SUMMARY[ev.intent]

    if ev.intent in ("create", "move"):
        ev.in_minutes = find_offset(text)
    if ev.intent == "move":
        ev.shift_minutes = find_shift(text)

    # время берем только из текста: модель часто придумывает его
    own = find_times(text)
    bare = find_bare_hours(text)
    new_times = [x[1] for x in own if not x[2]]
    new_bare = [x[1] for x in bare if not x[2]]
    if ev.in_minutes or (ev.shift_minutes and not new_times):
        pass
    elif new_times:
        ev.time = new_times[-1] if ev.intent == "move" else new_times[0]
        if len(new_times) >= 2 and re.search(r"\bдо\b|\d\s*[-–—]\s*\d", t) and new_times[1] != new_times[0]:
            ev.end_time = new_times[1]
    elif new_bare:
        h = new_bare[-1] if ev.intent == "move" else new_bare[0]
        ev.time, ev.time_ambiguous = guess_hour(h, full), h <= 12
    elif own:
        ev.time = own[0][1]
    elif ctx_list:
        latest_time_ctx = next((c for c in reversed(ctx_list) if any(not x[2] for x in find_times(c)) or any(not x[2] for x in find_bare_hours(c))), "")
        ctx_times = [x[1] for x in find_times(latest_time_ctx) if not x[2]]
        ctx_bare = [x[1] for x in find_bare_hours(latest_time_ctx) if not x[2]]
        if ctx_times:
            ev.time = ctx_times[0]
        elif ctx_bare:
            ev.time, ev.time_ambiguous = guess_hour(ctx_bare[0], full), ctx_bare[0] <= 12

    if len(new_times) == 1 and re.search(r"\bдо\s+\d{1,2}[:.]\d{2}", t) and not re.search(r"\bс\s+\d", t):
        ev.end_time, ev.time = new_times[0], None
        ev.time_ambiguous = False
    options = list(dict.fromkeys(new_times))
    if len(options) > 1 and re.search(r"\bили\b", t):
        ev.time_options = options
        ev.time_ambiguous = True
        ev.end_time = None

    ev.recurrence, ev.rrule = find_recurrence(text)
    if not ev.recurrence and not (own or bare or find_date(text)):
        ev.recurrence, ev.rrule = find_recurrence(ctx)
    if not ev.in_minutes:
        own_date = find_date(text)
        if ev.intent == "move":
            action = re.search(r"\b(?:перенос\w*|перенес\w*|сдвин\w*|сдвиг\w*|подвин\w*|отлож\w*)\b", t)
            if action:
                destination = re.split(r"[,;!?]|\b(?:потому\s+что|так\s+как|из-за)\b", t[action.end():], maxsplit=1)[0]
                own_date = find_date(destination)
        ev.date = own_date or next((d for c in reversed(ctx_list) if (d := find_date(c))), None)
        if ev.recurrence and ev.date and ev.date.startswith(("в ", "во ")) and "следующ" not in ev.date:
            ev.date = None   # "каждую неделю в пн": день уже в RRULE
    ev.series = ev.intent in ("move", "cancel") and bool(RE_SERIES.search(t) or ev.recurrence)

    if ev.intent == "create":
        ev.duration_minutes = find_duration(text)
    if ev.time and ev.end_time:
        a = int(ev.time[:2]) * 60 + int(ev.time[3:])
        b = int(ev.end_time[:2]) * 60 + int(ev.end_time[3:])
        ev.duration_minutes = (b - a) % (24 * 60)

    loc = llm.location
    if loc and low(loc)[:4] in t and not find_date(loc) and not re.search(r"\d", loc) \
            and low(loc)[:4] != low(ev.summary)[:4]:
        ev.location = loc
    words = re.findall(r"[а-я]+", t)
    ev.attendees = [a for a in llm.attendees
                    if a[:1].isupper() and any(w.startswith(low(a)[:max(2, len(a) - 1)]) for w in words)]
    ev.timezone = find_timezone(text)

    # нет ни даты, ни времени, ни договоренности: модель ошиблась ("фильм идет 2 часа")
    if ev.intent == "create" and not (ev.time or ev.date or ev.recurrence or ev.in_minutes) \
            and not (RE_AGREE.search(t) or context_agrees(ctx)):
        return Event()
    fill_datetimes(ev, now, tz)
    return ev


def fill_datetimes(ev: Event, now: Optional[datetime], tz: str) -> None:
    """Заполняет starts_at и ends_at по дате, времени и поясу."""
    zone = get_zone(ev.timezone) or get_zone(tz) or ZoneInfo(DEFAULT_TZ)
    now = (now or datetime.now(zone)).astimezone(zone)
    ev.starts_at = ev.ends_at = None
    if ev.time_ambiguous or ev.time_options:
        return
    if ev.in_minutes:
        start = now + timedelta(minutes=ev.in_minutes)
    elif ev.time:
        day = resolve_date(ev.date, now) if ev.date else None
        if ev.date and day is None:
            return
        day = day or now
        start = datetime(day.year, day.month, day.day, int(ev.time[:2]), int(ev.time[3:]), tzinfo=zone)
        byday = re.search(r"BYDAY=([A-Z]{2}(?:,[A-Z]{2})*)", ev.rrule or "")
        if byday and not ev.date:
            candidates = []
            for code in byday.group(1).split(","):
                candidate = start + timedelta(days=(BYDAY.index(code) - start.weekday()) % 7)
                if candidate <= now:
                    candidate += timedelta(days=7)
                candidates.append(candidate)
            start = min(candidates)
        elif not ev.date and start <= now:
            start += timedelta(days=1)   # "в 10:00", а уже полдень - значит завтра
    else:
        return
    ev.starts_at = start.isoformat()
    if ev.duration_minutes:
        ev.ends_at = (start + timedelta(minutes=ev.duration_minutes)).isoformat()


def missing_fields(ev: Event) -> List[str]:
    if ev.intent != "create":
        return []
    miss = []
    if not (ev.date or ev.recurrence or ev.in_minutes):
        miss.append("date")
    if not (ev.time or ev.in_minutes):
        miss.append("time")
    return miss


def clarify_question(ev: Event) -> Optional[str]:
    """Вопрос пользователю, если не хватает даты/времени или время неоднозначно (F-B-22)."""
    if ev.intent != "create":
        return None
    if ev.time_options:
        return "Какое время выбрать: " + " или ".join(ev.time_options) + "?"
    if ev.time_ambiguous and ev.time:
        n = int(ev.time[:2]) % 12
        return "Уточните время: %02d:00 или %02d:00?" % (n, n + 12)
    miss = missing_fields(ev)
    if miss == ["date"]:
        return "На какой день назначить? Например: завтра, в пятницу."
    if "time" in miss and ev.end_time:
        return "Во сколько начинается событие? Окончание — %s." % ev.end_time
    if miss == ["time"]:
        return "Во сколько? Например: в 15:00."
    if miss:
        return "Когда? Укажите день и время, например: завтра в 15:00."
    return None


def merge_clarification(ev: Event, reply: str, now: Optional[datetime] = None, tz: str = DEFAULT_TZ) -> Event:
    """Добавляет параметры ответа, сохраняя явные альтернативы и окончание."""
    ev = ev.model_copy(deep=True)
    reply = normalize_text(reply)
    t = low(reply).strip(" .!")
    times = list(dict.fromkeys(x[1] for x in find_times(reply) if not x[2]))
    bare = find_bare_hours(reply)
    if not bare and (ev.time_options or ev.time_ambiguous) and re.fullmatch(r"\d{1,2}", t) and int(t) < 24:
        bare = [(0, int(t), False)]
    end_only = len(times) == 1 and re.search(r"\bдо\s+\d{1,2}[:.]\d{2}", t) and not re.search(r"\bс\s+\d", t)
    if end_only:
        ev.end_time = times[0]
    elif len(times) > 1 and "или" in t:
        ev.time_options, ev.time_ambiguous = times, True
    elif times:
        ev.time, ev.time_ambiguous, ev.time_options = times[0], False, []
        if len(times) == 2 and re.search(r"\bдо\b", t):
            ev.end_time = times[1]
    elif bare:
        hour = bare[0][1]
        candidate = "%02d:00" % hour
        if ev.time_options:
            if candidate in ev.time_options:
                ev.time, ev.time_ambiguous, ev.time_options = candidate, False, []
        elif ev.time_ambiguous and ev.time and hour % 12 == int(ev.time[:2]) % 12:
            ev.time, ev.time_ambiguous = candidate, False
        elif ev.time is None:
            ev.time, ev.time_ambiguous = guess_hour(hour, reply), hour <= 12
    periods = {"утром": "утра", "утра": "утра", "днем": "дня", "дня": "дня", "вечером": "вечера", "вечера": "вечера", "ночью": "ночи", "ночи": "ночи"}
    if t in periods and ev.time_ambiguous and ev.time and not ev.time_options:
        hour = apply_period(int(ev.time[:2]) % 12 or 12, periods[t])
        ev.time, ev.time_ambiguous = "%02d:%s" % (hour, ev.time[3:]), False
    ev.date = find_date(reply) or ev.date
    if ev.time and ev.end_time:
        start = int(ev.time[:2]) * 60 + int(ev.time[3:])
        end = int(ev.end_time[:2]) * 60 + int(ev.end_time[3:])
        ev.duration_minutes = (end - start) % (24 * 60) or None
    fill_datetimes(ev, now, tz)
    return ev


# ---------------------------------------------------------------- модель

# Модель отвечает только на вопрос "что это за сообщение", без даты и времени:
# так ответ короче в 2 раза, а постоянную часть промпта можно посчитать один раз.
PROMPT_HEAD = """Ты - NLP-модуль ассистента ДЕЛУ.ВРЕМЯ. Определи, есть ли в тексте договоренность о встрече или деле на будущее, и верни один JSON.
Поля: intent (create | move | cancel | null), summary (тема, 1-3 слова), location (место, если названо), attendees (имена людей в начальной форме, если названы).
create - новая встреча или дело; move - перенос или опоздание на уже назначенное; cancel - отмена, "не приду".
Нет планов на будущее (болтовня, вопрос, рассказ о прошлом, справка о расписании) - верни {"intent": null}.
Дату и время не пиши. Поля без значения не пиши. Если даны предыдущие сообщения, учитывай их как контекст.
Текст: "Привет давай в 15:00 пересечемся в кафе посидим"
Ответ: {"intent": "create", "summary": "Посидеть в кафе", "location": "кафе"}
Текст: "завтра в 10:30 планерка в офисе"
Ответ: {"intent": "create", "summary": "Планерка", "location": "офис"}
Текст: "скинь мне файл с отчетом"
Ответ: {"intent": null}
Текст: "в четверг в 20:00 боулинг с Игорем и Мариной"
Ответ: {"intent": "create", "summary": "Боулинг", "attendees": ["Игорь", "Марина"]}
Текст: "теперь созваниваемся по средам с 19:00 до 20:30"
Ответ: {"intent": "create", "summary": "Созвон"}
Текст: "я вчера был в магазине, купил хлеб"
Ответ: {"intent": null}
Предыдущие сообщения: "давай в субботу встретимся" | "во сколько тебе удобно?"
Текст: "в 15:00 у метро"
Ответ: {"intent": "create", "summary": "Встреча", "location": "у метро"}
Текст: "через 2 часа созвонимся"
Ответ: {"intent": "create", "summary": "Созвон"}
Текст: "ну как дела, что делаешь"
Ответ: {"intent": null}
Текст: "задержусь, буду на полчаса позже"
Ответ: {"intent": "move", "summary": "Опоздание"}
Текст: "перенеси встречу на 20 минут раньше"
Ответ: {"intent": "move", "summary": "Перенос встречи"}
Текст: "аптека у дома работает до 21:00"
Ответ: {"intent": null}
Текст: "сегодня не получится прийти на тренировку"
Ответ: {"intent": "cancel", "summary": "Тренировка"}
Текст: "ужин с Ирой в субботу в 19:00"
Ответ: {"intent": "create", "summary": "Ужин", "attendees": ["Ира"]}
"""

_model = None
_tok = None
_prefix_ids = None
_prefix_cache = None
_load_lock = threading.Lock()
_gen_lock = threading.Lock()


def load_model():
    global _model, _tok, _prefix_ids, _prefix_cache
    with _load_lock:
        if _model is None:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
            cuda = torch.cuda.is_available()
            _tok = AutoTokenizer.from_pretrained(MODEL_NAME)
            _model = AutoModelForCausalLM.from_pretrained(
                MODEL_NAME, dtype=torch.float16 if cuda else torch.float32,
                device_map="auto" if cuda else None).eval()
            _prefix_ids = _tok(PROMPT_HEAD, return_tensors="pt").input_ids.to(_model.device)
            with torch.no_grad():
                _prefix_cache = _model(_prefix_ids, use_cache=True).past_key_values
    return _model, _tok


def clean_for_prompt(s: str, limit: int) -> str:
    # убираем переводы строк и кавычки, чтобы текст из чата не мог дописать свой "Ответ:"
    return re.sub(r"[\x00-\x1f\x7f\u2028\u2029]+", " ", s).replace('"', "'").strip()[:limit]


def build_tail(text: str, context: Optional[List[str]] = None) -> str:
    ctx = [clean_for_prompt(c, MAX_CTX_LEN) for c in (context or []) if isinstance(c, str) and c.strip()][-3:]
    head = ("Предыдущие сообщения: " + " | ".join('"%s"' % c for c in ctx) + "\n") if ctx else ""
    return head + 'Текст: "%s"\nОтвет:' % clean_for_prompt(text, MAX_TEXT_LEN)


def build_prompt(text: str, context: Optional[List[str]] = None, now: Optional[datetime] = None) -> str:
    return PROMPT_HEAD + build_tail(text, context)


def _generate(text: str, context: Optional[List[str]] = None, now: Optional[datetime] = None) -> str:
    import torch
    model, tok = load_model()
    tail = tok(build_tail(text, context), return_tensors="pt", add_special_tokens=False).input_ids
    ids = torch.cat([_prefix_ids, tail.to(model.device)], dim=1)
    with _gen_lock, torch.no_grad():
        try:
            out = model.generate(input_ids=ids, attention_mask=torch.ones_like(ids), past_key_values=_prefix_cache,
                                 max_new_tokens=MAX_NEW_TOKENS, do_sample=False, pad_token_id=tok.eos_token_id,
                                 stop_strings=["}"], tokenizer=tok)
        finally:
            extra = _prefix_cache.get_seq_length() - _prefix_ids.shape[1]
            if extra > 0:
                _prefix_cache.crop(-extra)   # возвращаем кэш к постоянной части промпта
    return tok.decode(out[0, ids.shape[1]:], skip_special_tokens=True)


def parse_json(raw: str) -> Optional[dict]:
    """Первый JSON из ответа модели. Если JSON битый, поля достаются регулярками."""
    start = raw.find("{")
    if start < 0:
        return None
    end = raw.find("}", start)
    chunk = raw[start:end + 1] if end >= 0 else raw[start:]
    for cand in (chunk, re.sub(r",\s*}", "}", chunk), chunk.replace("'", '"'), chunk + "}", chunk + "]}"):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    m = re.search(r'"intent"\s*:\s*(?:"(\w+)"|null)', raw)
    if not m:
        return None
    obj = {"intent": m.group(1)}
    for key in ("summary", "location"):
        mm = re.search(r'"%s"\s*:\s*"([^"]*)"' % key, raw)
        if mm:
            obj[key] = mm.group(1)
    mm = re.search(r'"attendees"\s*:\s*\[([^\]]*)', raw)
    if mm:
        obj["attendees"] = re.findall(r'"([^"]+)"', mm.group(1))
    return obj


RE_PLACE = re.compile(r"(?<=\s)(?:в|во|на)\s+([А-ЯЁA-Z][а-яёa-z-]+(?:\s+[А-ЯЁA-Z][а-яёa-z-]+)?)")


def find_place(raw_text: str) -> Optional[str]:
    """Название заведения с большой буквы: "ужин в Кофемании"."""
    for m in RE_PLACE.finditer(raw_text):
        name = m.group(1)
        if not find_date("в " + name) and not re.match(r"(?:%s)" % MONTHS_RE, low(name)):
            return name
    return None


def extract_entities(text: str, context: Optional[List[str]] = None,
                     message_time: Optional[datetime] = None, tz: str = DEFAULT_TZ) -> dict:
    """Разбор одного сообщения.

    context - до 3 предыдущих сообщений чата, message_time - время получения,
    tz - users.timezone. status: ok, needs_clarification, no_event, error.
    """
    if _model is None:
        load_model()   # загрузка один раз при старте бота, в задержку сообщения не входит
    t0 = _time.perf_counter()
    raw_text = (text or "")[:MAX_TEXT_LEN]
    text = normalize_text(raw_text)
    context = [normalize_text(c[:MAX_CTX_LEN]) for c in (context or []) if isinstance(c, str)][-3:]
    zone = get_zone(tz) or ZoneInfo(DEFAULT_TZ)
    now = (message_time or datetime.now(zone)).astimezone(zone)
    res = {"status": "error", "event": None, "missing": [], "clarify": None, "error": None, "tz": tz}
    try:
        data = parse_json(_generate(text, context, now))
        if data is None:
            res["error"] = "bad_json"
        else:
            ev = postprocess(text, data, context, now, tz)
            if ev.intent == "create" and not ev.location:
                ev.location = find_place(raw_text)
            miss = missing_fields(ev)
            if ev.intent is None:
                status = "no_event"
            elif ev.intent == "create" and (miss or ev.time_ambiguous):
                status = "needs_clarification"
            else:
                status = "ok"
            res.update(status=status, event=ev.model_dump() if ev.intent else None,
                       missing=miss, clarify=clarify_question(ev))
    except Exception as e:
        log.exception("nlp failed")
        res["error"] = type(e).__name__
    res["latency_s"] = round(_time.perf_counter() - t0, 3)
    # текст сообщения в лог не пишем (хранение переписки ограничено 30 днями)
    log.info("nlp status=%s intent=%s latency=%.2fs", res["status"],
             (res["event"] or {}).get("intent"), res["latency_s"])
    return res


def to_event_record(result: dict, *, user_id: str, chat_id: Optional[str] = None,
                    source_message_id: Optional[int] = None, calendar_connection_id: Optional[str] = None,
                    user_tz: Optional[str] = None) -> Optional[dict]:
    """Строка для таблицы events со статусом draft (до кнопки "Подтвердить").

    Для move и cancel возвращает None: это записи в operations для существующего события.
    """
    ev = result.get("event")
    if not ev or ev.get("intent") != "create":
        return None
    return {
        "user_id": user_id,
        "chat_id": chat_id,
        "calendar_connection_id": calendar_connection_id,
        "source_message_id": source_message_id,
        "title": ev["summary"],
        "location": ev["location"],
        "starts_at": ev["starts_at"],
        "ends_at": ev["ends_at"],
        "duration": timedelta(minutes=ev["duration_minutes"]) if ev["duration_minutes"] else None,
        "timezone": ev["timezone"] or user_tz or result.get("tz") or DEFAULT_TZ,
        "rrule": ev["rrule"],
        "participants": [{"name": n} for n in ev["attendees"]],
        "status": "draft",
        "confirmed_at": None,
        "google_event_id": None,
    }


# ---------------------------------------------------------------- проверка правил

def selftest() -> None:
    now = datetime(2026, 11, 4, 12, 0, tzinfo=ZoneInfo(DEFAULT_TZ))   # среда

    def run(text, data=None, ctx=None):
        return postprocess(text, data or {"intent": "create", "summary": "Встреча"}, ctx, now)

    cases = [
        ("завтра в 15:00 в кафе", {"intent": "create", "summary": "Кофе", "location": "кафе"},
         dict(date="завтра", time="15:00", location="кафе", starts_at="2026-11-05T15:00:00+03:00")),
        ("седня в15:00", None, dict(date="сегодня", time="15:00")),
        ("в суботу в 18 30 кино", None, dict(date="в субботу", starts_at="2026-11-07T18:30:00+03:00")),
        ("в следующую пятницу в 7 вечера", None, dict(starts_at="2026-11-13T19:00:00+03:00")),
        ("пятого числа в 10:00", None, dict(starts_at="2026-11-05T10:00:00+03:00")),
        ("25 ноября в полдень", None, dict(starts_at="2026-11-25T12:00:00+03:00")),
        ("в 7.30 вечера", None, dict(time="19:30")),
        ("через полчасика созвон", {"intent": None}, dict(intent="create", in_minutes=30)),
        ("по средам с 19:00 до 20:30 созвон", None,
         dict(rrule="FREQ=WEEKLY;BYDAY=WE", end_time="20:30", duration_minutes=90,
              starts_at="2026-11-04T19:00:00+03:00")),
        ("каждую неделю в пн с 16 до 17", None,
         dict(rrule="FREQ=WEEKLY;BYDAY=MO", time="16:00", end_time="17:00", date=None)),
        ("по утрам в 8 пробежка", None, dict(rrule="FREQ=DAILY")),
        ("раз в две недели по пятницам", None, dict(rrule="FREQ=WEEKLY;INTERVAL=2;BYDAY=FR")),
        ("давай в 3", None, dict(time="15:00", time_ambiguous=True)),
        ("ужин в 8", None, dict(time="20:00")),
        ("в 15:00 по екб", None, dict(timezone="Asia/Yekaterinburg", starts_at="2026-11-04T15:00:00+05:00")),
        ("сорян, сегодня не получится", {"intent": None}, dict(intent="cancel")),
        ("отмени все тренировки по средам", {"intent": "cancel"}, dict(intent="cancel", series=True)),
        ("завтра в половине третьего стоматолог", None, dict(time="14:30")),
        ("сорян, сегодня никак", {"intent": None}, dict(intent="cancel")),
        ("спасибо, до встречи!", {"intent": "create"}, dict(intent=None)),
        ("кто будет на созвоне?", {"intent": "create"}, dict(intent=None)),
        ("в субботу с 12 до 14 уборка", {"intent": "move"}, dict(intent="create", time="12:00", end_time="14:00")),
        ("не в 10, а в 11", {"intent": "move"}, dict(intent="move", time="11:00")),
        ("давай не в 7, а в 8 вечера", {"intent": "create"}, dict(intent="move", time="20:00")),
        ("опоздаю минут на 15", {"intent": "move"}, dict(intent="move", shift_minutes=15)),
        ("перенеси встречу на час позже", {"intent": "move"}, dict(shift_minutes=60, time=None)),
        ("вчера в 15:00 был созвон", None, dict(intent=None)),
        ("поезд прибыл в 14:20", None, dict(intent=None)),
        ("магазин открывается в 9:00", None, dict(intent=None)),
        ("не надо ничего отменять", {"intent": "cancel"}, dict(intent=None)),
        ("давай завтра брат", {"intent": "create", "summary": "Завтра", "attendees": ["Дима"]},
         dict(summary="Встреча", time=None, attendees=[])),
        ("ужин в субботу", {"intent": "create", "summary": "Ужин в субботу"}, dict(summary="Ужин")),
        ("встреча с Петей в 18:00", {"intent": "create", "summary": "Встреча", "attendees": ["Петя"]},
         dict(attendees=["Петя"], time="18:00")),
        ("в 10 классе было 30 человек", None, dict(intent=None)),
        ("кинь 1.5 кг сахара", None, dict(intent=None)),
    ]
    assert find_place("в четверг в 19:30 ужин с Олей в Кофемании") == "Кофемании"
    assert find_place("В пятницу в 18:00 кино") is None
    assert run("окей", {"intent": "create"}, ["перенесем на пятницу?"]).intent == "move"
    assert run("в 18:00", {"intent": None}, ["в четверг сходим в кино"]).intent == "create"
    for text, data, expected in cases:
        ev = run(text, data)
        got = {k: getattr(ev, k) for k in expected}
        assert got == expected, "%r: ждали %s, получили %s" % (text, expected, got)
    ev = run("в 15:00", ctx=["давай в субботу встретимся"])
    assert (ev.intent, ev.date, ev.time) == ("create", "в субботу", "15:00"), ev
    ev = run("ок", {"intent": None}, ["го завтра в 19:00 в кино?"])
    assert (ev.intent, ev.date, ev.time) == ("create", "завтра", "19:00"), ev
    ev = run("окей", {"intent": None}, ["перенесем на пятницу?"])
    assert (ev.intent, ev.date) == ("move", "в пятницу"), ev
    assert run("в 15:00", {"intent": "create"}, ["ну как дела?"]).intent is None
    ev = merge_clarification(run("давай встретимся"), "завтра в 18:00", now)
    assert ev.starts_at == "2026-11-05T18:00:00+03:00", ev
    ev = merge_clarification(run("завтра давай в 3"), "в 15", now)
    assert (ev.time, ev.time_ambiguous) == ("15:00", False), ev
    assert parse_json('{"intent": "move", "summary": "Перенос", "recurrence": "по }",') == \
        {"intent": "move", "summary": "Перенос"}
    print("selftest: OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("text", nargs="*")
    args, _ = ap.parse_known_args()
    if args.selftest:
        selftest()
    if args.text:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        print(json.dumps(extract_entities(" ".join(args.text)), ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- диалог и проверка результата

class DialogueState:
    """Черновик одного пользователя в одном чате; не разделяется между диалогами."""

    def __init__(self, timeout_seconds: int = 1800):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds должен быть положительным")
        self.timeout_seconds = timeout_seconds
        self.reset()

    def reset(self) -> None:
        self.event = None
        self.history = []
        self.target_event_id = None
        self.reference_time = None
        self.last_message_time = None


def dialogue_question(ev: Event, target_event_id=None) -> Tuple[List[str], Optional[str]]:
    """Проверяет черновик без обращения к БД или календарю."""
    if ev.intent is None:
        return [], None
    if ev.time_options:
        return ["time"], "Какое время выбрать: " + " или ".join(ev.time_options) + "?"
    if ev.time_ambiguous:
        probe = ev.model_copy(update={"intent": "create"}, deep=True)
        return ["time"], clarify_question(probe)
    if ev.intent in ("move", "cancel"):
        if target_event_id is None:
            return ["target_event_id"], "Какую встречу вы имеете в виду? Выберите событие из календаря."
        if ev.intent == "move" and not (
            ev.date or ev.time or ev.in_minutes or ev.shift_minutes
        ):
            return ["destination"], "На какой день или время перенести встречу?"
        return [], None
    missing = missing_fields(ev)
    return missing, clarify_question(ev)


def dialogue_result(ev: Optional[Event], *, tz: str, now: datetime,
                    target_event_id=None) -> dict:
    if ev is None or ev.intent is None:
        return {"status": "no_event", "event": None, "missing": [],
                "clarify": None, "error": None, "tz": tz}
    missing, question = dialogue_question(ev, target_event_id)
    if ev.date and resolve_date(ev.date, now) is None:
        missing = list(dict.fromkeys(missing + ["date"]))
        question = "Не удалось определить дату. Укажите существующую календарную дату."
    if ev.intent == "create" and not missing and not ev.starts_at:
        missing = ["starts_at"]
        question = "Уточните дату, время и часовой пояс начала события."
    return {"status": "needs_clarification" if missing else "ok",
            "event": ev.model_dump(), "missing": missing,
            "clarify": question, "error": None, "tz": tz,
            "target_event_id": target_event_id}


def process_dialogue_message(text: str, state: DialogueState, *,
                             message_time: Optional[datetime] = None,
                             tz: str = DEFAULT_TZ, target_event_id=None,
                             new_task: bool = False, extractor=None) -> dict:
    """Обрабатывает реплику и обновляет черновик. Запись во внешние системы не выполняется.

    target_event_id передаёт приложение после проверки владельца и выбора события.
    После подтверждения или отказа приложение вызывает state.reset().
    extractor можно подменить в тестах; по умолчанию используется extract_entities.
    """
    started = _time.perf_counter()
    zone = get_zone(tz)
    if zone is None:
        raise ValueError("Неизвестный часовой пояс")
    now = message_time or datetime.now(zone)
    if now.tzinfo is None:
        raise ValueError("message_time должен содержать часовой пояс")
    now = now.astimezone(zone)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Ожидалось непустое сообщение")
    if len(text) > MAX_TEXT_LEN:
        raise ValueError("Сообщение превышает MAX_TEXT_LEN")
    if state.last_message_time and now < state.last_message_time:
        raise ValueError("Сообщения должны обрабатываться по порядку")
    expired = state.last_message_time and (
        now - state.last_message_time
    ).total_seconds() > state.timeout_seconds
    explicit_task = bool(re.search(
        r"^\s*(?:(?:пожалуйста|теперь|а\s+теперь)\s*[, :]?\s*)?"
        r"(?:создай|запиши|запланируй|напомни|перенеси|отмени)\b",
        low(text),
    ))
    if expired or new_task or explicit_task:
        state.reset()
    normalized = normalize_text(text)
    if low(normalized).strip(" .!?") in ("не надо", "отбой", "забудь", "отмена") and state.event:
        state.reset()
        return {"status": "no_event", "event": None, "missing": [],
                "clarify": None, "error": None, "tz": tz, "discarded": True}
    reference = state.reference_time or now
    parser = extractor or extract_entities
    result = parser(text, context=list(state.history), message_time=reference, tz=tz)
    if result.get("status") == "error":
        return result
    incoming = Event.model_validate(result["event"]) if result.get("event") else None
    if state.event is None:
        ev = incoming
    else:
        ev = merge_clarification(state.event, text, reference, tz)
        offset = find_offset(normalized)
        if offset:
            ev.in_minutes = offset
            ev.date = ev.time = ev.end_time = None
            ev.time_options, ev.time_ambiguous = [], False
        elif find_date(normalized) or find_times(normalized) or find_bare_hours(normalized):
            ev.in_minutes = None
        duration = find_duration(normalized)
        if duration:
            ev.duration_minutes = duration
        explicit_zone = find_timezone(normalized)
        if explicit_zone:
            ev.timezone = explicit_zone
        recurrence, rule = find_recurrence(normalized)
        if recurrence:
            ev.recurrence, ev.rrule = recurrence, rule
        if ev.intent == "move":
            shift = find_shift(normalized)
            if shift:
                ev.shift_minutes = shift
                ev.in_minutes = None
                ev.date = ev.time = None
            elif find_date(normalized) or find_times(normalized) or find_bare_hours(normalized):
                ev.shift_minutes = None
        if incoming:
            if incoming.location:
                ev.location = incoming.location
            if incoming.attendees:
                ev.attendees = list(dict.fromkeys(ev.attendees + incoming.attendees))
            if incoming.summary and ev.summary in (None, *DEFAULT_SUMMARY.values()):
                ev.summary = incoming.summary
        place = find_place(text)
        if place:
            ev.location = place
        fill_datetimes(ev, reference, tz)
    state.history = (state.history + [text[:MAX_CTX_LEN]])[-3:]
    state.last_message_time = now
    if ev is not None and ev.intent:
        state.event = ev
        state.reference_time = reference
    if target_event_id is not None:
        if ev is None or ev.intent not in ("move", "cancel"):
            raise ValueError("Выбор существующего события применим только к move/cancel")
        state.target_event_id = target_event_id
    output = dialogue_result(ev, tz=tz, now=reference,
                             target_event_id=state.target_event_id)
    output["latency_s"] = round(_time.perf_counter() - started, 3)
    return output


def to_ready_event_record(result: dict, *, required_columns=(),
                          column_mapping=None, **record_arguments) -> Optional[dict]:
    """Готовит только завершённое создание; схема задаётся вызывающим кодом.

    column_mapping: имя поля существующего to_event_record -> имя колонки БД.
    При передаче mapping возвращаются только явно перечисленные поля.
    Типы, внешние ключи и права проверяются ORM/сервисом перед записью.
    """
    if result.get("status") != "ok":
        return None
    raw = result.get("event") or {}
    if raw.get("intent") != "create":
        return None
    ev = Event.model_validate(raw)
    if missing_fields(ev) or ev.time_ambiguous or ev.time_options or not ev.starts_at:
        return None
    start = datetime.fromisoformat(ev.starts_at)
    if start.tzinfo is None:
        raise ValueError("starts_at должен содержать часовой пояс")
    if ev.ends_at:
        end = datetime.fromisoformat(ev.ends_at)
        if end.tzinfo is None or end <= start:
            raise ValueError("Некорректное окончание события")
    record = to_event_record(result, **record_arguments)
    if column_mapping is not None:
        if len(set(column_mapping.values())) != len(column_mapping):
            raise ValueError("Несколько полей сопоставлены одной колонке")
        unknown = set(column_mapping) - set(record)
        if unknown:
            raise ValueError("Неизвестные поля: " + ", ".join(sorted(unknown)))
        record = {target: record[source] for source, target in column_mapping.items()}
    absent = [name for name in required_columns if record.get(name) is None]
    if absent:
        raise ValueError("Не заполнены обязательные колонки: " + ", ".join(absent))
    return record


def to_operation_payload(result: dict) -> Optional[dict]:
    """Нейтральный payload для сервиса операций, не готовая строка неизвестной БД."""
    ev = result.get("event") or {}
    if result.get("status") != "ok" or ev.get("intent") not in ("move", "cancel"):
        return None
    target = result.get("target_event_id")
    if target is None:
        return None
    validated = Event.model_validate(ev)
    missing, _ = dialogue_question(validated, target)
    if missing:
        return None
    changes = {}
    if validated.intent == "move":
        keys = ("date", "time", "in_minutes", "shift_minutes", "timezone", "rrule")
        changes = {key: ev[key] for key in keys if ev.get(key) is not None}
    return {"intent": validated.intent, "target_event_id": target, "changes": changes}


def evaluate_nlp_cases(cases, runner=None) -> dict:
    """Оценка размеченных отдельных сообщений и диалогов без записи в БД.

    Кейс: {id, text, context, expected}; либо {id, turns, expected}.
    В turns: text, message_time, new_task, target_event_id (как аргументы обработчика).
    expected: status и/или intent и/или fields (поля Event).
    Полное совпадение означает совпадение всех явно размеченных значений.
    """
    parser = runner or extract_entities
    records = []
    tp = fp = tn = fn = errors = intent_correct = intent_total = 0
    field_scores = {}
    for index, case in enumerate(cases):
        expected = case.get("expected", {})
        if (not expected or not set(expected).issubset({"status", "intent", "fields"})
                or not ("status" in expected or "intent" in expected or expected.get("fields"))):
            raise ValueError("Нужна разметка expected: status, intent, fields")
        if "fields" in expected and not set(expected["fields"]).issubset(Event.model_fields):
            raise ValueError("Неизвестное поле в expected.fields")
        try:
            if "turns" in case:
                if not case["turns"]:
                    raise ValueError("Пустой диалог")
                state = DialogueState()
                for turn in case["turns"]:
                    result = process_dialogue_message(state=state, extractor=parser, **turn)
            else:
                result = parser(case["text"], context=case.get("context"),
                                message_time=case.get("message_time"),
                                tz=case.get("tz", DEFAULT_TZ))
        except Exception as error:
            result = {"status": "error", "event": None, "error": type(error).__name__}
        failed = result.get("status") == "error"
        errors += int(failed)
        actual_event = result.get("event") or {}
        actual_intent = actual_event.get("intent")
        differences = {}
        if "intent" in expected:
            wanted = expected["intent"]
            intent_total += 1
            intent_correct += int(not failed and wanted == actual_intent)
            if wanted is not None:
                if actual_intent is not None and not failed:
                    tp += 1
                else:
                    fn += 1
            elif actual_intent is not None and not failed:
                fp += 1
            elif not failed:
                tn += 1
            if wanted != actual_intent or failed:
                differences["intent"] = {"expected": wanted, "actual": actual_intent}
        if "status" in expected and expected["status"] != result.get("status"):
            differences["status"] = {"expected": expected["status"], "actual": result.get("status")}
        for name, value in expected.get("fields", {}).items():
            correct = not failed and actual_event.get(name) == value
            counts = field_scores.setdefault(name, {"correct": 0, "total": 0})
            counts["total"] += 1
            counts["correct"] += int(correct)
            if not correct:
                differences[name] = {"expected": value, "actual": actual_event.get(name)}
        records.append({"id": case.get("id", index), "passed": not failed and not differences,
                        "differences": differences, "result": result})
    total = len(records)
    ratio = lambda a, b: a / b if b else None
    return {"examples": total, "errors": errors,
            "intent_accuracy": ratio(intent_correct, intent_total),
            "joint_accuracy": ratio(sum(r["passed"] for r in records), total),
            "detection": {"tp": tp, "fp": fp, "tn": tn, "fn": fn,
                          "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn)},
            "fields": {k: dict(v, accuracy=ratio(v["correct"], v["total"]))
                       for k, v in field_scores.items()}, "records": records}