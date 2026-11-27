import re
from hashlib import sha256
from pathlib import Path

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InputRichMessage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from datetime import datetime
from config import bot, logger
from calendarr.calendar_connector import check_connection
from calendarr.storage import DBTokenStorage
from database.models import (
    accept_consent, add_buss_conn, add_user, edit_user, get_buss_conn, get_consent, get_user, has_active_consent,
)
import tg_bot.keyboards as keyb
us = Router()

POLICY_HTML = Path(__file__).with_name("privacy_policy.html").read_text(encoding="utf-8")
POLICY_VERSION = "consent-" + sha256(POLICY_HTML.encode("utf-8")).hexdigest()[:24]
CONSENT_CALLBACK = f"user_consent_accept:{POLICY_VERSION}"


class TimezoneInput(StatesGroup):
    waiting_value = State()


TZ_QUESTION = ("Выберите ваш часовой пояс — по нему бот будет ставить события в календарь.\n"
               "Его всегда можно поменять командой /timezone.")
TZ_OTHER_PROMPT = ("Отправьте смещение от UTC, например <code>+5</code> или <code>UTC-3</code>, "
                   "или название пояса, например <code>Europe/Berlin</code>.")


def goto_settings_button():
    kb = InlineKeyboardBuilder()
    kb.button(text="Подключить", url="tg://settings/edit/")
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


def ready_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🕒 Сменить часовой пояс", callback_data="settings_tz")
    kb.button(text="🔄 Переподключить календарь", callback_data="google_connect")
    kb.adjust(1)
    return kb.as_markup()


async def send_ready(user_id: int):
    '''/start, когда подключены и бизнес-бот, и Google Календарь: проверяем, что токен Google
    живой, и показываем состояние подключения'''
    result = await check_connection(user_id, DBTokenStorage())
    if result.get("error_type") == "auth_error":
        logger.info("/start: токен Google не работает у user_id=%s: %s", user_id, result.get("message"))
        await bot.send_message(chat_id=user_id, text=(
            "💬 Бот подключён к вашему аккаунту, но доступа к Google Календарю нет: "
            "разрешение отозвано или устарело.\n\nПереподключите календарь — без этого события не попадут в него."),
            reply_markup=keyb.reconnect_kb())
        return

    user = await get_user(user_id)
    tz = timezone_title(user.timezone) if user and user.timezone else "не выбран — /timezone"
    if result.get("status") == "success":
        calendar = "📅 Google Календарь: подключён"
    else:  # Google временно недоступен — токен проверить не смогли, но он есть
        logger.warning("/start: не удалось проверить Google Календарь у user_id=%s: %s", user_id, result)
        calendar = "📅 Google Календарь: подключён (сейчас не отвечает — события добавлю, когда он станет доступен)"
    await bot.send_message(chat_id=user_id, reply_markup=ready_kb(), text=(
        "Добро пожаловать в Telegram-ассистент <b>ДЕЛУ.ВРЕМЯ</b>\n✅ Всё подключено, бот работает.\n\n"
        "💬 Бизнес-чаты: подключены\n"
        f"{calendar}\n"
        f"🕒 Часовой пояс: {tz}\n\n"
        "Я читаю ваши переписки, нахожу договорённости о встречах и присылаю их сюда — "
        "одной кнопкой событие попадает в календарь."))


GOOGLE_CONNECTED = "Google Календарь успешно подключён ✅"


async def continue_setup(user_id: int, google_just_connected: bool = False):
    '''следующий шаг после согласия и часового пояса: подключение бота и google-календаря.
    google_just_connected=True — вызов из OAuth-колбэка: если всё подключено, пользователь получает
    то же сообщение, что и на /start; иначе — «Google Календарь подключён» и следующий шаг'''
    async def confirm_google():
        if google_just_connected:
            await bot.send_message(chat_id=user_id, text=GOOGLE_CONNECTED)

    connection = await get_buss_conn(user_id)
    if connection is None or connection.business_token is None:
        await confirm_google()
        await message_buss_conn(user_id)
        return
    try:
        business = await bot.get_business_connection(business_connection_id=connection.business_token)
        lost = not business.is_enabled  # бот убран из «Автоматизации чатов», но подключение ещё числится
    except Exception as e:
        if "business connection not found" not in str(e):
            logger.warning("Не удалось проверить подключение Telegram Business (%s)", type(e).__name__)
            await confirm_google()
            await bot.send_message(chat_id=user_id, text="Произошла непредвиденная ошибка.")
            return
        business, lost = None, True
    if lost:
        await confirm_google()
        await bot.send_message(chat_id=user_id, text="Ваше подключение устарело/не найдено.\n"
                               "Нажмите кнопку ниже, перейдите в раздел 'Автоматизация чатов', и добавьте нашего бота: <code>@deluvremyabot</code>",
                               reply_markup=goto_settings_button())
        return
    if business.user.id != user_id:
        await confirm_google()
        await message_buss_conn(user_id)
        return
    if connection.status is None or connection.status is False:
        await add_buss_conn(user_id=connection.user_id, business_token=connection.business_token, status=True)
    if connection.google_token is None:
        await bot.send_message(chat_id=user_id, text="Пожалуйста, свяжите бота с google-календарём", reply_markup=keyb.google_conn_kb())
    else:
        await send_ready(user_id)


