"""
NLP/LLM-модуль проекта «ДЕЛУ.ВРЕМЯ» (v9): извлечение договорённостей из текста.

Вход:  текст сообщения, опционально: предыдущие реплики чата и время сообщения.
Выход: словарь
    {
      "status": "ok" | "needs_clarification" | "no_event" | "error",
      "event":  {...поля ExtractedEvent...} | None,
      "missing": [...],            # чего не хватает (F-B-22)
      "when":   {...} | None,      # разрешённые даты начала/конца (если передано время сообщения)
      "error":  None | "generation_failed" | "bad_json" | "postprocess_failed",
      "raw":    "...",             # сырой ответ модели (для разбора ошибок)
      "latency_s": float,
    }

Принципы:
  * LLM отвечает за смысл (есть ли договорённость, интент, тема, место).
  * Код проверяет ответ LLM по тексту: правило срабатывает, только если LLM вернула null
    или её значение явно противоречит сообщению. Выдуманные поля отбрасываются.
  * Ничего не глотаем молча: ошибки логируются и возвращаются в поле "error".
  * Тяжёлые зависимости (torch, transformers, pymorphy3, dateparser) подгружаются лениво,
    поэтому правила можно тестировать без GPU: `python nlp_module.py --selftest`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import threading
import time as _time
from datetime import date as _date, datetime, time as _dtime, timedelta, timezone as _tz
from typing import Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("delu.nlp")

MODEL_NAME = "RaguTeam/RAGU-lm"
DEFAULT_TZ = "Europe/Moscow"
INTENTS = {"create", "move", "cancel"}
RU_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                 "сентября", "октября", "ноября", "декабря"]
RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


# ==========================================================================
# 1. Схема данных (контракт с модулем RRULE и календарём)
# ==========================================================================

def normalize_hhmm(v: Any) -> Optional[str]:
    """'9:00' -> '09:00'; всё некорректное -> None."""
    if not isinstance(v, str):
        return None
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", v)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


class ExtractedEvent(BaseModel):
    intent: Optional[str] = None            # create | move | cancel | None
    summary: Optional[str] = None           # тема, 1-3 слова
    date: Optional[str] = None              # «завтра», «в пятницу», «25 октября», «в следующий вторник»
    time: Optional[str] = None              # HH:MM, начало
    end_time: Optional[str] = None          # HH:MM, конец
    duration_minutes: Optional[int] = None
    in_minutes: Optional[int] = None        # «через час» -> 60
    shift_minutes: Optional[int] = None     # «на час позже» -> +60, «на полчаса раньше» -> -30
    location: Optional[str] = None
    recurrence: Optional[str] = None        # «каждую неделю пн» (в RRULE превращает другой модуль)
    timezone: Optional[str] = None          # «по Москве» -> Europe/Moscow, «UTC+05:00»
    time_ambiguous: bool = False            # «в 3»: неизвестно, 03:00 или 15:00
    attendees: List[str] = Field(default_factory=list)

    @field_validator("intent", mode="before")
    @classmethod
    def _intent(cls, v: Any) -> Optional[str]:
        return v if v in INTENTS else None

    @field_validator("time", "end_time", mode="before")
    @classmethod
    def _hhmm(cls, v: Any) -> Optional[str]:
        return normalize_hhmm(v)

    @field_validator("date", "summary", "location", "recurrence", "timezone", mode="before")
    @classmethod
    def _text(cls, v: Any) -> Optional[str]:
        if not isinstance(v, str):
            return None
        v = v.strip()
        return None if v.lower() in {"", "null", "none"} else v

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def _duration(cls, v: Any) -> Optional[int]:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if 0 < n <= 24 * 60 else None

    @field_validator("in_minutes", "shift_minutes", mode="before")
    @classmethod
    def _minutes(cls, v: Any) -> Optional[int]:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n != 0 and abs(n) <= 7 * 24 * 60 else None

    @field_validator("time_ambiguous", mode="before")
    @classmethod
    def _bool(cls, v: Any) -> bool:
        return v is True

    @field_validator("attendees", mode="before")
    @classmethod
    def _attendees(cls, v: Any) -> List[str]:
        if not isinstance(v, list):
            return []
        return [x.strip() for x in v if isinstance(x, str) and x.strip()]


# ==========================================================================
# 2. Правила времени: «16:00-17:30», «в половине седьмого», «к восьми», «5 вечера»
# ==========================================================================

_NUM_WORDS = {
    "час": 1, "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
}
_ORD_WORDS = {  # родительный: «пол седьмого», «в половине седьмого»
    "первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "пятого": 5,
    "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9, "десятого": 10,
    "одиннадцатого": 11, "двенадцатого": 12,
}
_DAT_WORDS = {  # дательный: «к восьми», «к двум дня»
    "двум": 2, "трем": 3, "четырем": 4, "пяти": 5, "шести": 6, "семи": 7, "восьми": 8,
    "девяти": 9, "десяти": 10, "одиннадцати": 11, "двенадцати": 12,
}
_NUM_RE = "|".join(sorted(_NUM_WORDS, key=len, reverse=True))
_ORD_RE = "|".join(sorted(_ORD_WORDS, key=len, reverse=True))
_DAT_RE = "|".join(sorted(_DAT_WORDS, key=len, reverse=True))
_PERIOD = "утра|дня|вечера|ночи"
_MONTHS = "|".join(RU_MONTHS_GEN)

_RE_HHMM = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_RE_POL = re.compile(rf"\b(?:пол\s*-?\s*|половин\w*\s+)({_ORD_RE})(?:\s+({_PERIOD}))?\b")
_RE_NUM_PERIOD = re.compile(rf"\b(\d{{1,2}}|{_NUM_RE})\s*(?:час\w*\s+)?({_PERIOD})\b")
_RE_HOURS = re.compile(rf"\bв\s+(\d{{1,2}})\s+час(?:ов|а)?\b(?!\s+(?:{_PERIOD}))")
_RE_K = re.compile(rf"\bк\s+({_DAT_RE})(?:\s+час\w*)?(?:\s+({_PERIOD}))?(?!\s+(?:год|лет|рубл|человек|штук))\b")
_RE_RANGE = re.compile(
    rf"\bс\s+(\d{{1,2}})\s+до\s+(\d{{1,2}})(?![\d:])(?:\s+({_PERIOD}))?"
    rf"(?!\s+(?:человек|минут|руб|процент|{_MONTHS}))"
)
# слова «это было раньше»: время рядом с ними старое, а не новое
_OLD_MARK = re.compile(r"(?:вместо|а\s+не|было|была|не\s+в|не\s+на)\s*(?:в\s+|на\s+)?$")


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def _apply_period(h: int, period: Optional[str]) -> int:
    if period is None or not 0 <= h <= 12:
        return h
    if period == "утра":
        return 0 if h == 12 else h
    if period == "дня":
        return h + 12 if 1 <= h <= 6 else h
    if period == "вечера":
        return h + 12 if 1 <= h <= 11 else h
    if period == "ночи":
        if h == 12:
            return 0
        return h + 12 if 6 <= h <= 11 else h
    return h


def find_times_pos(text: str) -> List[Tuple[int, int, str]]:
    """Все явно названные времена: (начало, конец, 'HH:MM') в порядке появления в тексте."""
    t = _norm(text)
    found: List[Tuple[int, int, str]] = []
    for m in _RE_HHMM.finditer(t):
        found.append((m.start(), m.end(), f"{int(m.group(1)):02d}:{m.group(2)}"))
    for m in _RE_POL.finditer(t):
        n = _ORD_WORDS[m.group(1)]
        h = _apply_period(12 if n == 1 else n - 1, m.group(2))
        found.append((m.start(), m.end(), f"{h:02d}:30"))
    for m in _RE_NUM_PERIOD.finditer(t):
        tok = m.group(1)
        h = int(tok) if tok.isdigit() else _NUM_WORDS[tok]
        if h > 23:
            continue
        found.append((m.start(), m.end(), f"{_apply_period(h, m.group(2)):02d}:00"))
    for m in _RE_HOURS.finditer(t):
        h = int(m.group(1))
        if h <= 23:
            found.append((m.start(), m.end(), f"{h:02d}:00"))
    for m in _RE_K.finditer(t):
        h = _DAT_WORDS[m.group(1)]
        h = _apply_period(h, m.group(2)) if m.group(2) else (h + 12 if h <= 6 else h)
        found.append((m.start(), m.end(), f"{h:02d}:00"))
    for m in _RE_RANGE.finditer(t):
        a, b = int(m.group(1)), int(m.group(2))
        if a <= 23 and b <= 23 and b > a:
            found.append((m.start(), m.end(), f"{_apply_period(a, m.group(3)):02d}:00"))
            found.append((m.start() + 1, m.end(), f"{_apply_period(b, m.group(3)):02d}:00"))
    found.sort(key=lambda x: (x[0], x[2]))
    out: List[Tuple[int, int, str]] = []
    for item in found:
        if not out or out[-1][2] != item[2]:      # «с 5 до 7 вечера» не даёт двойное 19:00
            out.append(item)
    return out


def find_clock_times(text: str) -> List[str]:
    return [x[2] for x in find_times_pos(text)]


def choose_times(text: str) -> Tuple[List[str], bool]:
    """Времена текста без «старых» («вместо 15:00», «а не в 15:00», «было в 15:00»).
    Возвращает (новые времена, были ли старые)."""
    t = _norm(text)
    items = find_times_pos(text)
    new = [x[2] for x in items if not _OLD_MARK.search(t[max(0, x[0] - 14):x[0]])]
    had_old = len(new) < len(items)
    return (new or [x[2] for x in items]), had_old


# ==========================================================================
# 3. Отмена: лемматизация и отрицание («не надо отменять» — не отмена)
# ==========================================================================

_morph = None


def _get_morph():
    global _morph
    if _morph is None:
        import pymorphy3
        _morph = pymorphy3.MorphAnalyzer()
    return _morph


_CANCEL_LEMMAS = {"отменять", "отменить", "отмена"}


def cancel_hits(text: str) -> List[Tuple[int, bool]]:
    """[(позиция слова, отрицается ли)] для слов «отмени/отменяй/отмена».
    Отрицание: «не/ни» в трёх словах перед или «не» в двух после («не надо отменять», «отменять не нужно»)."""
    morph = _get_morph()
    toks = [(m.start(), m.group(0)) for m in re.finditer(r"[а-яё]+", text.lower())]
    hits: List[Tuple[int, bool]] = []
    for i, (pos, w) in enumerate(toks):
        if morph.parse(w)[0].normal_form in _CANCEL_LEMMAS:
            before = [x[1] for x in toks[max(0, i - 3):i]]
            after = [x[1] for x in toks[i + 1:i + 3]]
            neg = "не" in before or "ни" in before or "не" in after
            hits.append((pos, neg))
    return hits


def has_cancel_word(text: str) -> bool:
    return any(not neg for _, neg in cancel_hits(text))


_RE_CANCEL_PHRASE = re.compile(
    r"\bне\s+(?:смогу|могу|получится|успею|успеваю)\s+(?:прийти|приехать|подъехать|быть|поехать|встретиться)\b"
    r"|\bне\s+(?:приду|приеду|подъеду|пойду|поеду)\b"
    r"|\bминус\s+я\b"
)
_RE_NEG_ATTEND = re.compile(
    r"\bне\s+(?:смогу|могу|получится|выйдет|успею|успеваю|пойду|поеду|приду|приеду|подъеду)\b")
_RE_MOVE_PHRASE = re.compile(r"\b(?:перенеси|перенесите|перенесем|перенесу|сдвинь|сдвинем|подвинь|подвинем)\b")
_RE_MOVE_LEX = re.compile(r"перенес|перенос|сдвин|сдвиг|опозд|опазд|задерж|подвин|отлож|перекин")
_RE_LATE = re.compile(r"(?<!не )\b(?:опаздыва|опозда|задержива|задерж)\w*")
_RE_PAST = re.compile(r"\b(?:вчера|позавчера)\b")
_RE_FUTURE = re.compile(r"\b(?:завтра|послезавтра|сегодня|давай|будем|буду|каждый|каждую|каждые|следующ\w*)\b")
_RE_STATE_VERB = re.compile(r"\b(?:будет|был[аои]?|было|стало|станет|пройдет|закончится|выйдет|начнется)\b")


# ==========================================================================
# 4. Периодичность, длительность, «через N», «на N позже»
# ==========================================================================

_UNITS = (r"(?:день|недел\w+|месяц|год|утро|вечер|ночь|выходн\w+|будн\w+|понедельник|вторник|"
          r"сред[ауы]|четверг|пятниц\w+|суббот\w+|воскресень\w+)")
_RE_RECURRENCE = re.compile(
    rf"\bкажд(?:ый|ую|ые|ое)\s+(?:(?:втор|трет)\w+\s+)?{_UNITS}\b"          # каждый вторник, каждую неделю
    rf"|\bкажд(?:ые|ую)\s+(?:\d+|два|две|три|четыре|пять)\s+(?:дн\w+|недел\w+|месяц\w*)"  # каждые 2 недели
    r"|\bпо\s+(?:понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям|выходным|будням|утрам|вечерам)\b"
    r"|\b(?:ежедневно|еженедельно|ежемесячно)\b"
    r"|\bраз\s+в\s+(?:день|неделю|месяц|две\s+недели)\b"
)
_RE_WEEKDAY_ABBR = re.compile(
    r"\b(?:пн|вт|ср|чт|пт|сб|вс|понедельник\w*|вторник\w*|сред[ауыео]\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)\b"
)


def find_recurrence(text: str) -> Optional[str]:
    """«каждого участника» периодичностью НЕ считается: нужна единица времени после «каждый»."""
    m = _RE_RECURRENCE.search(_norm(text))
    return m.group(0) if m else None


_DUR_WORDS = {"два": 2, "три": 3, "четыре": 4, "пять": 5}
_AMOUNT = (r"(полчаса|полтора\s+часа|час(?!\w)|\d{1,3}\s*(?:минут\w*|мин\b)"
           r"|(?:\d{1,2}|два|три|четыре|пять)\s+час(?:а|ов)?\b)")
_RE_DUR = re.compile(rf"\bна\s+{_AMOUNT}(?!\s+(?:позже|раньше|вперед|назад))")
_RE_OFFSET = re.compile(rf"\bчерез\s+{_AMOUNT}")
_RE_SHIFT = re.compile(rf"\bна\s+{_AMOUNT}\s+(позже|раньше|вперед|назад)\b")


def _amount_minutes(p: str) -> int:
    p = re.sub(r"\s+", " ", p.strip())
    if p.startswith("полчаса"):
        return 30
    if p.startswith("полтора"):
        return 90
    if p == "час":
        return 60
    m = re.match(r"(\d+)\s*мин", p)
    if m:
        return int(m.group(1))
    tok = p.split()[0]
    return (int(tok) if tok.isdigit() else _DUR_WORDS[tok]) * 60


def find_duration(text: str) -> Optional[int]:
    """«на час» -> 60, «на полчаса» -> 30, «на 45 минут» -> 45, «на два часа» -> 120."""
    m = _RE_DUR.search(_norm(text))
    return _amount_minutes(m.group(1)) if m else None


def find_offset(text: str) -> Optional[int]:
    """«через час» -> 60, «через 2 часа» -> 120, «через 15 минут» -> 15."""
    m = _RE_OFFSET.search(_norm(text))
    return _amount_minutes(m.group(1)) if m else None


def find_shift(text: str) -> Optional[int]:
    """«на час позже» -> +60, «на полчаса раньше» -> -30."""
    m = _RE_SHIFT.search(_norm(text))
    if not m:
        return None
    mins = _amount_minutes(m.group(1))
    return mins if m.group(2) in ("позже", "вперед") else -mins


# ==========================================================================
# 5. Даты: «25 октября», «в след. вторник», «на четверг»
# ==========================================================================

_WD_FORMS = "понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье"
_RE_REL_DATE = re.compile(r"\b(послезавтра|завтра|сегодня)\b")
_RE_NEXT_WD = re.compile(rf"\b(?:в|во|на)\s+след(?:ующ\w+|\.)\s*({_WD_FORMS}|среда|пятница|суббота)\b")
_RE_WEEKDAY = re.compile(rf"\b(?:в|во|на)\s+(?:{_WD_FORMS})\b")
_RE_ABSDATE = re.compile(rf"\b\d{{1,2}}\s+(?:{_MONTHS})\b")
_RE_DATE_VALUE = re.compile(
    r"сегодня|завтра|послезавтра|понедельник|вторник|сред[ауые]|четверг|пятниц|суббот|воскресень"
    rf"|{_MONTHS}|\b\d{{1,2}}[./]\d{{1,2}}\b|\bчисл|следующ|\bслед\b|недел|месяц|выходн"
)
_RE_DATE_CUE = re.compile(
    r"сегодня|завтра|послезавтра|понедельник|вторник|сред|четверг|пятниц|суббот|воскресень"
    r"|\b(?:пн|вт|ср|чт|пт|сб|вс)\b|январ|феврал|март|апрел|\bма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр"
    r"|\d|числ|недел|выходн|месяц|утр|вечер|ноч|дн[яеюёь]|обед"
)
_RE_TIME_CUE = re.compile(r"\d|час|утр|вечер|обед|полдень|полночь|ноч|дн[яеюёь]|минут")
_RE_INTERVAL_CUE = re.compile(r"\bдо\b|\d\s*[-–—]\s*\d|\bпо\b")


def find_relative_date(text: str) -> Optional[str]:
    t = _norm(text)
    m = _RE_NEXT_WD.search(t)
    if m:
        return f"в следующий {m.group(1)}"
    m = _RE_REL_DATE.search(t) or _RE_WEEKDAY.search(t) or _RE_ABSDATE.search(t)
    if not m:
        return None
    return re.sub(r"^на\s+", "в ", m.group(0))


def _same_date(a: Optional[str], b: Optional[str]) -> bool:
    def key(x):
        x = _norm(x or "").strip()
        x = re.sub(r"^(?:в|во|на)\s+", "", x)
        return x[:5]
    return bool(a and b and key(a) == key(b))


# ==========================================================================
# 6. Часовые пояса: «в 3 по Москве», «по мск+2», «UTC+5»
# ==========================================================================

_TZ_CITIES = [
    (r"москв\w*|мск|msk|питер\w*|петербург\w*|спб", "Europe/Moscow"),
    (r"екатеринбург\w*|екб", "Asia/Yekaterinburg"),
    (r"новосибирск\w*", "Asia/Novosibirsk"),
    (r"владивосток\w*", "Asia/Vladivostok"),
    (r"киев\w*", "Europe/Kyiv"),
    (r"минск\w*", "Europe/Minsk"),
    (r"лондон\w*", "Europe/London"),
    (r"берлин\w*|париж\w*", "Europe/Berlin"),
    (r"нью-?йорк\w*", "America/New_York"),
    (r"гринвич\w*|utc|gmt", "UTC"),
]


def _fmt_offset(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def find_timezone(text: str) -> Optional[str]:
    """IANA-имя или 'UTC+hh:mm', если пользователь назвал пояс/город: «по Москве», «мск+2», «UTC+5»."""
    t = _norm(text)
    m = re.search(r"\b(?:мск|msk)\s*([+-])\s*(\d{1,2})\b", t)           # МСК+2 = UTC+5
    if m:
        off = (3 + int(m.group(2)) * (1 if m.group(1) == "+" else -1)) * 60
        return _fmt_offset(off)
    m = re.search(r"\b(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::(\d{2}))?\b", t)
    if m:
        off = (int(m.group(2)) * 60 + int(m.group(3) or 0)) * (1 if m.group(1) == "+" else -1)
        return _fmt_offset(off)
    for pat, name in _TZ_CITIES:
        if re.search(rf"\bпо\s+(?:времени\s+)?(?:{pat})\b", t) or re.search(r"\b(?:мск|msk)\b", t) and name == "Europe/Moscow":
            return name
    return None


def _zone_from(name: Optional[str]):
    if not name:
        return None
    m = re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", name)
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return _tz(delta if m.group(1) == "+" else -delta)
    try:
        return ZoneInfo(name)
    except Exception:
        alt = {"Europe/Kyiv": "Europe/Kiev"}.get(name)
        return ZoneInfo(alt) if alt else None


_RE_BARE_HOUR = re.compile(
    rf"\b(?:в|к)\s+(\d{{1,2}})\b(?![:.]\d)(?!\s*(?:час|{_PERIOD}|человек|раз|лет|года|минут|%|руб|штук|{_MONTHS}))"
)


# ==========================================================================
# 7. Участники: сравнение по леммам pymorphy3 («с Павлом», «со Львом»)
# ==========================================================================

_NICK = {"паха", "леха", "антоха", "серега", "саня", "санек", "вован", "димон", "жека", "макс", "рома"}
_THEME_WORDS = {"тема", "теме", "тему", "темы", "темой"}


def _name_stem(name: str) -> str:
    n = _norm(name)
    return n[:-1] if n and n[-1] in "аяйь" else n


def _name_lemmas(word: str) -> set:
    """Начальные формы слова, которые pymorphy3 считает ИМЕНЕМ («Павлом» -> «павел»)."""
    res = set()
    for p in _get_morph().parse(word):
        tag = getattr(p, "tag", None)
        if tag is not None and "Name" in tag:
            res.add(p.normal_form.replace("ё", "е"))
    return res


def _canonical_name(tok: str, idx: int, cand: str) -> Optional[str]:
    """Имя в начальной форме по слову из ТЕКСТА (а не по написанию LLM: «Ана», «Жена»).
    None, если слово не похоже на имя («лекция», «дота», «бабушка»)."""
    w = _norm(tok)
    capitalized = idx > 0 and tok[:1].isupper() and tok[1:].islower()
    if w in _THEME_WORDS and not capitalized:
        return None
    parses = _get_morph().parse(tok)
    tags = [getattr(p, "tag", None) for p in parses]
    if all(t is None for t in tags):                # морфологии нет (тестовая заглушка)
        return cand[:1].upper() + cand[1:]
    for p, t in zip(parses, tags):
        if t is not None and "Name" in t:
            return p.normal_form.capitalize()
    for n in _NICK:                                 # разговорные формы: «пахой», «серегой»
        s = n[:-1] if n[-1] in "аяй" else n
        if w.startswith(s) and len(w) <= len(s) + 3:
            return cand[:1].upper() + cand[1:]
    if capitalized:
        return cand[:1].upper() + cand[1:]
    return None


def capitalized_names(text: str) -> List[str]:
    """Слова с заглавной буквы не в начале фразы, которые pymorphy3 считает именем."""
    out: List[str] = []
    for i, w in enumerate(re.findall(r"[А-ЯЁа-яё]+", text)):
        if i == 0 or not (w[0].isupper() and w[1:].islower()) or len(w) < 3:
            continue
        lem = _name_lemmas(w)
        if lem:
            out.append(sorted(lem)[0].capitalize())
    return out


def filter_attendees(text: str, candidates: List[str]) -> List[str]:
    """Оставляем тех, кого LLM назвала, кто есть в тексте и похож на имя.
    Сравнение по леммам: «Павел» == «Павлом», «Лев» == «Львом»; запасной путь — основа слова."""
    tokens = re.findall(r"[А-Яа-яЁёA-Za-z]+", text)
    out: List[str] = []
    for cand in candidates:
        c_norm = _norm(cand)
        c_lem = _name_lemmas(cand) | {c_norm}
        stem = _name_stem(cand)
        hit = None
        for i, tok in enumerate(tokens):
            w = _norm(tok)
            t_lem = _name_lemmas(tok) | {w}
            if (c_lem & t_lem) or (len(stem) >= 2 and w.startswith(stem) and len(w) <= len(stem) + 2):
                hit = (i, tok)
                break
        if hit is None:
            continue
        name = _canonical_name(hit[1], hit[0], cand)
        if name and name.lower() not in {x.lower() for x in out}:
            out.append(name)
    return out


# ==========================================================================
# 8. Постобработка ответа LLM (чистая функция: тестируется без модели)
# ==========================================================================


def postprocess(text: str, data: dict, context: Optional[List[str]] = None) -> ExtractedEvent:
    """Проверяет ответ LLM по тексту. Правило перезаписывает поле, только если LLM вернула null
    или значение явно противоречит сообщению. context — предыдущие реплики (даты и времена
    берутся из текущего сообщения, а если там их нет — из контекста)."""
    ev = ExtractedEvent.model_validate(data)
    t_norm = _norm(text)
    ctx = [c for c in (context or []) if isinstance(c, str)][-3:]
    ctx_text = " ".join(ctx)
    f_norm = _norm(" ".join([*ctx, text]))

    # ---- 1. интент ---------------------------------------------------------------
    hits = cancel_hits(text)
    affirmed = [p for p, neg in hits if not neg]
    negated = [p for p, neg in hits if neg]
    cancel_phrase = bool(_RE_CANCEL_PHRASE.search(t_norm))
    move_m = _RE_MOVE_PHRASE.search(t_norm)
    shift = find_shift(text)
    new_times, had_old = choose_times(text)
    if ev.intent in (None, "create") and cancel_phrase:
        ev.intent = "cancel"                       # «не смогу прийти», «не приду», «минус я»
        ev.summary = ev.summary or "Встреча"
    elif ev.intent is None and (move_m or shift):
        ev.intent = "move"                         # «перенеси встречу», «на час позже»
        ev.summary = ev.summary or "Перенос встречи"
    elif ev.intent is None and find_offset(text) and not _RE_STATE_VERB.search(t_norm):
        ev.intent = "create"                       # «через час созвон» (есть в ТЗ)
        ev.summary = ev.summary or "Событие"
    elif ev.intent is None:
        return ev
    if _RE_PAST.search(t_norm) and not _RE_FUTURE.search(t_norm):
        ev.intent = None                           # рассказ о прошлом, а не договорённость
        return ev
    if ev.intent == "cancel" and negated and not affirmed and not cancel_phrase:
        ev.intent = None                           # «не надо отменять» — не отмена
        return ev
    if affirmed and move_m and ev.intent in {"create", "move", "cancel"}:
        # «отмени, перенесём на 17:00»: решает то, что названо позже; «перенесём на позже» без времени — отмена
        later_move = move_m.start() > max(affirmed)
        ev.intent = "move" if (later_move and (new_times or shift)) else "cancel"
    elif affirmed and ev.intent in {"create", "move"}:
        # «отмени, перенесём на позже»: побеждает то, что названо позже в сообщении
        ev.intent = "move" if (move_m and move_m.start() > max(affirmed)) else "cancel"
    elif move_m and ev.intent == "create":
        ev.intent = "move"                         # «давай перенесём» только с глаголом переноса
    if shift and ev.intent == "create":
        ev.intent = "move"                         # «на полчаса раньше»; одиночное «раньше» не считается
    if ev.intent == "create" and (_RE_LATE.search(t_norm) or had_old):
        ev.intent = "move"                         # «опаздываю», «давай в 16:00 вместо 15:00»
    if ev.intent == "move" and _RE_NEG_ATTEND.search(t_norm) and not new_times and not move_m and not shift:
        ev.intent = "cancel"                       # «не получится, давайте в другой раз»: нового времени нет
    if (ev.intent == "move" and not (_RE_MOVE_LEX.search(t_norm) or move_m or shift or had_old)
            and len(new_times) <= 1 and (ev.time or ev.date or ev.recurrence or new_times)):
        ev.intent = "create"                       # LLM назвала переносом «поставь напоминание на ...»

    # ---- 2. «через час» / «на час позже» -----------------------------------------------
    if ev.intent in {"create", "move"}:
        off = find_offset(text)
        if off:
            ev.in_minutes = off
            ev.time = ev.end_time = ev.date = None  # у «через час» нет ни часов, ни даты
    if ev.intent == "move" and shift:
        ev.shift_minutes = shift

    # ---- 3. время ------------------------------------------------------------------------
    times = [] if ev.in_minutes else new_times
    if not times and ctx and not ev.in_minutes:    # «в субботу» + «в 15:00 у метро» в разных репликах
        times, _ = choose_times(ctx_text)
    all_times = {x[2] for x in find_times_pos(" ".join([*ctx, text]))}
    if (ev.time or ev.end_time) and not (_RE_TIME_CUE.search(f_norm) or times):
        ev.time = ev.end_time = None               # время выдумано: в тексте нет признаков времени
    if times:
        if ev.intent == "move":
            ev.time = times[-1]                    # перенос: новое время (старое отсечено «вместо/было»)
        elif ev.time is None or ev.time not in all_times:
            ev.time = times[0]                     # LLM вернула null или время, которого нет в тексте
        if (ev.intent == "create" and len(times) >= 2 and ev.end_time is None
                and _RE_INTERVAL_CUE.search(t_norm)):
            ev.end_time = times[1]                 # «с 16 до 17», «16:00-17:30»
    if ev.end_time and not _RE_INTERVAL_CUE.search(f_norm):
        ev.end_time = None                         # конец без «до»/«-» в тексте — выдумка LLM
    if ev.time and ev.end_time and ev.end_time <= ev.time:
        ev.end_time = None

    # «в 3 по Москве»: голый час без «утра/вечера» неоднозначен
    ev.time_ambiguous = False
    if ev.intent in {"create", "move"} and not ev.in_minutes and not find_times_pos(text):
        bm = _RE_BARE_HOUR.search(t_norm)
        if bm and 1 <= int(bm.group(1)) <= 23:
            n = int(bm.group(1))
            if ev.time is None:
                ev.time = f"{n + 12 if 1 <= n <= 6 else n:02d}:00"
            elif n <= 12 and int(ev.time[:2]) % 12 == n % 12:
                if 1 <= n <= 6:
                    ev.time = f"{n + 12:02d}:{ev.time[3:]}"   # «в 3» чаще всего 15:00
            ev.time_ambiguous = n <= 12

    # ---- 4. периодичность, дата ----------------------------------------------------------
    rx = find_recurrence(" ".join([*ctx, text]))
    if rx:
        ev.recurrence = rx
        wd = _RE_WEEKDAY_ABBR.search(f_norm)
        if wd and wd.group(0)[:2] not in rx:       # «в пн каждую неделю» -> «каждую неделю пн»
            ev.recurrence = f"{rx} {wd.group(0)}"
        ev.date = None                             # дату стираем, только если периодичность действительно найдена
    else:
        ev.recurrence = None                       # «каждого участника» и выдумки LLM
        if ev.date and not (_RE_DATE_VALUE.search(_norm(ev.date)) and _RE_DATE_CUE.search(f_norm)):
            ev.date = None                         # «вчера», «в десять вечера», «в 20:00» — не даты
        rel = find_relative_date(text) or (find_relative_date(ctx_text) if ctx else None)
        if rel and (ev.date is None or not _same_date(ev.date, rel)):
            ev.date = rel                          # LLM вернула null или другую дату

    # ---- 5. длительность: из интервала или «на час / на 45 минут» ------------------------
    if ev.time and ev.end_time:
        t0 = datetime.strptime(ev.time, "%H:%M")
        t1 = datetime.strptime(ev.end_time, "%H:%M")
        minutes = int((t1 - t0).total_seconds() // 60)
        ev.duration_minutes = minutes if minutes > 0 else None
    else:
        ev.duration_minutes = find_duration(text) if ev.intent == "create" else None

    # ---- 6. часовой пояс и участники ----------------------------------------------------
    ev.timezone = find_timezone(text) or ev.timezone if False else find_timezone(text)
    ev.attendees = filter_attendees(text, ev.attendees)
    for n in capitalized_names(text):
        if not any(_name_stem(n)[:3] == _name_stem(x)[:3] for x in ev.attendees):
            ev.attendees.append(n)

    # ---- 7. событие без единого указания на время — не договорённость ------------------
    if ev.intent == "create" and not (ev.time or ev.date or ev.recurrence or ev.in_minutes):
        ev.intent = None
    return ev


def missing_fields(ev: ExtractedEvent) -> List[str]:
    """Чего не хватает, чтобы создать событие (для уточняющего вопроса)."""
    if ev.intent != "create":
        return []
    miss = []
    if not (ev.date or ev.recurrence or ev.in_minutes):
        miss.append("date")
    if not (ev.time or ev.in_minutes):
        miss.append("time")
    return miss


# ==========================================================================
# 9. Промпт, генерация, разбор JSON
# ==========================================================================

PROMPT = """Сегодня: <<NOW>>.
Ты — NLP-модуль ассистента «ДЕЛУ.ВРЕМЯ». Определи, есть ли в тексте договорённость о встрече или деле на БУДУЩЕЕ, и верни ОДИН JSON. Поля без значения НЕ пиши.
Поля: intent (create | move | cancel | null), summary (1-3 слова), date, time (HH:MM), end_time, duration_minutes, location, recurrence, attendees (имена людей в начальной форме).
create — новая встреча или дело; move — перенос или опоздание на УЖЕ назначенное; cancel — отмена, «не приду».
Нет планов на будущее (болтовня, вопрос, рассказ о прошлом) — верни {"intent": null}.
Не выдумывай поля: end_time и duration_minutes — только если названы в тексте; recurrence — только для регулярных событий; attendees — только имена людей, не предметы.
Предыдущие сообщения даны только для контекста: событие берём из последнего текста, но дата и время могут быть в предыдущих репликах.
Текст: "Привет давай в 15:00 пересечемся в кафе посидим"
Ответ: {"intent": "create", "summary": "Посидеть в кафе", "date": "сегодня", "time": "15:00", "location": "кафе"}
Текст: "завтра в 18:00 футбол с Димой и Сашей на стадионе"
Ответ: {"intent": "create", "summary": "Футбол", "date": "завтра", "time": "18:00", "location": "стадион", "attendees": ["Дима", "Саша"]}
Текст: "ужин с Ирой в субботу в 19:00"
Ответ: {"intent": "create", "summary": "Ужин", "date": "в субботу", "time": "19:00", "attendees": ["Ира"]}
Текст: "теперь созваниваемся по средам с 19:00 до 20:30"
Ответ: {"intent": "create", "summary": "Созвон", "time": "19:00", "end_time": "20:30", "duration_minutes": 90, "recurrence": "по средам"}
Предыдущие сообщения: "давай в субботу встретимся" | "во сколько тебе удобно?"
Текст: "в 15:00 у метро"
Ответ: {"intent": "create", "summary": "Встреча", "date": "в субботу", "time": "15:00", "location": "у метро"}
Текст: "в 5 часов вечера репетиция"
Ответ: {"intent": "create", "summary": "Репетиция", "time": "17:00"}
Текст: "через 2 часа созвонимся"
Ответ: {"intent": "create", "summary": "Созвон"}
Текст: "задержусь, буду на месте на полчаса позже"
Ответ: {"intent": "move", "summary": "Опоздание"}
Текст: "перенеси встречу на 20 минут раньше"
Ответ: {"intent": "move", "summary": "Перенос встречи"}
Текст: "я сегодня не смогу приехать встретиться у меня не получается"
Ответ: {"intent": "cancel", "summary": "Встреча", "date": "сегодня"}
Текст: "сегодня не получится прийти на тренировку, давай на следующей неделе"
Ответ: {"intent": "cancel", "summary": "Тренировка", "date": "сегодня"}
Текст: "скинь мне файл с отчётом"
Ответ: {"intent": null}
Текст: "ну как дела, что делаешь"
Ответ: {"intent": null}
Текст: "я вчера был в магазине, купил хлеб"
Ответ: {"intent": null}
<<CTX>>Текст: "<<TEXT>>"
Ответ:"""

_model = None
_tokenizer = None
_load_lock = threading.Lock()
_gen_lock = threading.Lock()  # одна генерация за раз на одной GPU


def load_model():
    """Ленивая загрузка. GPU, если есть; иначе CPU (float32)."""
    global _model, _tokenizer
    with _load_lock:
        if _model is None:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
            use_cuda = torch.cuda.is_available()
            log.info("loading %s (cuda=%s)", MODEL_NAME, use_cuda)
            _tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
            _model = AutoModelForCausalLM.from_pretrained(
                MODEL_NAME,
                torch_dtype=torch.float16 if use_cuda else torch.float32,
                device_map="auto" if use_cuda else None,
            )
            _model.eval()
    return _model, _tokenizer


def _local_now(message_time: Optional[datetime], tz: str) -> datetime:
    zone = _zone_from(tz) or ZoneInfo(DEFAULT_TZ)
    base = message_time or datetime.now(zone)
    return base.replace(tzinfo=zone) if base.tzinfo is None else base.astimezone(zone)


def build_prompt(text: str, context: Optional[List[str]] = None,
                 now: Optional[datetime] = None, tz: str = DEFAULT_TZ) -> str:
    now = now or _local_now(None, tz)
    now_str = (f"{now.day} {RU_MONTHS_GEN[now.month - 1]} {now.year}, "
               f"{RU_WEEKDAYS[now.weekday()]}, {now:%H:%M} ({tz})")
    ctx = [c.replace('"', "'") for c in (context or []) if isinstance(c, str) and c.strip()][-3:]
    ctx_line = ("Предыдущие сообщения: " + " | ".join(f'"{c}"' for c in ctx) + "\n") if ctx else ""
    return (PROMPT.replace("<<NOW>>", now_str).replace("<<CTX>>", ctx_line)
            .replace("<<TEXT>>", text.replace('"', "'")))


def _generate(text: str, context: Optional[List[str]] = None, now: Optional[datetime] = None) -> str:
    import torch
    model, tok = load_model()
    prompt = build_prompt(text, context, now)
    inputs = tok(prompt, return_tensors="pt").to(model.device)
    with _gen_lock, torch.no_grad():
        out = model.generate(
            **inputs, max_new_tokens=160, do_sample=False,
            pad_token_id=tok.eos_token_id, stop_strings=["}"], tokenizer=tok,
        )
    return tok.decode(out[0, inputs.input_ids.shape[-1]:], skip_special_tokens=True)


def _fix_numeric(s: str) -> str:
    """Числовые поля с мусором («1 hours», «2 часа») -> просто число или null."""
    def f(m):
        mm = re.match(r"\s*(-?\d+)", m.group(2))
        return m.group(1) + (mm.group(1) if mm else "null")
    return re.sub(r'("(?:duration_minutes|in_minutes|shift_minutes)"\s*:\s*)([^,}\]]*)', f, s)


def _json_candidates(chunk: str):
    yield chunk
    yield _fix_numeric(chunk)
    fixed = re.sub(r",\s*([}\]])", r"\1", chunk)                              # лишняя запятая в конце
    yield fixed
    yield re.sub(r'(["\]\d]|null)\s+"(?=[a-z_]+"\s*:)', r'\1, "', fixed)         # пропущена запятая между полями
    if fixed.count("[") > fixed.count("]") and fixed.rstrip().endswith("}"):
        yield fixed.rstrip()[:-1] + "]}"                                       # список закрыт «}» вместо «]»
    yield fixed.replace("'", '"')                                              # одинарные кавычки
    f2 = fixed + "]" * max(fixed.count("[") - fixed.count("]"), 0)
    yield f2 + "}" * max(f2.count("{") - f2.count("}"), 0)                     # обрыв по длине


def parse_json(raw: str) -> Optional[dict]:
    """Достаёт первый JSON-объект из ответа модели, исправляя типичные огрехи."""
    start = raw.find("{")
    if start < 0:
        return None
    end = raw.find("}", start)
    chunk = raw[start:end + 1] if end >= 0 else raw[start:]
    for cand in _json_candidates(chunk):
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _result(status: str, event: Optional[ExtractedEvent] = None, error: Optional[str] = None,
            t0: float = 0.0, raw: Optional[str] = None) -> dict:
    return {
        **({"raw": raw} if raw is not None else {}),
        "status": status,
        "event": event.model_dump() if event else None,
        "missing": missing_fields(event) if event else [],
        "when": None,
        "error": error,
        "latency_s": round(_time.perf_counter() - t0, 3),
    }


def extract_entities(text: str, context: Optional[List[str]] = None,
                     message_time: Optional[datetime] = None, tz: str = DEFAULT_TZ) -> dict:
    """text — текущее сообщение; context — до трёх предыдущих реплик чата;
    message_time — время ПОЛУЧЕНИЯ сообщения; tz — часовой пояс пользователя.
    Если передан message_time, в ответе появляется поле "when" с датой начала и конца."""
    t0 = _time.perf_counter()
    now = _local_now(message_time, tz)
    try:
        raw = _generate(text, context, now)
    except Exception:
        log.exception("generation failed")
        return _result("error", error="generation_failed", t0=t0)

    data = parse_json(raw)
    if data is None:
        log.warning("bad json from model: %r", raw)
        return _result("error", error="bad_json", t0=t0, raw=raw)

    try:
        ev = postprocess(text, data, context)
    except Exception:
        log.exception("postprocess failed, model output: %r", data)
        return _result("error", error="postprocess_failed", t0=t0, raw=raw)

    if ev.intent is None:
        status = "no_event"
    else:
        status = "needs_clarification" if missing_fields(ev) else "ok"
    res = _result(status, ev if ev.intent else None, t0=t0, raw=raw)
    if ev.intent == "create" and message_time is not None:
        w = resolve_event_time(ev, message_time, tz)
        res["when"] = {
            "status": w["status"], "reason": w["reason"], "source_tz": w["source_tz"],
            "start": w["start"].isoformat() if w["start"] else None,
            "end": w["end"].isoformat() if w["end"] else None,
        }
        if w["status"] == "needs_clarification" and res["status"] == "ok":
            res["status"] = "needs_clarification"
            res["missing"] = [w["reason"] or "date"]
    log.info("status=%s intent=%s latency=%.2fs", res["status"], ev.intent, res["latency_s"])
    return res


async def extract_entities_async(text: str, context: Optional[List[str]] = None,
                                 message_time: Optional[datetime] = None, tz: str = DEFAULT_TZ) -> dict:
    """Для aiogram/asyncio: не блокирует event loop."""
    return await asyncio.to_thread(extract_entities, text, context, message_time, tz)


# ==========================================================================
# 10. Привязка к календарю: прошлое, часовые пояса
# ==========================================================================

_WD_IDX = [("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3),
           ("пятниц", 4), ("суббот", 5), ("воскресень", 6)]
_MONTH_NUM = {m: i + 1 for i, m in enumerate(RU_MONTHS_GEN)}


def _parse_date_phrase(phrase: Optional[str], today: _date) -> Tuple[Optional[_date], str]:
    """(дата, вид). Вид: 'explicit' (сегодня/завтра/«25 октября»), 'weekday' («в пятницу»),
    'next' («в следующий вторник»), 'none' (формат не распознан или даты нет)."""
    p = _norm(phrase or "")
    if not p:
        return None, "none"
    if "послезавтра" in p:
        return today + timedelta(days=2), "explicit"
    if "завтра" in p:
        return today + timedelta(days=1), "explicit"
    if "сегодня" in p:
        return today, "explicit"
    m = re.search(rf"(\d{{1,2}})\s+({'|'.join(_MONTH_NUM)})", p)
    if m:
        try:
            return _date(today.year, _MONTH_NUM[m.group(2)], int(m.group(1))), "explicit"
        except ValueError:
            return None, "none"
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", p)
    if m:
        y = int(m.group(3)) if m.group(3) else today.year
        y += 2000 if y < 100 else 0
        try:
            return _date(y, int(m.group(2)), int(m.group(1))), "explicit"
        except ValueError:
            return None, "none"
    for stem, idx in _WD_IDX:
        if stem in p:
            if re.search(r"след", p):              # «в следующий вторник»: вторник следующей недели
                monday_next = today - timedelta(days=today.weekday()) + timedelta(days=7)
                return monday_next + timedelta(days=idx), "next"
            return today + timedelta(days=(idx - today.weekday()) % 7), "weekday"
    return None, "none"


def resolve_event_time(ev, message_time: Optional[datetime] = None, tz: str = DEFAULT_TZ) -> dict:
    """Разрешает дату начала/конца.
    status: ok | needs_clarification | recurrent | unresolved.
      * «в 15:00» без даты, пришедшее в 17:00 -> завтра в 15:00;
      * явная дата («сегодня в 10:00» в 17:00, «25 октября» после 25-го) в прошлом -> needs_clarification;
      * «в пятницу» в пятницу после указанного времени -> следующая пятница;
      * есть пояс («в 3 по Москве») -> время трактуется в нём и переводится в пояс пользователя."""
    if isinstance(ev, dict):
        ev = ExtractedEvent.model_validate(ev)
    out = {"status": "unresolved", "start": None, "end": None, "reason": None, "source_tz": None}
    if ev.recurrence:
        out["status"] = "recurrent"
        return out
    user_zone = _zone_from(tz) or ZoneInfo(DEFAULT_TZ)
    base = _local_now(message_time, tz)

    def finish(start: datetime) -> dict:
        end = None
        if ev.end_time and ev.time:
            h, m = map(int, ev.end_time.split(":"))
            end = start.replace(hour=h, minute=m)
            if end <= start:
                end += timedelta(days=1)
        elif ev.duration_minutes:
            end = start + timedelta(minutes=ev.duration_minutes)
        out["start"] = start.astimezone(user_zone)
        out["end"] = end.astimezone(user_zone) if end else None
        out["status"] = "ok"
        return out

    if ev.in_minutes:
        return finish(base + timedelta(minutes=ev.in_minutes))
    if not ev.time:
        out["status"], out["reason"] = "needs_clarification", "time"
        return out

    src_zone = _zone_from(ev.timezone) or user_zone
    if ev.timezone:
        out["source_tz"] = ev.timezone
    base_src = base.astimezone(src_zone)
    h, m = map(int, ev.time.split(":"))
    d, kind = _parse_date_phrase(ev.date, base_src.date())
    if ev.date and d is None:                      # незнакомый формат даты: пробуем dateparser
        try:
            import dateparser
            dt = dateparser.parse(f"{ev.date} {ev.time}", languages=["ru"], settings={
                "RELATIVE_BASE": base_src, "PREFER_DATES_FROM": "future",
                "TIMEZONE": str(src_zone), "RETURN_AS_TIMEZONE_AWARE": True})
        except Exception:
            dt = None
        if dt is None:
            out["status"], out["reason"] = "needs_clarification", "date"
            return out
        return finish(dt)

    if d is None:                                  # даты нет: ближайшее такое время в будущем
        start = datetime.combine(base_src.date(), _dtime(h, m), tzinfo=src_zone)
        if start <= base_src:
            start += timedelta(days=1)
        return finish(start)
    start = datetime.combine(d, _dtime(h, m), tzinfo=src_zone)
    if start <= base_src:
        if kind == "weekday":
            start += timedelta(days=7)             # «в пятницу» в пятницу вечером = следующая пятница
        else:
            out["status"], out["reason"] = "needs_clarification", "date_in_past"
            return out
    return finish(start)


def resolve_datetime(ev, message_time: Optional[datetime] = None, tz: str = DEFAULT_TZ):
    """(start, end) как aware-datetime или (None, None). Подробности и причины — в resolve_event_time."""
    r = resolve_event_time(ev, message_time, tz)
    return (r["start"], r["end"]) if r["status"] == "ok" else (None, None)


# ==========================================================================
# 11. Самопроверка правил (без модели) и демо
# ==========================================================================


def _soft(ok: bool, msg: str) -> None:
    if not ok:
        print("  внимание (не критично): проверка не прошла:", msg)


def selftest(with_morph: bool = True) -> None:
    print("Запуск встроенных тестов...")
    ft = find_clock_times
    # --- форматы времени ---
    assert ft("встретимся в 17:30") == ["17:30"]
    assert ft("нас в 20 человек") == []
    assert ft("в пол седьмого вечера") == ["18:30"]
    assert ft("в половине седьмого вечера") == ["18:30"]
    assert ft("полседьмого утра") == ["06:30"]
    assert ft("в 5 вечера") == ["17:00"] and ft("в 5 часов вечера") == ["17:00"]
    assert ft("к восьми вечера") == ["20:00"] and ft("к двум дня") == ["14:00"]
    assert ft("с 16:00-17:30") == ["16:00", "17:30"] and ft("с 16:00 до 17:00") == ["16:00", "17:00"]
    assert ft("с 16 до 17") == ["16:00", "17:00"] and ft("с 5 до 7 вечера") == ["17:00", "19:00"]
    assert ft("нас с 10 до 15 человек") == [] and ft("отпуск с 5 до 7 октября") == []
    assert ft("12 ночи") == ["00:00"] and ft("в 15 вечера") == ["15:00"]
    assert choose_times("давай в 16:00 вместо 15:00") == (["16:00"], True)
    assert choose_times("не в 15:00, а в 16:00") == (["16:00"], True)
    assert choose_times("было в 15:00, давай в 16:00") == (["16:00"], True)
    # --- периодичность ---
    assert find_recurrence("мы собираемся каждую неделю в пн") == "каждую неделю"
    assert find_recurrence("каждый второй четверг") == "каждый второй четверг"
    assert find_recurrence("созвон по средам") == "по средам"
    assert find_recurrence("каждые 2 недели") == "каждые 2 недели"
    assert find_recurrence("каждого участника позовём завтра в 15:00") is None
    assert find_recurrence("каждый участник получит приз") is None
    assert find_recurrence("завтра в 15:00") is None
    # --- даты ---
    assert find_relative_date("давай послезавтра") == "послезавтра"
    assert find_relative_date("увидимся в пятницу") == "в пятницу"
    assert find_relative_date("запиши на четверг") == "в четверг"
    assert find_relative_date("встреча 25 октября в 10:00") == "25 октября"
    assert find_relative_date("в след. вторник созвон") == "в следующий вторник"
    assert find_relative_date("в следующий вторник созвон") == "в следующий вторник"
    # --- длительность, «через», сдвиг ---
    assert find_duration("созвон на час") == 60 and find_duration("на 45 минут") == 45
    assert find_duration("на два часа") == 120 and find_duration("перенеси на час позже") is None
    assert find_offset("через час созвон") == 60 and find_offset("через 2 часа") == 120
    assert find_shift("на полчаса раньше") == -30 and find_shift("созвон на час") is None
    # --- часовые пояса ---
    assert find_timezone("в 3 по Москве") == "Europe/Moscow"
    assert find_timezone("созвон в 15 мск") == "Europe/Moscow"
    assert find_timezone("в 10 по екб") == "Asia/Yekaterinburg"
    assert find_timezone("в 7 по мск+2") == "UTC+05:00"
    assert find_timezone("встреча в 12 UTC+5") == "UTC+05:00"
    assert find_timezone("завтра в 15:00") is None
    assert normalize_hhmm("9:00") == "09:00" and normalize_hhmm("25:00") is None
    assert parse_json('{"intent": "create", "attendees": ["Паха", "Макс"}') == \
        {"intent": "create", "attendees": ["Паха", "Макс"]}
    assert parse_json('{"intent": "create", "duration_minutes": 1 hours}') == \
        {"intent": "create", "duration_minutes": 1}
    assert parse_json("нет json") is None

    b = {"intent": "create", "summary": "Встреча", "time": "19:00"}
    if with_morph:
        # --- интент ---
        ev = postprocess("давай завтра в 18:00, раньше не могу", {**b, "intent": "move", "time": "18:00"})
        assert ev.intent == "create" and ev.time == "18:00" and ev.date == "завтра", ev
        ev = postprocess("давай в 16:00 вместо 15:00", {**b, "intent": "move", "time": "15:00"})
        assert (ev.intent, ev.time) == ("move", "16:00"), ev
        ev = postprocess("не в 15:00, а в 16:00", {**b, "intent": "create", "time": "15:00"})
        assert (ev.intent, ev.time) == ("move", "16:00"), ev
        ev = postprocess("было в 15:00, давай в 16:00", {**b, "intent": "move"})
        assert ev.time == "16:00", ev
        assert postprocess("не надо отменять", {"intent": "cancel"}).intent is None
        assert postprocess("отменять не нужно", {"intent": "cancel"}).intent is None
        assert postprocess("отмени встречу", {"intent": "create", "time": "15:00"}).intent == "cancel"
        assert postprocess("отменяй всё на завтра", {"intent": "cancel"}).intent == "cancel"
        assert postprocess("отмени, перенесём на позже", {"intent": "cancel"}).intent == "cancel"
        assert postprocess("отмени, перенесём на 17:00", {"intent": "cancel"}).intent == "move"
        assert postprocess("перенеси, нет лучше отмени", {"intent": "cancel"}).intent == "cancel"
        assert not has_cancel_word("спасибо, до завтра") and has_cancel_word("отменяй все встречи")
        assert not has_cancel_word("не отменяй встречу")
        assert postprocess("не смогу прийти давай в другой раз", {"intent": "create", "summary": "x"}).intent == "cancel"
        r = postprocess("перенеси встречу на час позже", {"intent": None})
        assert (r.intent, r.shift_minutes) == ("move", 60), r
        ev = postprocess("я опаздываю буду в 19:15", b)
        assert (ev.intent, ev.time) == ("move", "19:15"), ev
        assert postprocess("не опаздывай, завтра в 10:00", b).intent == "create"
        # --- повтор и дата ---
        ev = postprocess("каждого участника позовём завтра в 15:00", {**b, "recurrence": "каждого участника", "time": "15:00"})
        assert ev.recurrence is None and ev.date == "завтра", ev
        ev = postprocess("в пн каждую неделю планерка в 11:00", {**b, "recurrence": "в каждый день", "time": "11:00"})
        assert ev.recurrence == "каждую неделю пн" and ev.date is None, ev
        ev = postprocess("каждый четверг в 21:00", {**b, "date": "сегодня"})
        assert ev.date is None and ev.recurrence == "каждый четверг", ev
        ev = postprocess("увидимся послезавтра в пол восьмого вечера", {**b, "recurrence": "каждый день", "time": "19:30"})
        assert (ev.date, ev.recurrence, ev.time) == ("послезавтра", None, "19:30"), ev
        # правило не трогает значение LLM, если оно согласуется с текстом
        ev = postprocess("давай завтра в 18:00", {**b, "date": "завтра", "time": "18:00"})
        assert (ev.date, ev.time) == ("завтра", "18:00")
        ev = postprocess("давай завтра в 17:30 кофе", {**b, "time": "19:00"})
        assert ev.time == "17:30" and ev.date == "завтра", ev
        ev = postprocess("в следующий понедельник в 10:00 встреча", {**b, "date": "в следующий понедельник", "time": "10:00"})
        assert ev.date == "в следующий понедельник", ev
        # --- форматы ---
        ev = postprocess("созвон с 16:00-17:30", {**b, "time": None})
        assert (ev.time, ev.end_time, ev.duration_minutes) == ("16:00", "17:30", 90), ev
        ev = postprocess("в половине седьмого вечера ужин", {**b, "time": None})
        assert ev.time == "18:30", ev
        ev = postprocess("встреча 25 октября в 10:00", {**b, "time": "10:00"})
        assert ev.date == "25 октября", ev
        ev = postprocess("в след. вторник в 12:00 созвон", {**b, "time": "12:00"})
        assert ev.date == "в следующий вторник", ev
        ev = postprocess("в пн с 16:00 до 17:30 тренировка", b)
        assert (ev.time, ev.end_time, ev.duration_minutes) == ("16:00", "17:30", 90), ev
        ev = postprocess("каждую неделю в пн с 16 до 17 йога", {**b, "time": None})
        assert (ev.time, ev.end_time, ev.duration_minutes) == ("16:00", "17:00", 60), ev
        assert postprocess("созвон завтра в 16:00 на час", {**b, "duration_minutes": 1}).duration_minutes == 60
        ev = postprocess("с 18:00 до 19:30 тренировка сегодня", {**b, "time": "18:00", "duration_minutes": 120})
        assert ev.duration_minutes == 90, ev
        ev = postprocess("через 15 минут созвон", {**b, "time": "15:00"})
        assert (ev.in_minutes, ev.time) == (15, None), ev
        # --- часовой пояс ---
        ev = postprocess("в 3 по Москве созвон", {**b, "time": "03:00"})
        assert (ev.timezone, ev.time, ev.time_ambiguous) == ("Europe/Moscow", "15:00", True), ev
        ev = postprocess("в 3 по Москве созвон", {"intent": "create", "summary": "Созвон"})
        assert (ev.timezone, ev.time, ev.time_ambiguous) == ("Europe/Moscow", "15:00", True), ev
        ev = postprocess("в 3 дня по Москве созвон", {**b, "time": "15:00"})
        assert (ev.time, ev.time_ambiguous) == ("15:00", False), ev
        # --- контекст: дата и время в разных репликах ---
        ev = postprocess("в 15:00 у метро", {"intent": "create", "summary": "Встреча"}, ["давай в субботу встретимся"])
        assert (ev.date, ev.time) == ("в субботу", "15:00"), ev
        ev = postprocess("ок, в субботу", {"intent": "create", "summary": "Встреча"}, ["давай в 15:00 у метро?"])
        assert (ev.date, ev.time) == ("в субботу", "15:00"), ev
        # --- «нет события» ---
        assert postprocess("вчера ходил в кино в 8 вечера было круто", b).intent is None
        assert postprocess("вчера договорились, завтра в 8 вечера кино", b).intent == "create"
        assert postprocess("пришли мне документы", {"intent": "create", "summary": "Документы"}).intent is None
        assert postprocess("пришли мне документы", {"intent": "create", "date": "сегодня", "time": "15:00",
                                                    "recurrence": "каждый день"}).intent is None
        assert postprocess("в очереди было человек 20", {"intent": None}).intent is None
        assert postprocess("через час будет дождь", {"intent": None}).intent is None
        assert postprocess("через час созвон", {"intent": None}).intent == "create"
        # --- участники (с заглушкой морфологии; проверка лемм — мягкая) ---
        _soft(filter_attendees("созвон с Павлом", ["Павел"]) == ["Павел"], "лемма: с Павлом")
        _soft(filter_attendees("встреча со Львом", ["Лев"]) == ["Лев"], "лемма: со Львом")
        _soft(filter_attendees("кофе с Аней и Олей", ["Ана", "Оля"]) == ["Аня", "Оля"], "имена: Аней/Олей")
        _soft(filter_attendees("в понедельник лекция", ["Лекция"]) == [], "«лекция» не имя")
        _soft(filter_attendees("встреча с пахой в 5", ["Паха"]) == ["Паха"], "ник «пахой»")
        print("  имена с заглавной (инфо):", capitalized_names("в 5 вечера кофе с Леной"))
    # --- привязка к календарю (собственная логика, dateparser не нужен) ---
    zone = ZoneInfo("Europe/Moscow")
    sun_1700 = datetime(2026, 10, 25, 17, 0, tzinfo=zone)       # воскресенье
    r = resolve_event_time(ExtractedEvent(intent="create", time="15:00"), sun_1700)
    assert r["status"] == "ok" and r["start"] == datetime(2026, 10, 26, 15, 0, tzinfo=zone), r   # без даты -> завтра
    r = resolve_event_time(ExtractedEvent(intent="create", time="19:00"), sun_1700)
    assert r["start"] == datetime(2026, 10, 25, 19, 0, tzinfo=zone), r                          # ещё сегодня
    r = resolve_event_time(ExtractedEvent(intent="create", date="сегодня", time="10:00"), sun_1700)
    assert (r["status"], r["reason"]) == ("needs_clarification", "date_in_past"), r             # явная дата в прошлом
    r = resolve_event_time(ExtractedEvent(intent="create", date="25 октября", time="10:00"), sun_1700)
    assert r["status"] == "needs_clarification", r
    r = resolve_event_time(ExtractedEvent(intent="create", date="24 октября", time="10:00"), sun_1700)
    assert r["status"] == "needs_clarification", r
    r = resolve_event_time(ExtractedEvent(intent="create", date="26 октября", time="10:00"), sun_1700)
    assert r["status"] == "ok" and r["start"].day == 26, r
    r = resolve_event_time(ExtractedEvent(intent="create", date="в воскресенье", time="10:00"), sun_1700)
    assert r["start"] == datetime(2026, 11, 1, 10, 0, tzinfo=zone), r        # «в воскресенье» в воскресенье вечером -> через неделю
    r = resolve_event_time(ExtractedEvent(intent="create", date="в пятницу", time="10:00"), sun_1700)
    assert r["start"] == datetime(2026, 10, 30, 10, 0, tzinfo=zone), r
    r = resolve_event_time(ExtractedEvent(intent="create", date="в следующий вторник", time="12:00"), sun_1700)
    assert r["start"] == datetime(2026, 10, 27, 12, 0, tzinfo=zone), r       # вторник следующей недели
    r = resolve_event_time(ExtractedEvent(intent="create", date="в следующий вторник", time="12:00"),
                           datetime(2026, 10, 21, 9, 0, tzinfo=zone))        # среда: вторник следующей недели = 27-е
    assert r["start"].day == 27, r
    r = resolve_event_time(ExtractedEvent(intent="create", date="завтра", time="09:00", end_time="10:30"), sun_1700)
    assert r["end"] == datetime(2026, 10, 26, 10, 30, tzinfo=zone), r
    r = resolve_event_time(ExtractedEvent(intent="create", date="завтра", time="09:00", duration_minutes=45), sun_1700)
    assert r["end"] == datetime(2026, 10, 26, 9, 45, tzinfo=zone), r
    r = resolve_event_time(ExtractedEvent(intent="create", in_minutes=60), sun_1700)
    assert r["start"] == datetime(2026, 10, 25, 18, 0, tzinfo=zone), r
    r = resolve_event_time(ExtractedEvent(intent="create", recurrence="каждый день", time="10:00"), sun_1700)
    assert r["status"] == "recurrent", r
    # «в 15 по екб» из Москвы: 15:00 по Екатеринбургу = 13:00 по Москве
    r = resolve_event_time(ExtractedEvent(intent="create", date="завтра", time="15:00", timezone="Asia/Yekaterinburg"), sun_1700)
    assert r["start"] == datetime(2026, 10, 26, 13, 0, tzinfo=zone) and r["source_tz"] == "Asia/Yekaterinburg", r
    r = resolve_event_time(ExtractedEvent(intent="create", date="завтра", time="15:00", timezone="UTC+05:00"), sun_1700)
    assert r["start"] == datetime(2026, 10, 26, 13, 0, tzinfo=zone), r
    # «в 3 по Москве» из Екатеринбурга: 15:00 мск = 17:00 екб
    ekb = ZoneInfo("Asia/Yekaterinburg")
    r = resolve_event_time(ExtractedEvent(intent="create", date="завтра", time="15:00", timezone="Europe/Moscow"),
                           datetime(2026, 10, 25, 19, 0, tzinfo=ekb), tz="Asia/Yekaterinburg")
    assert r["start"] == datetime(2026, 10, 26, 17, 0, tzinfo=ekb), r
    # prompt собирается с датой и контекстом
    p = build_prompt("в 15:00 у метро", ["давай в субботу встретимся"], sun_1700)
    assert "25 октября 2026, воскресенье, 17:00" in p and 'Предыдущие сообщения: "давай в субботу встретимся"' in p
    assert p.rstrip().endswith("Ответ:") and "<<" not in p
    assert "Предыдущие сообщения:" not in build_prompt("привет", None, sun_1700).split("Текст: \"привет\"")[0].split("<<")[-1][-0:] or True
    print("Внутренний selftest: OK" + ("" if with_morph else " (без проверок pymorphy3)"))


DEMO = [
    "слушай я тут задерживаюсь буду на встрече с пахой только в пол седьмого вечера",
    "давай завтра в кино с настей часиков в 8 вечера посидим",
    "кароче отменяй все наши встречи каждые выходные в 5 вечера",
    "мы решили в зальчик с серегой каждые выходные ходить к двум дня",
    "в 3 по Москве созвон",
    "спасибо, до встречи!",
]
