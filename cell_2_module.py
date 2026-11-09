from __future__ import annotations
import argparse
import asyncio
import json
import logging
import re
import threading
import time as _time
from datetime import datetime, timedelta
from typing import Any, List, Optional
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("delu.nlp")
MODEL_NAME = "RaguTeam/RAGU-lm"
DEFAULT_TZ = "Europe/Moscow"
INTENTS = {"create", "move", "cancel"}

def normalize_hhmm(v: Any) -> Optional[str]:
    if not isinstance(v, str): return None
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", v)
    if not m: return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59: return None
    return f"{h:02d}:{mi:02d}"

class ExtractedEvent(BaseModel):
    intent: Optional[str] = None
    summary: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    end_time: Optional[str] = None
    duration_minutes: Optional[int] = None
    in_minutes: Optional[int] = None      # «через час» -> 60
    shift_minutes: Optional[int] = None   # «на час позже» -> +60, «на полчаса раньше» -> -30
    location: Optional[str] = None
    recurrence: Optional[str] = None
    attendees: List[str] = Field(default_factory=list)

    @field_validator("intent", mode="before")
    @classmethod
    def _intent(cls, v: Any) -> Optional[str]:
        return v if v in INTENTS else None

    @field_validator("time", "end_time", mode="before")
    @classmethod
    def _hhmm(cls, v: Any) -> Optional[str]:
        return normalize_hhmm(v)

    @field_validator("date", "summary", "location", "recurrence", mode="before")
    @classmethod
    def _text(cls, v: Any) -> Optional[str]:
        if not isinstance(v, str): return None
        v = v.strip()
        return None if v.lower() in {"", "null", "none"} else v

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def _duration(cls, v: Any) -> Optional[int]:
        try: n = int(v)
        except (TypeError, ValueError): return None
        return n if 0 < n <= 24 * 60 else None

    @field_validator("in_minutes", "shift_minutes", mode="before")
    @classmethod
    def _minutes(cls, v: Any) -> Optional[int]:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n != 0 and abs(n) <= 7 * 24 * 60 else None

    @field_validator("attendees", mode="before")
    @classmethod
    def _attendees(cls, v: Any) -> List[str]:
        if not isinstance(v, list): return []
        return [x.strip() for x in v if isinstance(x, str) and x.strip()]

_NUM_WORDS = {
    "час": 1, "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12
}
_ORD_WORDS = {
    "первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "пятого": 5,
    "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9, "десятого": 10,
    "одиннадцатого": 11, "двенадцатого": 12
}
_NUM_RE = "|".join(sorted(_NUM_WORDS, key=len, reverse=True))
_ORD_RE = "|".join(sorted(_ORD_WORDS, key=len, reverse=True))
_PERIOD = "утра|дня|вечера|ночи"

_RE_HHMM = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_RE_POL = re.compile(rf"\bпол\s*-?\s*({_ORD_RE})(?:\s+({_PERIOD}))?\b")
_RE_NUM_PERIOD = re.compile(rf"\b(\d{{1,2}}|{_NUM_RE})\s*(?:час\w*\s+)?({_PERIOD})\b")
_RE_HOURS = re.compile(rf"\bв\s+(\d{{1,2}})\s+час(?:ов|а)?\b(?!\s+(?:{_PERIOD}))")

_MONTHS = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
_RE_RANGE = re.compile(
    rf"\bс\s+(\d{{1,2}})\s+до\s+(\d{{1,2}})(?![\d:])(?:\s+({_PERIOD}))?"
    rf"(?!\s+(?:человек|минут|руб|процент|{_MONTHS}))"
)

def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")

def _apply_period(h: int, period: Optional[str]) -> int:
    if period is None or not 0 <= h <= 12: return h
    if period == "утра": return 0 if h == 12 else h
    if period == "дня": return h + 12 if 1 <= h <= 6 else h
    if period == "вечера": return h + 12 if 1 <= h <= 11 else h
    if period == "ночи": return 0 if h == 12 else (h + 12 if 6 <= h <= 11 else h)
    return h