async def send_privacy_policy(user_id: int) -> None:
    consent = await get_consent(user_id)
    accepted = (consent is not None and consent.policy_version == POLICY_VERSION
                and consent.accepted_at is not None and consent.revoked_at is None)
    reply_markup = None
    if not accepted:
        kb = InlineKeyboardBuilder()
        kb.button(text="Даю согласие", callback_data=CONSENT_CALLBACK)
        reply_markup = kb.as_markup()
    await bot.send_rich_message(
        chat_id=user_id,
        rich_message=InputRichMessage(
            html="<h2>Согласие на обработку персональных данных</h2>"
                 f"<details><summary>Читать полный текст согласия</summary>{POLICY_HTML}</details>",
        ),
        reply_markup=reply_markup,
    )


@us.message(CommandStart())
async def command_start(msg: Message, state: FSMContext):
    await state.clear()
    if not await has_active_consent(msg.from_user.id):
        if msg.chat.type != "private":
            await msg.answer("Откройте личный чат с ботом и отправьте /start.")
            return
        await send_privacy_policy(msg.from_user.id)
        return
    user = await get_user(msg.from_user.id)
    if user is None or not user.timezone:
        # согласие есть, а пояс ещё не выбран (например, пользователь из старой версии бота)
        await add_user(user_id=msg.from_user.id)
        await msg.answer(TZ_QUESTION, reply_markup=keyb.timezone_kb())
        return
    await continue_setup(msg.from_user.id)

@us.callback_query(F.data == "user_consent_accept")
@us.callback_query(F.data.startswith("user_consent_accept:"))
async def user_consent_accept(call: CallbackQuery):
    if (not isinstance(call.message, Message) or call.message.chat.type != "private"
            or call.message.chat.id != call.from_user.id):
        await call.answer("Подтвердите согласие в личном чате с ботом.", show_alert=True)
        return
    if call.data != CONSENT_CALLBACK:
        await call.answer("Текст согласия обновился.")
        await send_privacy_policy(call.from_user.id)
        return
    date = datetime.now(tz=ZoneInfo("Europe/Moscow")).replace(tzinfo=None, microsecond=0)
    result = await accept_consent(call.from_user.id, POLICY_VERSION, date)
    if result is None:
        await call.answer("Не удалось сохранить согласие. Попробуйте ещё раз.", show_alert=True)
        return
    if result is False:
        await call.answer("Согласие уже сохранено.")
        return
    await call.answer("✅ Согласие сохранено")
    try:
        await call.message.delete_reply_markup()
    except TelegramBadRequest:
        logger.info("Не удалось убрать кнопку согласия")
    user = await get_user(call.from_user.id)
    if user is not None and user.timezone:
        await continue_setup(call.from_user.id)
        return
    await call.message.answer(TZ_QUESTION, reply_markup=keyb.timezone_kb())


@us.message(Command("timezone"))
async def command_timezone(msg: Message, state: FSMContext):
    await state.clear()
    user = await get_user(msg.from_user.id)
    current = f"Сейчас выбран: {timezone_title(user.timezone)}.\n\n" if user and user.timezone else ""
    await msg.answer(current + TZ_QUESTION, reply_markup=keyb.timezone_kb())


@us.callback_query(F.data == "settings_tz")
async def settings_timezone(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_user(call.from_user.id)
    current = f"Сейчас выбран: {timezone_title(user.timezone)}.\n\n" if user and user.timezone else ""
    await call.answer()
    await call.message.answer(current + TZ_QUESTION, reply_markup=keyb.timezone_kb())


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
