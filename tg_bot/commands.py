"""Команды бота: /help, /history, меню команд и кнопка Mini App (F-B-06, F-B-15, задача #805).

/settings реализован отдельно в tg_bot/settings.py (уведомления и доступ к чатам).
"""
import html
import os

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (BotCommand, CallbackQuery, MenuButtonCommands, MenuButtonWebApp,
                           Message, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import bot, logger
from database.models import get_event, get_user_events, has_active_consent

cm = Router()

# https-адрес Mini App. Его нужно взять у Елизара и положить в .env как MINI_APP_URL.
# Пока переменной нет, кнопка «Открыть приложение» не показывается.
MINI_APP_URL = os.getenv("MINI_APP_URL")

HISTORY_LIMIT = 10

COMMANDS = [
    BotCommand(command="start", description="Начать и проверить подключение"),
    BotCommand(command="help", description="Как пользоваться ботом"),
    BotCommand(command="history", description="Последние 10 событий"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="timezone", description="Сменить часовой пояс"),
]

HELP_TEXT = (
    "<b>ДЕЛУ.ВРЕМЯ</b> замечает в ваших личных чатах договорённости о встречах "
    "и предлагает добавить их в Google Календарь.\n\n"
    "<b>Как это работает</b>\n"
    "Вы переписываетесь как обычно (можно голосовыми). Когда в сообщении есть договорённость, "
    "бот пришлёт вам карточку события: нажмите «Добавить в календарь», «Изменить» "
    "или «Не добавлять».\n\n"
    "<b>Примеры фраз, которые бот понимает</b>\n"
    "• «Давай созвонимся завтра в 15:00»\n"
    "• «Встреча в пятницу в 18:30 в кафе на Невском»\n"
    "• «Каждый понедельник в 10:00 планёрка»\n"
    "• «Тренировка через неделю в 19:00 на полтора часа»\n"
    "• «Созвон 25 ноября в 12:00 по екб»\n\n"
    "<b>Команды</b>\n"
    "/history — последние 10 событий\n"
    "/settings — уведомления и доступ к чатам\n"
    "/timezone — сменить часовой пояс\n"
    "/start — проверить подключение"
)

STATUS_LABELS = {
    "draft": ("⏳", "ждёт вашего решения"),
    "queued": ("🕓", "в очереди на отправку в календарь"),
    "confirmed": ("✅", "добавлено в календарь"),
    "failed": ("⚠️", "не удалось добавить в календарь"),
}


# ---------- меню команд и кнопка Mini App ----------

async def setup_bot_menu():
    """Вызывается один раз при старте бота: список команд и кнопка меню.
    Ошибка не должна ронять бота, поэтому только пишем её в лог."""
    try:
        await bot.set_my_commands(COMMANDS)
        if MINI_APP_URL:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="Открыть приложение", web_app=WebAppInfo(url=MINI_APP_URL)))
        else:
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            logger.warning("MINI_APP_URL не задан: кнопка Mini App не установлена")
    except Exception:
        logger.exception("Не удалось настроить меню бота")


# ---------- /help ----------

def help_kb():
    if not MINI_APP_URL:
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Открыть приложение", web_app=WebAppInfo(url=MINI_APP_URL))
    return kb.as_markup()


@cm.message(Command("help"), F.chat.type == "private")
async def command_help(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(HELP_TEXT, reply_markup=help_kb())


# ---------- /history ----------

NO_CONSENT_TEXT = "Согласие на обработку данных не действует, история недоступна. Чтобы продолжить, отправьте /start."


def _short(text: str | None, limit: int = 28) -> str:
    text = (text or "Без названия").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _when(ev) -> str:
    """В БД время хранится как местное (без пояса), поэтому показываем его как есть."""
    return ev.starts_at.strftime("%d.%m %H:%M") if ev.starts_at else "время не указано"


def history_kb(events):
    kb = InlineKeyboardBuilder()
    for ev in events:
        icon = STATUS_LABELS.get(ev.status, ("•", ""))[0]
        kb.button(text=f"{icon} {_when(ev)} · {_short(ev.title)}", callback_data=f"hist:show:{ev.id}")
    kb.adjust(1)
    return kb.as_markup()


async def render_history(user_id: int):
    """Возвращает (текст, клавиатура) для списка последних событий."""
    events = await get_user_events(user_id, limit=HISTORY_LIMIT)
    if not events:
        return "Пока нет ни одного события. Как только бот найдёт договорённость в ваших чатах, она появится здесь.", None
    return f"Последние события (не больше {HISTORY_LIMIT}). Нажмите на событие, чтобы посмотреть детали:", history_kb(events)


def event_details(ev) -> str:
    icon, label = STATUS_LABELS.get(ev.status, ("•", ev.status or "неизвестно"))
    lines = [f"<b>{html.escape(ev.title or 'Без названия')}</b>", f"🕒 {_when(ev)}"]
    if ev.ends_at:
        lines[-1] += f" – {ev.ends_at.strftime('%H:%M')}"
    if ev.location:
        lines.append(f"📍 {html.escape(ev.location)}")
    if ev.rrule:
        lines.append("🔁 Повторяющееся событие")
    lines.append(f"{icon} Статус: {label}")
    return "\n".join(lines)


@cm.message(Command("history"), F.chat.type == "private")
async def command_history(msg: Message, state: FSMContext):
    await state.clear()
    if not await has_active_consent(msg.from_user.id):
        await msg.answer(NO_CONSENT_TEXT)
        return
    text, markup = await render_history(msg.from_user.id)
    await msg.answer(text, reply_markup=markup)


async def _consent_or_deny(call: CallbackQuery) -> bool:
    """Проверка согласия для кнопок истории. Старые кнопки остаются в чате и после отзыва
    согласия, поэтому проверяем при каждом нажатии: без действующего согласия ничего не
    показываем и убираем клавиатуру."""
    if await has_active_consent(call.from_user.id):
        return True
    await call.answer(NO_CONSENT_TEXT, show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass  # сообщение могло быть удалено или устареть: отказ уже показан
    return False


@cm.callback_query(F.data.startswith("hist:show:"))
async def history_show(call: CallbackQuery):
    if not await _consent_or_deny(call):
        return
    ev = await get_event(int(call.data.rsplit(":", 1)[1]))
    # чужие события открывать нельзя: сверяем владельца
    if ev is None or ev.user_id != call.from_user.id:
        await call.answer("Событие не найдено.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ К списку", callback_data="hist:list")
    await call.message.edit_text(event_details(ev), reply_markup=kb.as_markup())
    await call.answer()


@cm.callback_query(F.data == "hist:list")
async def history_list(call: CallbackQuery):
    if not await _consent_or_deny(call):
        return
    text, markup = await render_history(call.from_user.id)
    await call.message.edit_text(text, reply_markup=markup)
    await call.answer()
