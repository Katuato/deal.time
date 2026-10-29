"""
NLP/LLM-модуль проекта «ДЕЛУ.ВРЕМЯ» (v9)
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

def normalize_hhmm(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    m = re.fullmatch(r"\s*(\d{1,2})[:\.](\d{2})\s*", v)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"

class ExtractedEvent(BaseModel):
    intent: Optional[str] = None
    summary: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    end_time: Optional[str] = None
    duration_minutes: Optional[int] = None
    in_minutes: Optional[int] = None
    shift_minutes: Optional[int] = None
    location: Optional[str] = None
    recurrence: Optional[str] = None
    timezone: Optional[str] = None
    time_ambiguous: bool = False
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

_NUM_WORDS = {
    "час": 1, "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
}
_ORD_WORDS = {
    "первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "пятого": 5,
    "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9, "десятого": 10,
    "одиннадцатого": 11, "двенадцатого": 12,
}
_DAT_WORDS = {
    "двум": 2, "трем": 3, "четырем": 4, "пяти": 5, "шести": 6, "семи": 7, "восьми": 8,
    "девяти": 9, "десяти": 10, "одиннадцати": 11, "двенадцати": 12,
}
_NUM_RE = "|".join(sorted(_NUM_WORDS, key=len, reverse=True))
_ORD_RE = "|".join(sorted(_ORD_WORDS, key=len, reverse=True))
_DAT_RE = "|".join(sorted(_DAT_WORDS, key=len, reverse=True))
_PERIOD = "утра|дня|вечера|ночи"
_MONTHS = "|".join(RU_MONTHS_GEN)
_RE_HHMM = re.compile(r"(?<![\\d:])([01]?\\d|2[0-3]):([0-5]\\d)(?!\\d)")
_RE_POL = re.compile(rf"\\b(?:пол\\s*-?\\s*|половин\\w*\\s+)({_ORD_RE})(?:\\s+({_PERIOD}))?\\b")
_RE_NUM_PERIOD = re.compile(rf"\\b(\\d{{1,2}}|{_NUM_RE})\\s*(?:час\\w*\\s+)?({_PERIOD})\\b")
_RE_HOURS = re.compile(rf"\\bв\\s+(\\d{{1,2}})\\s+час(?:ов|а)?\\b(?!\\s+(?:{_PERIOD}))")
_RE_K = re.compile(rf"\\bк\\s+({_DAT_RE})(?:\\s+час\\w*)?(?:\\s+({_PERIOD}))?(?!\\s+(?:год|лет|рубл|человек|штук))\\b")
_RE_RANGE = re.compile(
    rf"\\bс\\s+(\\d{{1,2}})\\s+до\\s+(\\d{{1,2}})(?![\\d:])(?:\\s+({_PERIOD}))? "
    rf"(?!\\s+(?:человек|минут|руб|процент|{_MONTHS}))"
)
_OLD_MARK = re.compile(r"(?:вместо|а\\s+не|было|была|не\\s+в|не\\s+на)\\s*(?:в\\s+|на\\s+)?$")

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
        if not out or out[-1][2] != item[2]:
            out.append(item)
    return out

def find_clock_times(text: str) -> List[str]:
    return [x[2] for x in find_times_pos(text)]

def choose_times(text: str) -> Tuple[List[str], bool]:
    t = _norm(text)
    items = find_times_pos(text)
    new = [x[2] for x in items if not _OLD_MARK.search(t[max(0, x[0] - 14):x[0]])]
    had_old = len(new) < len(items)
    return (new or [x[2] for x in items]), had_old

_morph = None
def _get_morph():
    global _morph
    if _morph is None:
        import pymorphy3
        _morph = pymorphy3.MorphAnalyzer()
    return _morph

_CANCEL_LEMMAS = {"отменять", "отменить", "отмена"}
def cancel_hits(text: str) -> List[Tuple[int, bool]]:
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
_RE_NEG_ATTEND = re.compile(r"\bне\s+(?:смогу|могу|получится|выйдет|успею|успеваю|пойду|поеду|приду|приеду|подъеду)\b")
_RE_MOVE_PHRASE = re.compile(r"\b(?:перенеси|перенесите|перенесем|перенесу|сдвинь|сдвинем|подвинь|подвинем)\b")
_RE_MOVE_LEX = re.compile(r"перенес|перенос|сдвин|сдвиг|опозд|опазд|задерж|подвин|отлож|перекин")
_RE_LATE = re.compile(r"(?<!не)\b(?:опаздыва|опозда|задержива|задерж)\w*")
_RE_PAST = re.compile(r"\b(?:вчера|позавчера)\b")
_RE_FUTURE = re.compile(r"\b(?:завтра|послезавтра|сегодня|давай|будем|буду|каждый|каждую|каждые|следующ\w*)\b")
_RE_STATE_VERB = re.compile(r"\b(?:будет|был[аои]?|было|стало|станет|пройдет|закончится|выйдет|начнется)\b")

_UNITS = (r"(?:день|недел\w+|месяц|год|утро|вечер|ночь|выходн\w+|будн\w+|понедельник|вторник|"
          r"сред[ауы]|четверг|пятниц\w+|суббот\w+|воскресень\w+)")
_RE_RECURRENCE = re.compile(
    rf"\bкажд(?:ый|ую|ые|ое)\s+(?:(?:втор|трет)\w+\s+)?{_UNITS}\b"
    rf"|\bкажд(?:ые|ую)\s+(?:\d+|два|две|три|четыре|пять)\s+(?:дн\w+|недел\w+|месяц\w*)"
    r"|\bпо\s+(?:понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям|выходным|будням|утрам|вечерам)\b"
    r"|\b(?:ежедневно|еженедельно|ежемесячно)\b"
    r"|\bраз\s+в\s+(?:день|неделю|месяц|две\s+недели)\b"
)
_RE_WEEKDAY_ABBR = re.compile(
    r"\b(?:пн|вт|ср|чт|пт|сб|вс|понедельник\w*|вторник\w*|сред[ауыео]\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)\b"
)

def find_recurrence(text: str) -> Optional[str]:
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
    m = _RE_DUR.search(_norm(text))
    return _amount_minutes(m.group(1)) if m else None

def find_offset(text: str) -> Optional[int]:
    m = _RE_OFFSET.search(_norm(text))
    return _amount_minutes(m.group(1)) if m else None

def find_shift(text: str) -> Optional[int]:
    m = _RE_SHIFT.search(_norm(text))
    if not m:
        return None
    mins = _amount_minutes(m.group(1))
    return mins if m.group(2) in ("позже", "вперед") else -mins

_WD_FORMS = "понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье"
_RE_REL_DATE = re.compile(r"\b(послезавтра|завтра|сегодня)\b")
_RE_NEXT_WD = re.compile(rf"\b(?:в|во|на)\s+след(?:ующ\w+|.)\s*({_WD_FORMS}|среда|пятница|суббота)\b")
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
    t = _norm(text)
    m = re.search(r"\b(?:мск|msk)\s*([+-])\s*(\d{1,2})\b", t)
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

_NICK = {"паха", "леха", "антоха", "серега", "саня", "санек", "вован", "димон", "жека", "макс", "рома"}
_THEME_WORDS = {"тема", "теме", "тему", "темы", "темой"}

def _name_stem(name: str) -> str:
    n = _norm(name)
    return n[:-1] if n and n[-1] in "аяйь" else n

def _name_lemmas(word: str) -> set:
    res = set()
    for p in _get_morph().parse(word):
        tag = getattr(p, "tag", None)
        if tag is not None and "Name" in tag:
            res.add(p.normal_form.replace("ё", "е"))
    return res

def _canonical_name(tok: str, idx: int, cand: str) -> Optional[str]:
    w = _norm(tok)
    capitalized = idx > 0 and tok[:1].isupper() and tok[1:].islower()
    if w in _THEME_WORDS and not capitalized:
        return None
    parses = _get_morph().parse(tok)
    tags = [getattr(p, "tag", None) for p in parses]
    if all(t is None for t in tags):
        return cand[:1].upper() + cand[1:]
    for p, t in zip(parses, tags):
        if t is not None and "Name" in t:
            return p.normal_form.capitalize()
    for n in _NICK:
        s = n[:-1] if n[-1] in "аяй" else n
        if w.startswith(s) and len(w) <= len(s) + 3:
            return cand[:1].upper() + cand[1:]
    if capitalized:
        return cand[:1].upper() + cand[1:]
    return None

def capitalized_names(text: str) -> List[str]:
    out: List[str] = []
    for i, w in enumerate(re.findall(r"[А-ЯЁа-яё]+", text)):
        if i == 0 or not (w[0].isupper() and w[1:].islower()) or len(w) < 3:
            continue
        lem = _name_lemmas(w)
        if lem:
            out.append(sorted(lem)[0].capitalize())
    return out

def filter_attendees(text: str, candidates: List[str]) -> List[str]:
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

def postprocess(text: str, data: dict, context: Optional[List[str]] = None) -> ExtractedEvent:
    ev = ExtractedEvent.model_validate(data)
    t_norm = _norm(text)
    ctx = [c for c in (context or []) if isinstance(c, str)][-3:]
    ctx_text = " ".join(ctx)
    f_norm = _norm(" ".join([*ctx, text]))
    hits = cancel_hits(text)
    affirmed = [p for p, neg in hits if not neg]
    negated = [p for p, neg in hits if neg]
    cancel_phrase = bool(_RE_CANCEL_PHRASE.search(t_norm))
    move_m = _RE_MOVE_PHRASE.search(t_norm)
    shift = find_shift(text)
    new_times, had_old = choose_times(text)
    if ev.intent in (None, "create") and cancel_phrase:
        ev.intent = "cancel"
        ev.summary = ev.summary or "Встреча"
    elif ev.intent is None and (move_m or shift):
        ev.intent = "move"
        ev.summary = ev.summary or "Перенос встречи"
    elif ev.intent is None and find_offset(text) and not _RE_STATE_VERB.search(t_norm):
        ev.intent = "create"
        ev.summary = ev.summary or "Событие"
    elif ev.intent is None:
        return ev
    if _RE_PAST.search(t_norm) and not _RE_FUTURE.search(t_norm):
        ev.intent = None
        return ev
    if ev.intent == "cancel" and negated and not affirmed and not cancel_phrase:
        ev.intent = None
        return ev
    if affirmed and move_m and ev.intent in {"create", "move", "cancel"}:
        later_move = move_m.start() > max(affirmed)
        ev.intent = "move" if (later_move and (new_times or shift)) else "cancel"
    elif affirmed and ev.intent in {"create", "move"}:
        ev.intent = "move" if (move_m and move_m.start() > max(affirmed)) else "cancel"
    elif move_m and ev.intent == "create":
        ev.intent = "move"
    if shift and ev.intent == "create":
        ev.intent = "move"
    if ev.intent == "create" and (_RE_LATE.search(t_norm) or had_old):
        ev.intent = "move"
    if ev.intent == "move" and _RE_NEG_ATTEND.search(t_norm) and not new_times and not move_m and not shift:
        ev.intent = "cancel"
    if (ev.intent == "move" and not (_RE_MOVE_LEX.search(t_norm) or move_m or shift or had_old)
            and len(new_times) <= 1 and (ev.time or ev.date or ev.recurrence or new_times)):
        ev.intent = "create"
    if ev.intent in {"create", "move"}:
        off = find_offset(text)
        if off:
            ev.in_minutes = off
            ev.time = ev.end_time = ev.date = None
    if ev.intent == "move" and shift:
        ev.shift_minutes = shift
    times = [] if ev.in_minutes else new_times
    if not times and ctx and not ev.in_minutes:
        times, _ = choose_times(ctx_text)
    all_times = {x[2] for x in find_times_pos(" ".join([*ctx, text]))}
    if (ev.time or ev.end_time) and not (_RE_TIME_CUE.search(f_norm) or times):
        ev.time = ev.end_time = None
    if times:
        if ev.intent == "move":
            ev.time = times[-1]
        elif ev.time is None or ev.time not in all_times:
            ev.time = times[0]
        if (ev.intent == "create" and len(times) >= 2 and ev.end_time is None
                and _RE_INTERVAL_CUE.search(t_norm)):
            ev.end_time = times[1]
    if ev.end_time and not _RE_INTERVAL_CUE.search(f_norm):
        ev.end_time = None
    if ev.time and ev.end_time and ev.end_time <= ev.time:
        ev.end_time = None
    ev.time_ambiguous = False
    if ev.intent in {"create", "move"} and not ev.in_minutes and not find_times_pos(text):
        bm = _RE_BARE_HOUR.search(t_norm)
        if bm and 1 <= int(bm.group(1)) <= 23:
            n = int(bm.group(1))
            if ev.time is None:
                ev.time = f"{n + 12 if 1 <= n <= 6 else n:02d}:00"
            elif n <= 12 and int(ev.time[:2]) % 12 == n % 12:
                if 1 <= n <= 6:
                    ev.time = f"{n + 12:02d}:{ev.time[3:]}"
            ev.time_ambiguous = n <= 12
    rx = find_recurrence(" ".join([*ctx, text]))
    if rx:
        ev.recurrence = rx
        wd = _RE_WEEKDAY_ABBR.search(f_norm)
        if wd and wd.group(0)[:2] not in rx:
            ev.recurrence = f"{rx} {wd.group(0)}"
        ev.date = None
    else:
        ev.recurrence = None
        if ev.date and not (_RE_DATE_VALUE.search(_norm(ev.date)) and _RE_DATE_CUE.search(f_norm)):
            ev.date = None
        rel = find_relative_date(text) or (find_relative_date(ctx_text) if ctx else None)
        if rel and (ev.date is None or not _same_date(ev.date, rel)):
            ev.date = rel
    if ev.time and ev.end_time:
        t0 = datetime.strptime(ev.time, "%H:%M")
        t1 = datetime.strptime(ev.end_time, "%H:%M")
        minutes = int((t1 - t0).total_seconds() // 60)
        ev.duration_minutes = minutes if minutes > 0 else None
    else:
        ev.duration_minutes = find_duration(text) if ev.intent == "create" else None
    ev.timezone = find_timezone(text) or ev.timezone if False else find_timezone(text)
    ev.attendees = filter_attendees(text, ev.attendees)
    for n in capitalized_names(text):
        if not any(_name_stem(n)[:3] == _name_stem(x)[:3] for x in ev.attendees):
            ev.attendees.append(n)
    if ev.intent == "create" and not (ev.time or ev.date or ev.recurrence or ev.in_minutes):
        ev.intent = None
    return ev

def missing_fields(ev: ExtractedEvent) -> List[str]:
    if ev.intent != "create":
        return []
    miss = []
    if not (ev.date or ev.recurrence or ev.in_minutes):
        miss.append("date")
    if not (ev.time or ev.in_minutes):
        miss.append("time")
    return miss

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
Ответ: """

