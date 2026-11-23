import html
import re
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from datetime import datetime
from config import bot, logger
from database.models import add_user, get_user, edit_user, add_consent, has_active_consent, edit_consent, get_buss_conn, add_buss_conn
import tg_bot.keyboards as keyb
us = Router()


class TimezoneInput(StatesGroup):
    waiting_value = State()


TZ_QUESTION = ("Выберите ваш часовой пояс — по нему бот будет ставить события в календарь.\n"
               "Его всегда можно поменять командой /timezone.")
TZ_OTHER_PROMPT = ("Отправьте смещение от UTC, например <code>+5</code> или <code>UTC-3</code>, "
                   "или название пояса, например <code>Europe/Berlin</code>.")


def goto_settings_button():
    kb = InlineKeyboardBuilder()
    kb.button(text="Кнопка", url="tg://settings/edit/")
    return kb.as_markup()

async def message_buss_conn(user_id: int):
    await bot.send_message(chat_id=user_id, text=("Добро пожаловать в Telegram-ассистент <b>ДЕЛУ.ВРЕМЯ</b>\nДля продолжения подключите ассисента к своему аккаунту.\n\n"
                                 "Нажмите кнопку ниже, перейдите в раздел 'Автоматизация чатов', и добавьте нашего бота: <code>@deluvremyabot</code>"),
                                 reply_markup=goto_settings_button()
                                 )


def parse_timezone(text: str) -> str | None:
    """'+5', 'UTC+5', 'GMT-3', 'Europe/Berlin' -> IANA-название пояса или None."""
    text = text.strip()
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::00)?", text, flags=re.IGNORECASE)
    if m:
        hours = int(m.group(2))
        if hours > 14:
            return None
        if hours == 0:
            return "Etc/UTC"
        # в Etc/GMT знак обратный: UTC+5 — это Etc/GMT-5
        return f"Etc/GMT{'-' if m.group(1) == '+' else '+'}{hours}"
    if text.upper() in ("UTC", "GMT"):
        return "Etc/UTC"
    if "/" in text:
        try:
            ZoneInfo(text)
            return text
        except (ZoneInfoNotFoundError, ValueError):
            return None
    return None


def timezone_title(tz: str) -> str:
    for title, name in keyb.TIMEZONES:
        if name == tz:
            return title
    offset = datetime.now(ZoneInfo(tz)).strftime("%z")  # например "+0400" или "+0530"
    hours, minutes = offset[0] + str(int(offset[1:3])), offset[3:]
    utc = "UTC" if offset in ("+0000", "-0000") else f"UTC{hours}" + (f":{minutes}" if minutes != "00" else "")
    if tz.startswith("Etc/"):  # Etc/GMT-4 — служебное название с обратным знаком, показываем просто UTC+4
        return utc
    return f"{tz} ({utc})"


async def continue_setup(user_id: int):
    '''следующий шаг после согласия и часового пояса: подключение бота и google-календаря'''
    connection = await get_buss_conn(user_id)
    if connection is None or connection.business_token is None:
        await message_buss_conn(user_id)
        return
    try:
        business = await bot.get_business_connection(business_connection_id=connection.business_token)
    except Exception as e:
        if "business connection not found" in str(e):
            await bot.send_message(chat_id=user_id, text="Ваше подключение устарело/не найдено.\n"
                                   "Нажмите кнопку ниже, перейдите в раздел 'Автоматизация чатов', и добавьте нашего бота: <code>@deluvremyabot</code>",
                                   reply_markup=goto_settings_button())
        else:
            logger.warning("Не удалось проверить подключение Telegram Business (%s)", type(e).__name__)
            await bot.send_message(chat_id=user_id, text="Произошла непредвиденная ошибка.")
        return
    if not business.is_enabled or business.user.id != user_id:
        await message_buss_conn(user_id)
        return
    if connection.google_token is None:
        await bot.send_message(chat_id=user_id, text="Пожалуйста, свяжите бота с google-календарём", reply_markup=keyb.google_conn_kb())
    else:
        await bot.send_message(chat_id=user_id, text="Добро пожаловать в Telegram-ассистент <b>ДЕЛУ.ВРЕМЯ</b>\n✅Бот запущен и готов к работе.")
    if connection.status is None or connection.status is False:
        await add_buss_conn(user_id=connection.user_id, business_token=connection.business_token, status=True)


