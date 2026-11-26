from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import bot, logger
from database.models import edit_user, get_business_chats, get_buss_conn, has_active_consent, notifications_enabled

router = Router(name="settings")
PAGE_SIZE = 10
SETTINGS_TITLE = "<b>Настройки</b>"
CHAT_ACCESS_HINT = "Текущий доступ к чатам можно посмотреть в Telegram Business → Чат-боты."


def settings_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="Уведомления", callback_data="settings:notifications")
    keyboard.button(text="Доступ к чатам", callback_data="settings:chats:0")
    keyboard.adjust(1)
    return keyboard.as_markup()


def notification_screen(enabled: bool) -> tuple[str, InlineKeyboardMarkup]:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text="Выключить" if enabled else "Включить",
        callback_data="settings:notifications:off" if enabled else "settings:notifications:on",
    )
    keyboard.button(text="Назад", callback_data="settings:main")
    keyboard.adjust(1)
    status = "включены" if enabled else "выключены"
    text = (f"Уведомления {status}.\n\n"
            "Когда настройка выключена, бот пропускает отдельные уведомления и присылает карточки событий без звука.")
    return text, keyboard.as_markup()


async def chat_access_screen(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="Открыть Telegram Business", url="tg://settings/business")
    keyboard.button(text="Назад", callback_data="settings:main")
    keyboard.adjust(1)
    connection = await get_buss_conn(user_id)
    if connection is None or not connection.business_token:
        return f"Telegram Business пока не подключён.\n\n{CHAT_ACCESS_HINT}", keyboard.as_markup()
    try:
        business = await bot.get_business_connection(business_connection_id=connection.business_token)
    except TelegramAPIError as error:
        logger.info("Не удалось проверить доступ Telegram Business (%s)", type(error).__name__)
        return f"Не удалось проверить подключение.\n\n{CHAT_ACCESS_HINT}", keyboard.as_markup()
    if not business.is_enabled or business.user.id != user_id:
        return f"Подключение Telegram Business отключено.\n\n{CHAT_ACCESS_HINT}", keyboard.as_markup()

    chats = await get_business_chats(user_id, offset=page * PAGE_SIZE, limit=PAGE_SIZE + 1)
    if chats is None:
        return f"Не удалось загрузить чаты.\n\n{CHAT_ACCESS_HINT}", keyboard.as_markup()
    if page:
        keyboard.button(text="Предыдущая страница", callback_data=f"settings:chats:{page - 1}")
    if len(chats) > PAGE_SIZE:
        keyboard.button(text="Следующая страница", callback_data=f"settings:chats:{page + 1}")
    keyboard.adjust(1)
    text = "<b>Чаты, из которых бот получал сообщения</b>"
    if chats:
        names = [escape((chat.chat_title or f"Чат {chat.chat_id}")[:120]) for chat in chats[:PAGE_SIZE]]
        text += "\n\n" + "\n".join(f"• {name}" for name in names)
    else:
        text += "\n\nНа этой странице пока нет известных чатов."
    return f"{text}\n\n{CHAT_ACCESS_HINT}", keyboard.as_markup()


@router.message(Command("settings"), F.chat.type == "private")
async def command_settings(msg: Message) -> None:
    if not await has_active_consent(msg.from_user.id):
        await msg.answer("Сначала завершите настройку через /start.")
        return
    await msg.answer(SETTINGS_TITLE, reply_markup=settings_keyboard())


@router.callback_query(F.data.startswith("settings:"))
async def settings_action(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    msg = call.message
    if not isinstance(msg, Message) or msg.chat.type != "private" or msg.chat.id != user_id:
        await call.answer("Откройте /settings в личном чате с ботом.", show_alert=True)
        return
    if not await has_active_consent(user_id):
        await call.answer("Сначала завершите настройку через /start.", show_alert=True)
        return

    action = call.data.removeprefix("settings:")
    if action in {"notifications:on", "notifications:off"}:
        if not await edit_user(user_id, notification=action.endswith(":on")):
            await call.answer("Не удалось сохранить настройку.", show_alert=True)
            return
        action = "notifications"

    await call.answer()
    if action == "main":
        text, keyboard = SETTINGS_TITLE, settings_keyboard()
    elif action == "notifications":
        text, keyboard = notification_screen(await notifications_enabled(user_id))
    elif action.startswith("chats:"):
        page = action.removeprefix("chats:")
        if not (page.isascii() and page.isdecimal() and len(page) <= 6):
            return
        text, keyboard = await chat_access_screen(user_id, int(page))
    else:
        return

    try:
        await msg.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error):
            raise