_model = None
_tokenizer = None
_load_lock = threading.Lock()
_gen_lock = threading.Lock()

def load_model():
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
    def f(m):
        mm = re.match(r"\s*(-?\d+)", m.group(2))
        return m.group(1) + (mm.group(1) if mm else "null")
    return re.sub(r'("(?:duration_minutes|in_minutes|shift_minutes)"\s*:\s*)([^,}\]]*)', f, s)

def _json_candidates(chunk: str):
    yield chunk
    yield _fix_numeric(chunk)
    fixed = re.sub(r",\s*([}\]])", r"\1", chunk)
    yield fixed
    yield re.sub(r'(["]\d]|null)\s+"(?=[a-z_]+"\s*:)', r'\1, "', fixed)
    if fixed.count("[") > fixed.count("]") and fixed.rstrip().endswith("}"):
        yield fixed.rstrip()[:-1] + "]}"
    yield fixed.replace("'", '"')
    f2 = fixed + "]" * max(fixed.count("[") - fixed.count("]"), 0)
    yield f2 + "}" * max(f2.count("{") - f2.count("}"), 0)

def parse_json(raw: str) -> Optional[dict]:
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
    return await asyncio.to_thread(extract_entities, text, context, message_time, tz)

_WD_IDX = [("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3),
           ("пятниц", 4), ("суббот", 5), ("воскресень", 6)]
_MONTH_NUM = {m: i + 1 for i, m in enumerate(RU_MONTHS_GEN)}

def _parse_date_phrase(phrase: Optional[str], today: _date) -> Tuple[Optional[_date], str]:
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
            if re.search(r"след", p):
                monday_next = today - timedelta(days=today.weekday()) + timedelta(days=7)
                return monday_next + timedelta(days=idx), "next"
            return today + timedelta(days=(idx - today.weekday()) % 7), "weekday"
    return None, "none"

def resolve_event_time(ev, message_time: Optional[datetime] = None, tz: str = DEFAULT_TZ) -> dict:
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
    if ev.date and d is None:
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
    if d is None:
        start = datetime.combine(base_src.date(), _dtime(h, m), tzinfo=src_zone)
        if start <= base_src:
            start += timedelta(days=1)
        return finish(start)
    start = datetime.combine(d, _dtime(h, m), tzinfo=src_zone)
    if start <= base_src:
        if kind == "weekday":
            start += timedelta(days=7)
        else:
            out["status"], out["reason"] = "needs_clarification", "date_in_past"
            return out
    return finish(start)

def resolve_datetime(ev, message_time: Optional[datetime] = None, tz: str = DEFAULT_TZ):
    r = resolve_event_time(ev, message_time, tz)
    return (r["start"], r["end"]) if r["status"] == "ok" else (None, None)

if __name__ == "__main__":
    print("✅ NLP-модуль Ани загружен. Функция extract_entities_async доступна.")
