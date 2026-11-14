from aiogram.utils.keyboard import InlineKeyboardBuilder

def google_conn_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Подключить google-календарь", callback_data="google_connect")

    return kb.as_markup()