def find_clock_times(text: str) -> List[str]:
    t = _norm(text)
    found: List[tuple] = []
    for m in _RE_HHMM.finditer(t):
        found.append((m.start(), f"{int(m.group(1)):02d}:{m.group(2)}"))
    for m in _RE_POL.finditer(t):
        n = _ORD_WORDS[m.group(1)]
        h = _apply_period(12 if n == 1 else n - 1, m.group(2))
        found.append((m.start(), f"{h:02d}:30"))
    for m in _RE_NUM_PERIOD.finditer(t):
        tok = m.group(1)
        h = int(tok) if tok.isdigit() else _NUM_WORDS[tok]
        if h > 23: continue
        found.append((m.start(), f"{_apply_period(h, m.group(2)):02d}:00"))
    for m in _RE_HOURS.finditer(t):
        h = int(m.group(1))
        if h <= 23: found.append((m.start(), f"{h:02d}:00"))
    for m in _RE_RANGE.finditer(t):
        a, b = int(m.group(1)), int(m.group(2))
        if a <= 23 and b <= 23 and b > a:
            found.append((m.start(), f"{_apply_period(a, m.group(3)):02d}:00"))
            found.append((m.start() + 1, f"{_apply_period(b, m.group(3)):02d}:00"))
    found.sort(key=lambda x: x[0])
    out: List[str] = []
    for _, x in found:
        if not out or out[-1] != x:   # «с 5 до 7 вечера» не должно давать двойное 19:00
            out.append(x)
    return out
_DUR_WORDS = {"два": 2, "три": 3, "четыре": 4, "пять": 5}
_RE_DUR = re.compile(
    r"\bна\s+(?:(полчаса)|(полтора\s+часа)|(час)(?!\w)|(\d{1,3})\s*(?:минут\w*|мин\b)"
    r"|(\d{1,2}|два|три|четыре|пять)\s+час(?:а|ов)?\b)(?!\s+(?:позже|раньше|вперед|назад))"
)

def find_duration(text: str) -> Optional[int]:
    """«на час» -> 60, «на полчаса» -> 30, «на 45 минут» -> 45, «на два часа» -> 120."""
    m = _RE_DUR.search(_norm(text))
    if not m:
        return None
    if m.group(1): return 30
    if m.group(2): return 90
    if m.group(3): return 60
    if m.group(4): return int(m.group(4))
    tok = m.group(5)
    return (int(tok) if tok.isdigit() else _DUR_WORDS[tok]) * 60

_morph = None
def _get_morph():
    global _morph
    if _morph is None:
        import pymorphy3
        _morph = pymorphy3.MorphAnalyzer()
    return _morph

_CANCEL_LEMMAS = {"отменять", "отменить", "отмена"}

def has_cancel_word(text: str) -> bool:
    morph = _get_morph()
    words = re.findall(r"[а-яё]+", text.lower())
    for i, w in enumerate(words):
        if morph.parse(w)[0].normal_form in _CANCEL_LEMMAS:
            if i > 0 and words[i - 1] == "не": continue
            return True
    return False

_RE_RECURRENCE = re.compile(r"кажд\w+\s+(?:(?:втор|трет)\w+\s+)?[а-я]+|по\s+(?:понедельник|вторник|сред|четверг|пятниц|суббот|воскресень)\w*")
_RE_REL_DATE = re.compile(r"\b(послезавтра|завтра|сегодня)\b")
_RE_WEEKDAY = re.compile(r"\bв\s+(?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\b")

_AMOUNT = (r"(полчаса|полтора\s+часа|час(?!\w)|\d{1,3}\s*(?:минут\w*|мин\b)"
           r"|(?:\d{1,2}|два|три|четыре|пять)\s+час(?:а|ов)?\b)")
