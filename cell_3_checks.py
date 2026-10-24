"""
Проверка NLP-модуля по пунктам ТЗ «ДЕЛУ.ВРЕМЯ».

Как пользоваться (Colab, после того как определена extract_entities):
    <вставить этот файл в ячейку и выполнить>
    run_checks(extract_entities)               # все случаи
    run_checks(extract_entities, limit=6)      # быстрый прогон на CPU (каждый вызов ~30 с)
    run_checks(extract_entities, tags=["нет события"])   # только одна группа

ВАЖНО: это набор для РАЗРАБОТКИ (чтобы видеть, что чинить). Для итоговой цифры
«точность >= 90%» нужна отдельная выборка 150-300 сообщений, которую вы не использовали
при настройке промпта и правил.
"""
import re

# expect: no_event | intent | date | time | end_time | duration_minutes |
#         recurrence_has (любое из слов) | no_recurrence | attendees (набор имён)
CASES = [
    # --- разовые события: «завтра в 15:00», «встреча с [имя] в [время]» ---
    ("разовое", "давай созвонимся завтра в 15:00",
     {"intent": "create", "date": "завтра", "time": "15:00"}),
    ("разовое", "встреча с Иваном в 14:30",
     {"intent": "create", "time": "14:30", "attendees": {"иван"}}),
    ("разовое", "в пятницу в 10:00 у меня стоматолог",
     {"intent": "create", "date": "пятницу", "time": "10:00"}),
    ("разовое", "увидимся послезавтра в пол восьмого вечера",
     {"intent": "create", "date": "послезавтра", "time": "19:30"}),
    ("разовое", "в 5 вечера кофе с Леной",
     {"intent": "create", "time": "17:00", "attendees": {"лена"}}),
    ("разовое", "в субботу в 15:00 футбол с Пахой и Максом",
     {"intent": "create", "time": "15:00", "attendees": {"паха", "макс"}}),
    ("разовое", "через час созвон",                       # «через час» есть в ТЗ
     {"intent": "create"}),
    # --- повторяющиеся (F-B-10) ---
    ("повтор", "в пн каждую неделю планерка в 11:00",
     {"intent": "create", "time": "11:00", "recurrence_has": ["пн", "понедельник"]}),
    ("повтор", "каждую неделю в пн с 16 до 17 йога",     # пример из F-B-10
     {"intent": "create", "time": "16:00", "end_time": "17:00",
      "recurrence_has": ["пн", "понедельник"]}),
    ("повтор", "по субботам в 12:00 бассейн",
     {"intent": "create", "time": "12:00", "recurrence_has": ["суббот"]}),
    ("повтор", "завтра в 12:00 бассейн",                  # разовое не должно стать серией
     {"intent": "create", "time": "12:00", "no_recurrence": True}),
    # --- длительность ---
    ("длительность", "созвон завтра в 16:00 на час",
     {"intent": "create", "time": "16:00", "duration_minutes": 60}),
    ("длительность", "с 18:00 до 19:30 тренировка сегодня",
     {"intent": "create", "time": "18:00", "end_time": "19:30", "duration_minutes": 90}),
    # --- перенос (F-B-17) ---
    ("перенос", "перенеси встречу на час позже", {"intent": "move"}),
    ("перенос", "давай перенесём на четверг на 12:00",
     {"intent": "move", "time": "12:00"}),
    ("перенос", "я опаздываю буду в 19:15",
     {"intent": "move", "time": "19:15"}),
    # --- отмена (F-B-16, F-B-18) ---
    ("отмена", "отмени встречу завтра", {"intent": "cancel", "date": "завтра"}),
    ("отмена", "не смогу прийти давай в другой раз", {"intent": "cancel"}),
    ("отмена", "отмени все встречи по пятницам",
     {"intent": "cancel", "recurrence_has": ["пятниц"]}),
    # --- голос после STT: без пунктуации, слова-паразиты ---
    ("голос (STT)", "ээ ну короче давай это завтра часиков в восемь вечера в кафе",
     {"intent": "create", "date": "завтра", "time": "20:00"}),
    # --- НЕТ события (NF-19: ложные срабатывания) ---
    ("нет события", "ну как дела что нового", {"no_event": True}),
    ("нет события", "вчера ходил в кино в 8 вечера было круто", {"no_event": True}),
    ("нет события", "спасибо до связи", {"no_event": True}),
    ("нет события", "в очереди было человек 20", {"no_event": True}),
    ("нет события", "пришли мне документы", {"no_event": True}),
]


