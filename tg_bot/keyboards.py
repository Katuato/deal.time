from aiogram.utils.keyboard import InlineKeyboardBuilder

def google_conn_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Подключить google-календарь", callback_data="google_connect")

    return kb.as_markup()


# Часовые пояса России: подпись на кнопке -> IANA-название (его понимают и бот, и Google Календарь)
TIMEZONES = [
    ("Калининград (UTC+2)", "Europe/Kaliningrad"),
    ("Москва (UTC+3)", "Europe/Moscow"),
    ("Самара (UTC+4)", "Europe/Samara"),
    ("Екатеринбург (UTC+5)", "Asia/Yekaterinburg"),
    ("Омск (UTC+6)", "Asia/Omsk"),
    ("Новосибирск, Красноярск (UTC+7)", "Asia/Krasnoyarsk"),
    ("Иркутск (UTC+8)", "Asia/Irkutsk"),
    ("Якутск (UTC+9)", "Asia/Yakutsk"),
    ("Владивосток (UTC+10)", "Asia/Vladivostok"),
    ("Магадан (UTC+11)", "Asia/Magadan"),
    ("Камчатка (UTC+12)", "Asia/Kamchatka"),
]


def timezone_kb():
    kb = InlineKeyboardBuilder()
    for title, tz in TIMEZONES:
        kb.button(text=title, callback_data=f"tz:{tz}")
    kb.button(text="🌍 Другой", callback_data="tz:other")
    kb.adjust(1)
    return kb.as_markup()