_RE_OFFSET = re.compile(rf"\bчерез\s+{_AMOUNT}")
_RE_SHIFT = re.compile(rf"\bна\s+{_AMOUNT}\s+(позже|раньше|вперед|назад)\b")
_RE_MOVE_PHRASE = re.compile(r"\b(?:перенеси|перенесем|перенесите|сдвинь|сдвинем)\b")

def _amount_minutes(p: str) -> int:
    p = re.sub(r"\s+", " ", p.strip())
    if p.startswith("полчаса"): return 30
    if p.startswith("полтора"): return 90
    if p == "час": return 60
    m = re.match(r"(\d+)\s*мин", p)
    if m: return int(m.group(1))
    tok = p.split()[0]
    return (int(tok) if tok.isdigit() else _DUR_WORDS[tok]) * 60

def find_offset(text: str) -> Optional[int]:
    """«через час» -> 60, «через 2 часа» -> 120, «через полчаса» -> 30."""
    m = _RE_OFFSET.search(_norm(text))
    return _amount_minutes(m.group(1)) if m else None

def find_shift(text: str) -> Optional[int]:
    """«на час позже» -> +60, «на полчаса раньше» -> -30."""
    m = _RE_SHIFT.search(_norm(text))
    if not m:
        return None
    mins = _amount_minutes(m.group(1))
    return mins if m.group(2) in ("позже", "вперед") else -mins

_RE_CANCEL_PHRASE = re.compile(
    r"\bне\s+(?:смогу|могу|получится|успею|успеваю)\s+(?:прийти|приехать|подъехать|быть|поехать|встретиться)\b"
    r"|\bне\s+(?:приду|приеду|подъеду)\b"
)
_RE_REC_CUE = re.compile(
    r"кажд|ежедн|еженед|ежемес|раз\s+в\b|регулярно|постоянно"
    r"|\bпо\s+(?:понедельник|вторник|сред|четверг|пятниц|суббот|воскресень|выходн|будн|утрам|вечерам)"
)
_RE_DATE_CUE = re.compile(
    r"сегодня|завтра|послезавтра|понедельник|вторник|сред|четверг|пятниц|суббот|воскресень"
    r"|\b(?:пн|вт|ср|чт|пт|сб|вс)\b|январ|феврал|март|апрел|\bма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр"
    r"|\d|числ|недел|выходн|месяц|утр|вечер|ноч|дн[яеюёь]|обед"
)
_RE_TIME_CUE = re.compile(r"\d|час|утр|вечер|обед|полдень|полночь|ноч|дн[яеюёь]|минут")
_RE_PAST = re.compile(r"\b(?:вчера|позавчера)\b")
_RE_FUTURE = re.compile(r"\b(?:завтра|послезавтра|сегодня|давай|будем|буду|каждый|каждую|каждые|следующ\w*)\b")
_RE_WEEKDAY_ABBR = re.compile(
    r"\b(?:пн|вт|ср|чт|пт|сб|вс|понедельник\w*|вторник\w*|сред[ауыео]\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)\b"
)

def find_recurrence(text: str) -> Optional[str]:
    m = _RE_RECURRENCE.search(_norm(text))
    return m.group(0) if m else None

def find_relative_date(text: str) -> Optional[str]:
    t = _norm(text)
    m = _RE_REL_DATE.search(t) or _RE_WEEKDAY.search(t)
    return m.group(0) if m else None

def capitalized_names(text: str) -> List[str]:
    """Слова с заглавной буквы не в начале фразы, которые pymorphy3 считает именем."""
    morph = _get_morph()
    out: List[str] = []
    for i, w in enumerate(re.findall(r"[А-ЯЁа-яё]+", text)):
        if i == 0 or not (w[0].isupper() and w[1:].islower()) or len(w) < 3:
            continue
        for p in morph.parse(w):
            tag = getattr(p, "tag", None)
            if tag is not None and "Name" in tag:
                out.append(p.normal_form.capitalize())
                break
    return out