@us.message(CommandStart())
async def command_start(msg: Message, state: FSMContext):
    await state.clear()
    if not await has_active_consent(msg.from_user.id):
        kb = InlineKeyboardBuilder()
        kb.button(text="Разрешаю обработку сообщений", callback_data="user_consent_accept")
        await msg.answer(f"Здравствуйте, {html.escape(msg.from_user.full_name)}!\n\n"
                        "Бот обрабатывает текстовые и голосовые сообщения из выбранных вами чатов "
                        "Telegram Business, чтобы предлагать события. Подтверждённые события передаются "
                        "в подключённый Google Календарь. Доступ к переписке можно выключить в Telegram Business.",
                        reply_markup=kb.as_markup())
        return
    user = await get_user(msg.from_user.id)
    if user is None or not user.timezone:
        # согласие есть, а пояс ещё не выбран (например, пользователь из старой версии бота)
        await add_user(user_id=msg.from_user.id)
        await msg.answer(TZ_QUESTION, reply_markup=keyb.timezone_kb())
        return
    await continue_setup(msg.from_user.id)

@us.callback_query(F.data=="user_consent_accept")
async def user_consent_accept(call: CallbackQuery):
    date = datetime.now(tz=ZoneInfo("Europe/Moscow")).replace(tzinfo=None, microsecond=0)
    result = await edit_consent(user_id=call.from_user.id, policy_version="business-messages-v1",
                                accepted_at=date, revoked_at=None)
    if result is False:
        result = await add_consent(user_id=call.from_user.id, policy_version="business-messages-v1", accepted_at=date)
        if result is False:
            await call.answer("Не удалось записать информацию о вашем согласии. Пожалуйста, обратитесь в поддержку", show_alert=True)
            return False
    await add_user(user_id=call.from_user.id)
    await call.answer("✅ Согласие сохранено")
    user = await get_user(call.from_user.id)
    if user is not None and user.timezone:
        await call.message.delete_reply_markup()
        await continue_setup(call.from_user.id)
        return
    await call.message.edit_text(TZ_QUESTION, reply_markup=keyb.timezone_kb())


@us.message(Command("timezone"))
async def command_timezone(msg: Message, state: FSMContext):
    await state.clear()
    user = await get_user(msg.from_user.id)
    current = f"Сейчас выбран: {timezone_title(user.timezone)}.\n\n" if user and user.timezone else ""
    await msg.answer(current + TZ_QUESTION, reply_markup=keyb.timezone_kb())


async def save_timezone(user_id: int, tz: str) -> bool:
    '''сохраняет пояс; если пользователь выбирал его впервые — продолжает настройку бота'''
    await add_user(user_id=user_id)  # на случай, если записи пользователя ещё нет
    user = await get_user(user_id)
    first_time = user is None or not user.timezone
    if not await edit_user(user_id, timezone=tz):
        await bot.send_message(chat_id=user_id, text="Не удалось сохранить часовой пояс. Попробуйте ещё раз: /timezone")
        return False
    now = datetime.now(ZoneInfo(tz)).strftime("%H:%M")
    await bot.send_message(chat_id=user_id, text=f"✅ Часовой пояс: {timezone_title(tz)}. Сейчас у вас {now}.")
    if first_time and await has_active_consent(user_id):
        await continue_setup(user_id)
    return True


@us.callback_query(F.data.startswith("tz:"))
async def choose_timezone(call: CallbackQuery, state: FSMContext):
    tz = call.data.removeprefix("tz:")
    if tz == "other":
        await state.set_state(TimezoneInput.waiting_value)
        await call.message.edit_text(TZ_OTHER_PROMPT)
        await call.answer()
        return
    if tz not in {name for _, name in keyb.TIMEZONES}:
        await call.answer("Неизвестный часовой пояс.", show_alert=True)
        return
    await call.answer()
    await call.message.delete_reply_markup()
    await save_timezone(call.from_user.id, tz)


@us.message(TimezoneInput.waiting_value, F.text)
async def timezone_input(msg: Message, state: FSMContext):
    tz = parse_timezone(msg.text)
    if tz is None:
        await msg.answer("Не понял часовой пояс. " + TZ_OTHER_PROMPT)
        return
    await state.clear()
    await save_timezone(msg.from_user.id, tz)