def _norm_date(d):
    d = (d or "").lower().strip()
    return re.sub(r"^(в|во|на)\s+", "", d)


def _check(exp, res):
    """Список расхождений (пустой — случай пройден)."""
    ev = res.get("event") or {}
    if exp.get("no_event"):
        return [] if res["status"] == "no_event" else [f"ложное срабатывание: {res['status']}"]
    if res["status"] in ("no_event", "error"):
        extra = f" | ответ модели: {res['raw']!r}" if res.get("raw") else ""
        return [f"статус {res['status']} ({res.get('error')}){extra}"]
    errs = []
    for k in ("intent", "time", "end_time", "duration_minutes"):
        if k in exp and ev.get(k) != exp[k]:
            errs.append(f"{k}: ждали {exp[k]!r}, получили {ev.get(k)!r}")
    if "date" in exp and _norm_date(ev.get("date")) != _norm_date(exp["date"]):
        errs.append(f"date: ждали {exp['date']!r}, получили {ev.get('date')!r}")
    if "recurrence_has" in exp:
        r = (ev.get("recurrence") or "").lower()
        if not any(w in r for w in exp["recurrence_has"]):
            errs.append(f"recurrence: ждали что-то из {exp['recurrence_has']}, получили {ev.get('recurrence')!r}")
    if exp.get("no_recurrence") and ev.get("recurrence"):
        errs.append(f"recurrence: ждали null, получили {ev.get('recurrence')!r}")
    if "attendees" in exp:
        got = {a.lower() for a in ev.get("attendees", [])}
        if got != exp["attendees"]:
            errs.append(f"attendees: ждали {sorted(exp['attendees'])}, получили {sorted(got)}")
    return errs


LAST = {}  # результат последнего прогона, его читает итоговый отчёт


def _pick(cases, limit):
    """limit: берём случаи по кругу из каждой группы, чтобы быстрый прогон
    покрывал все пункты ТЗ, а не только первые по списку."""
    groups = {}
    for c in cases:
        groups.setdefault(c[0], []).append(c)
    out, i = [], 0
    while len(out) < limit and any(i < len(g) for g in groups.values()):
        for g in groups.values():
            if i < len(g) and len(out) < limit:
                out.append(g[i])
        i += 1
    return out


def run_checks(extract, limit=None, tags=None):
    cases = [c for c in CASES if not tags or c[0] in tags]
    if limit:
        cases = _pick(cases, limit)
    by_tag, lat, fails, fail_details = {}, [], [], []
    for tag, text, exp in cases:
        res = extract(text)
        errs = _check(exp, res)
        lat.append(res.get("latency_s", 0))
        by_tag.setdefault(tag, []).append(not errs)
        print(("PASS  " if not errs else "FAIL  ") + f"[{tag}] {text}")
        for e in errs:
            print("        -", e)
        if errs:
            fails.append(text)
            fail_details.append((tag, text, errs))
    ok = sum(sum(v) for v in by_tag.values())
    total = sum(len(v) for v in by_tag.values())
    print("\n=== ИТОГ ===")
    for tag, v in by_tag.items():
        print(f"{tag:14s} {sum(v)}/{len(v)}")
    print(f"ВСЕГО          {ok}/{total} = {100 * ok / max(total, 1):.0f}%  (цель ТЗ: >= 90%)")
    neg = by_tag.get("нет события")
    if neg:
        print(f"ложные срабатывания: {len(neg) - sum(neg)} из {len(neg)}")
    if lat:
        print(f"время на сообщение: среднее {sum(lat) / len(lat):.1f} с, максимум {max(lat):.1f} с (цель ТЗ: <= 10 с)")
    LAST.clear()
    LAST.update(
        ok=ok, total=total, pct=100 * ok / max(total, 1),
        false_pos=(len(neg) - sum(neg)) if neg else 0, neg_total=len(neg) if neg else 0,
        by_tag={t: (sum(v), len(v)) for t, v in by_tag.items()},
        fails=fail_details,
        lat_avg=(sum(lat) / len(lat)) if lat else None, lat_max=max(lat) if lat else None,
    )
    return fails