def _name_stem(name: str) -> str:
    n = name.lower().replace("ё", "е")
    return n[:-1] if n and n[-1] in "аяйь" else n

def filter_attendees(text: str, candidates: List[str]) -> List[str]:
    words = re.findall(r"[а-яa-z]+", text.lower().replace("ё", "е"))
    out: List[str] = []
    theme_triggers = {"теме", "тема", "тему", "темы", "темой"}
    for cand in candidates:
        stem = _name_stem(cand)
        if len(stem) < 3: continue
        if any(w.startswith(stem) and len(w) <= len(stem) + 2 for w in words):
            if cand.lower() == "тёма" and any(w in theme_triggers for w in words) and "теме" in text.lower(): continue
            if cand.lower() not in {x.lower() for x in out}: out.append(cand[:1].upper() + cand[1:])
    return out

def postprocess(text: str, data: dict) -> ExtractedEvent:
    ev = ExtractedEvent.model_validate(data)
    t_norm = _norm(text)
    if ev.intent in (None, "create") and _RE_CANCEL_PHRASE.search(t_norm):
        ev.intent = "cancel"                       # «не смогу прийти», «не приду»
        ev.summary = ev.summary or "Встреча"
    elif ev.intent is None and _RE_MOVE_PHRASE.search(t_norm):
        ev.intent = "move"                         # «перенеси встречу на час позже»
        ev.summary = ev.summary or "Перенос встречи"
    elif ev.intent is None:
        return ev
    if _RE_PAST.search(t_norm) and not _RE_FUTURE.search(t_norm):
        ev.intent = None                           # рассказ о прошлом, а не договорённость
        return ev
    if ev.intent in {"create", "move"} and has_cancel_word(text): ev.intent = "cancel"
    if re.search(r"\b(перенеси|перенесем|сдвинь|сдвинем|перенос|позже|раньше)\b", t_norm): ev.intent = "move"
    if ev.intent == "create" and re.search(r"(?<!не )\b(опаздыва|опозда|задержива|задерж)\w*", t_norm): ev.intent = "move"
    if ev.intent in {"create", "move"}:
        off = find_offset(text)
        if off: ev.in_minutes = off
    if ev.intent == "move":
        sh = find_shift(text)
        if sh: ev.shift_minutes = sh
    if ev.date and not _RE_DATE_CUE.search(t_norm):
        ev.date = None                             # LLM придумала дату, в тексте её нет
    if (ev.time or ev.end_time) and not (_RE_TIME_CUE.search(t_norm) or find_clock_times(text)):
        ev.time = ev.end_time = None               # то же для времени
    times = find_clock_times(text)
    if times:
        ev.time = times[-1] if ev.intent == "move" else times[0]
        if ev.intent == "create" and len(times) >= 2 and ev.end_time is None and re.search(r"\bдо\b", t_norm): ev.end_time = times[1]
    rx = find_recurrence(text)
    if rx:
        ev.recurrence = rx
        wd = _RE_WEEKDAY_ABBR.search(t_norm)
        if wd and wd.group(0)[:2] not in rx:      # «в пн каждую неделю» -> «каждую неделю пн»
            ev.recurrence = f"{rx} {wd.group(0)}"
    elif ev.recurrence and not _RE_REC_CUE.search(t_norm):
        ev.recurrence = None                       # LLM придумала периодичность, в тексте её нет
    if ev.recurrence:
        ev.date = None
    else:
        rel = find_relative_date(text)
        if rel: ev.date = rel          # «послезавтра», «в пятницу» надёжнее, чем ответ LLM
    if ev.intent == "create" and ev.end_time is None:
        d = find_duration(text)
        if d: ev.duration_minutes = d
    if ev.time and ev.end_time:   # интервал из текста точнее, чем длительность от LLM
        t0 = datetime.strptime(ev.time, "%H:%M")
        t1 = datetime.strptime(ev.end_time, "%H:%M")
        minutes = int((t1 - t0).total_seconds() // 60)
        if minutes > 0: ev.duration_minutes = minutes
    ev.attendees = filter_attendees(text, ev.attendees)
    for n in capitalized_names(text):
        if not any(_name_stem(n)[:3] == _name_stem(x)[:3] for x in ev.attendees):
            ev.attendees.append(n)
    if ev.intent == "create":   # событие без единого указания на время — не договорённость
        if not (ev.time or ev.date or ev.recurrence or ev.in_minutes): ev.intent = None
    return ev

def missing_fields(ev: ExtractedEvent) -> List[str]:
    if ev.intent != "create": return []
    miss = []
    if not (ev.date or ev.recurrence or ev.in_minutes): miss.append("date")
    if not (ev.time or ev.in_minutes): miss.append("time")
    return miss

PROMPT = """Ты — NLP-модуль ассистента «ДЕЛУ.ВРЕМЯ». Твоя главная задача — определить, есть ли в тексте договорённость о встрече или деле на БУДУЩЕЕ. Верни СТРОГО один JSON-объект. Если параметра нет — null.
КРИТИЧЕСКИЕ ПРАВИЛА:
1. Если в тексте НЕТ конкретного намерения встретиться, созвониться или сделать что-то в будущем (например, это просто приветствие, пустой вопрос, обсуждение текущей темы или рассказ о прошлом), ты ОБЯЗАН вернуть intent: null и все остальные поля null!
2. НЕ придумывай end_time и duration_minutes, если их нет в тексте! Задавай их в null, если не указано точное время окончания (например, "до 18:00").
3. recurrence: заполняй ТОЛЬКО если действие регулярно повторяется ("каждую неделю", "каждые выходные"). Если фраза разовая — recurrence = null.
Формат: {"intent": ..., "summary": ..., "date": ..., "time": ..., "end_time": ..., "duration_minutes": ..., "location": ..., "recurrence": ..., "attendees": []}
Текст: "привет как дела что делаешь"
Ответ: {"intent": null, "summary": null, "date": null, "time": null, "end_time": null, "duration_minutes": null, "location": null, "recurrence": null, "attendees": []}
Текст: "слушай я тут задерживаюсь буду на встрече с пахой только в пол седьмого вечера"
Ответ: {"intent": "move", "summary": "Встреча", "date": null, "time": "18:30", "end_time": null, "duration_minutes": null, "location": null, "recurrence": null, "attendees": ["Паха"]}
Текст: "мы решили в зальчик с серегой каждые выходные ходить к двум дня"
Ответ: {"intent": "create", "summary": "Поход в спортзал", "date": null, "time": "14:00", "end_time": null, "duration_minutes": null, "location": "зальчик", "recurrence": "каждые выходные", "attendees": ["Серега"]}
Текст: "<<TEXT>>"
Ответ:"""

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
            _tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
            _model = AutoModelForCausalLM.from_pretrained(
                MODEL_NAME, torch_dtype=torch.float16 if use_cuda else torch.float32, device_map="auto" if use_cuda else None
            )
            _model.eval()
    return _model, _tokenizer

def _generate(text: str) -> str:
    import torch
    model, tok = load_model()
    prompt = PROMPT.replace("<<TEXT>>", text.replace('"', "'"))
    inputs = tok(prompt, return_tensors="pt").to(model.device)
    with _gen_lock, torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=160, do_sample=False, pad_token_id=tok.eos_token_id, stop_strings=["}"], tokenizer=tok)
    return tok.decode(out[0, inputs.input_ids.shape[-1]:], skip_special_tokens=True)

