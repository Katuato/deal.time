def build_rrule(freq: str, interval: int = 1, byday: list[str] = None, count: int = None, until: str = None) -> str:
    """
    Преобразует параметры периодичности в строку стандарта RRULE

    :param freq: Частота ('DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY')
    :param interval: Интервал (например, 1 — каждую неделю, 2 — раз в две недели)
    :param byday: Список дней недели, например ['MO', 'WE', 'FR']
    :param count: Количество повторений (например, 10 раз)
    :param until: Дата окончания в формате 'YYYYMMDD' (например, '20261231')
    :return: Готовая строка RRULE для Google Calendar API
    """
    # Начинаем собирать части правила в список
    parts = [f"FREQ={freq.upper()}"]

    if interval and interval > 1:
        parts.append(f"INTERVAL={interval}")

    if byday:
        # Соединяем дни недели через запятую: ['MO', 'WE'] -> 'MO,WE'
        days_str = ",".join(day.upper() for day in byday)
        parts.append(f"BYDAY={days_str}")

    if count:
        parts.append(f"COUNT={count}")
    elif until:
        # В стандарте RFC 5545 к дате окончания нужно добавить время конца дня в UTC
        parts.append(f"UNTIL={until}T235959Z")

    # Склеиваем все части через точку с запятой
    rrule_string = "RRULE:" + ";".join(parts)
    return rrule_string


# Блок для быстрой проверки работы файла
if __name__ == "__main__":
    # Пример из ТЗ: «каждую неделю в пн»
    test_rule = build_rrule(freq="WEEKLY", byday=["MO"])
    print("Результат для понедельников:", test_rule)
    # Выведет: RRULE:FREQ=WEEKLY;BYDAY=MO