from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from zoneinfo import ZoneInfo

from datetime import datetime
from config import bot
from database.models import add_user, add_consent, get_consent, get_buss_conn, add_buss_conn
import keyboards as keyb
us = Router()
def goto_settings_button():
    kb = InlineKeyboardBuilder()
    kb.button(text="Кнопка", url="tg://settings/edit/")
    return kb.as_markup()

async def message_buss_conn(user_id: int):
    await bot.send_message(chat_id=user_id, text=("Добро пожаловать в Telegram-ассистент <b>ДЕЛУ.ВРЕМЯ</b>\nДля продолжения подключите ассисента к своему аккаунту.\n\n"
                                 "Нажмите кнопку ниже, перейдите в раздел 'Автоматизация чатов', и добавьте нашего бота: <code>@deluvremyabot</code>"),
                                 reply_markup=goto_settings_button()
                                 )

@us.message(CommandStart())
async def command_start(msg: Message):
    consent = await get_consent(msg.from_user.id)
    if consent is None or consent.revoked_at is not None:
        kb = InlineKeyboardBuilder()
        kb.button(text="Я ознакомлен(-а) с пользовательским соглашением", callback_data="user_consent_accept")
        await msg.answer(f"Здравствуйте, {msg.from_user.full_name}!\n\n"
                        f"Перед началом использования прочтите <a href='https://google.com/'>Пользовательское соглашение</a>.",
                        reply_markup=kb.as_markup())
    else:
        connection = await get_buss_conn(msg.from_user.id)
        match connection:
            case None:
                await message_buss_conn(msg.from_user.id)
            
            case _:
                try: 
                    await bot.get_business_connection(business_connection_id=connection.business_token)
                except Exception as e:
                    if "business connection not found" in str(e):
                        await msg.answer("Ваше подключение устарело/не найдено.\n"
                                        "Нажмите кнопку ниже, перейдите в раздел 'Автоматизация чатов', и добавьте нашего бота: <code>@deluvremyabot</code>",
                                        reply_markup=goto_settings_button())
                    else:
                        await msg.answer(f"Произошла непредвиденная ошибка.\n", e)
                else:
                    if connection.google_token is None:
                        await msg.answer("Пожалуйста, свяжите бота с google-календарём", reply_markup=keyb.google_conn_kb())
                    if connection.status is None or connection.status is False:
                        await add_buss_conn(user_id=connection.user_id, business_token=connection.business_token, status=True)

@us.callback_query(F.data=="user_consent_accept")
async def user_consent_accept(call: CallbackQuery):
    date = datetime.now(tz=ZoneInfo("Europe/Moscow")).replace(tzinfo=None, microsecond=0)
    await add_consent(user_id=call.from_user.id, policy_version='123', accepted_at=date)
    await call.answer("✅ Пользовательское соглашение принято")
    user = await add_user(user_id=call.from_user.id)
    print(user)
    await call.message.edit_text("Добро пожаловать в Telegram-ассистент <b>ДЕЛУ.ВРЕМЯ</b>\nДля продолжения подключите ассисента к своему аккаунту.\n\n"
                                 "Нажмите кнопку ниже, перейдите в раздел 'Автоматизация чатов', и добавьте нашего бота: <code>@deluvremyabot</code>",
                                 reply_markup=goto_settings_button())