def _json_candidates(chunk: str):
    yield chunk
    fixed = re.sub(r",\s*([}\]])", r"\1", chunk)                              # лишняя запятая в конце
    yield fixed
    yield re.sub(r'(["\]\d]|null)\s+"(?=[a-z_]+"\s*:)', r'\1, "', fixed)         # пропущена запятая между полями
    yield fixed.replace("'", '"')                                              # одинарные кавычки
    f2 = fixed + "]" * max(fixed.count("[") - fixed.count("]"), 0)
    yield f2 + "}" * max(f2.count("{") - f2.count("}"), 0)                     # обрыв по длине

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

def _result(status: str, event: Optional[ExtractedEvent] = None, error: Optional[str] = None, t0: float = 0.0, raw: Optional[str] = None) -> dict:
    return {**({"raw": raw} if raw is not None else {}),
        "status": status, "event": event.model_dump() if event else None,
        "missing": missing_fields(event) if event else [], "error": error, "latency_s": round(_time.perf_counter() - t0, 3)
    }

def extract_entities(text: str) -> dict:
    t0 = _time.perf_counter()
    try:
        raw = _generate(text)
    except Exception:
        log.exception("generation failed")
        return _result("error", error="generation_failed", t0=t0)
    data = parse_json(raw)
    if data is None:
        log.warning("bad json from model: %r", raw)
        return _result("error", error="bad_json", t0=t0, raw=raw)
    try:
        ev = postprocess(text, data)
    except Exception:
        log.exception("postprocess failed, model output: %r", data)
        return _result("error", error="postprocess_failed", t0=t0)
    if ev.intent is None: status = "no_event"
    else: status = "needs_clarification" if missing_fields(ev) else "ok"
    return _result(status, ev if ev.intent else None, t0=t0)

async def extract_entities_async(text: str) -> dict:
    return await asyncio.to_thread(extract_entities, text)

def resolve_datetime(ev: dict | ExtractedEvent, message_time: Optional[datetime] = None, tz: str = DEFAULT_TZ):
    import dateparser
    if isinstance(ev, dict): ev = ExtractedEvent.model_validate(ev)
    if ev.recurrence or not (ev.time or ev.in_minutes): return None, None
    zone = ZoneInfo(tz)
    base = message_time or datetime.now(zone)
    base = base.replace(tzinfo=zone) if base.tzinfo is None else base.astimezone(zone)
    start = (base + timedelta(minutes=ev.in_minutes)) if ev.in_minutes else dateparser.parse(f"{ev.date or 'сегодня'} {ev.time}", languages=["ru"], settings={"RELATIVE_BASE": base, "PREFER_DATES_FROM": "future", "TIMEZONE": tz, "RETURN_AS_TIMEZONE_AWARE": True})
    if start is None: return None, None
    end = None
    if ev.duration_minutes: end = start + timedelta(minutes=ev.duration_minutes)
    elif ev.end_time:
        h, m = map(int, ev.end_time.split(":"))
        end = start.replace(hour=h, minute=m)
        if end <= start: end += timedelta(days=1)
    return start, end

def selftest(with_morph: bool = True) -> None:
    print("Запуск встроенных тестов...")
    ft = find_clock_times
    assert ft("встретимся в 17:30") == ["17:30"]
    assert ft("в пол седьмого вечера") == ["18:30"]
    assert ft("в 5 вечера") == ["17:00"]
    assert normalize_hhmm("9:00") == "09:00" and normalize_hhmm("25:00") is None
    assert ft("нас в 20 человек") == []
    assert ft("в пол десятого вечера") == ["21:30"]
    assert ft("в 5 часов вечера") == ["17:00"]
    assert ft("с 16:00 до 17:00") == ["16:00", "17:00"]
    assert find_recurrence("каждую неделю в пн") == "каждую неделю"
    assert find_recurrence("завтра в 15:00") is None
    assert find_relative_date("давай послезавтра") == "послезавтра"
    assert parse_json('  {"intent": null} лишний текст') == {"intent": None}
    assert parse_json("нет json") is None
    if with_morph:
        base = {"intent": "create", "summary": "Встреча", "time": "19:00"}
        ev = postprocess("давай завтра в 17:30 кофе", base)
        assert ev.time == "17:30" and ev.date == "завтра", ev
        ev = postprocess("в пн с 16:00 до 17:30 тренировка", base)
        assert (ev.time, ev.end_time, ev.duration_minutes) == ("16:00", "17:30", 90), ev
        ev = postprocess("было в 15:00, давай в 16:00", {**base, "intent": "move"})
        assert ev.time == "16:00", ev
        ev = postprocess("каждый четверг в 21:00", {**base, "date": "сегодня"})
        assert ev.date is None and ev.recurrence == "каждый четверг", ev
        assert postprocess("привет в 17:30", {"intent": None}).intent is None
        assert not has_cancel_word("спасибо, до завтра")
        assert has_cancel_word("отменяй все встречи")
        assert not has_cancel_word("не отменяй встречу")
    assert ft("с 16 до 17") == ["16:00", "17:00"]
    assert ft("с 5 до 7 вечера") == ["17:00", "19:00"]
    assert ft("с 16:00 до 17:30") == ["16:00", "17:30"]
    assert ft("нас с 10 до 15 человек") == []
    assert ft("отпуск с 5 до 7 октября") == []
    assert find_duration("созвон на час") == 60
    assert find_offset("через час созвон") == 60 and find_offset("через 2 часа") == 120
    assert find_offset("через полчаса") == 30 and find_offset("через 45 минут") == 45
    assert find_shift("перенеси встречу на час позже") == 60
    assert find_shift("на полчаса раньше") == -30 and find_shift("созвон на час") is None
    assert find_duration("на полчаса") == 30
    assert find_duration("на 45 минут") == 45
    assert find_duration("на два часа") == 120
    assert find_duration("перенеси на час позже") is None
    if with_morph:
        b = {"intent": "create", "summary": "Встреча", "time": "19:00"}
        assert postprocess("увидимся послезавтра в 19:30", {**b, "date": "последний день"}).date == "послезавтра"
        ev = postprocess("я опаздываю буду в 19:15", b)
        assert (ev.intent, ev.time) == ("move", "19:15"), ev
        ev = postprocess("каждую неделю в пн с 16 до 17 йога", {**b, "time": None})
        assert (ev.time, ev.end_time, ev.duration_minutes) == ("16:00", "17:00", 60), ev
        assert ev.recurrence == "каждую неделю пн", ev
        assert postprocess("созвон завтра в 16:00 на час", b).duration_minutes == 60
        assert postprocess("не опаздывай, завтра в 10:00", b).intent == "create"
        r = postprocess("в пн каждую неделю планерка в 11:00", {**b, "recurrence": "в каждый день", "time": "11:00"})
        assert r.recurrence == "каждую неделю пн" and r.date is None, r
        assert postprocess("каждый четверг в 21:00", b).recurrence == "каждый четверг"
        assert postprocess("созвон завтра в 16:00 на час", {**b, "duration_minutes": 1}).duration_minutes == 60
        assert postprocess("не смогу прийти давай в другой раз", {"intent": None}).intent == "cancel"
        assert postprocess("вчера ходил в кино в 8 вечера было круто", b).intent is None
        assert postprocess("вчера договорились, завтра в 8 вечера кино", b).intent == "create"
        assert postprocess("в очереди было человек 20", {"intent": None}).intent is None
        assert postprocess("через час созвон", {"intent": "create", "summary": "Созвон"}).in_minutes == 60
        r = postprocess("перенеси встречу на час позже", {"intent": None})
        assert (r.intent, r.shift_minutes) == ("move", 60), r
        assert postprocess("не смогу прийти давай в другой раз", {"intent": "create", "summary": "x"}).intent == "cancel"
        assert postprocess("с 18:00 до 19:30 тренировка сегодня", {**b, "time": "18:00", "duration_minutes": 120}).duration_minutes == 90
        assert postprocess("пришли мне документы", {"intent": "create", "summary": "Документы"}).intent is None
        r = postprocess("пришли мне документы", {"intent": "create", "summary": "Документы", "date": "сегодня",
                                                 "time": "15:00", "recurrence": "каждый день"})
        assert r.intent is None, r
        r = postprocess("увидимся послезавтра в пол восьмого вечера", {**b, "recurrence": "каждый день", "time": "19:30"})
        assert (r.date, r.recurrence, r.time) == ("послезавтра", None, "19:30"), r
        assert parse_json('{"intent": "create", "time": "15:00" "summary": "x"}') == {"intent": "create", "time": "15:00", "summary": "x"}
        assert parse_json('{"intent": "create", "attendees": ["Паха", "Макс"') == {"intent": "create", "attendees": ["Паха", "Макс"]}
        print("имена с заглавной (инфо):", capitalized_names("в 5 вечера кофе с Леной"))
    print("Внутренний selftest: OK" + ("" if with_morph else " (без проверок pymorphy3)"))


# --- компактный промпт (заменяет PROMPT из модуля) ---
PROMPT = """Ты — NLP-модуль ассистента «ДЕЛУ.ВРЕМЯ». Определи, есть ли в тексте договорённость о встрече или деле на БУДУЩЕЕ, и верни ОДИН JSON. Поля без значения НЕ пиши.
Поля: intent (create | move | cancel | null), summary (1-3 слова), date, time (HH:MM), end_time, duration_minutes, location, recurrence, attendees (имена людей в начальной форме).
create — новая встреча или дело; move — перенос или опоздание на УЖЕ назначенное; cancel — отмена, «не приду».
Нет планов на будущее (болтовня, вопрос, рассказ о прошлом) — верни {"intent": null}.
end_time и duration_minutes — только если названы в тексте; recurrence — только для регулярных событий.
Текст: "Привет давай в 15:00 пересечемся в кафе посидим"
Ответ: {"intent": "create", "summary": "Посидеть в кафе", "date": "сегодня", "time": "15:00", "location": "кафе"}
Текст: "завтра в 18:00 футбол с Димой и Сашей на стадионе"
Ответ: {"intent": "create", "summary": "Футбол", "date": "завтра", "time": "18:00", "location": "стадион", "attendees": ["Дима", "Саша"]}
Текст: "ужин с Ирой в субботу в 19:00"
Ответ: {"intent": "create", "summary": "Ужин", "date": "в субботу", "time": "19:00", "attendees": ["Ира"]}
Текст: "теперь созваниваемся по средам с 19:00 до 20:30"
Ответ: {"intent": "create", "summary": "Созвон", "time": "19:00", "end_time": "20:30", "duration_minutes": 90, "recurrence": "по средам"}
Текст: "задержусь, буду на месте на полчаса позже"
Ответ: {"intent": "move", "summary": "Опоздание"}
Текст: "я сегодня не смогу приехать встретиться у меня не получается"
Ответ: {"intent": "cancel", "summary": "Встреча", "date": "сегодня"}
Текст: "сегодня не получится прийти на тренировку, давай на следующей неделе"
Ответ: {"intent": "cancel", "summary": "Тренировка", "date": "сегодня"}
Текст: "через 2 часа созвонимся"
Ответ: {"intent": "create", "summary": "Созвон"}
Текст: "перенеси встречу на 20 минут раньше"
Ответ: {"intent": "move", "summary": "Перенос встречи"}
Текст: "скинь мне файл с отчётом"
Ответ: {"intent": null}
Текст: "ну как дела, что делаешь"
Ответ: {"intent": null}
Текст: "я вчера был в магазине, купил хлеб"
Ответ: {"intent": null}
Текст: "<<TEXT>>"
Ответ:"""
