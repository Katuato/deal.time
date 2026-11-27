import importlib.util
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User


@pytest.fixture
def onboarding(monkeypatch):
    bot = AsyncMock()
    models = SimpleNamespace(
        accept_consent=AsyncMock(return_value=True),
        add_buss_conn=AsyncMock(),
        add_user=AsyncMock(),
        edit_user=AsyncMock(),
        get_buss_conn=AsyncMock(),
        get_consent=AsyncMock(return_value=None),
        get_user=AsyncMock(return_value=None),
        has_active_consent=AsyncMock(return_value=False),
    )
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(bot=bot, logger=logging.getLogger(__name__)))
    monkeypatch.setitem(sys.modules, "database.models", models)
    monkeypatch.setitem(sys.modules, "calendarr.storage", SimpleNamespace(DBTokenStorage=lambda: object()))
    path = Path(__file__).resolve().parents[2] / "tg_bot" / "user_end.py"
    spec = importlib.util.spec_from_file_location("_test_consent_onboarding", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, models, bot


def private_message(bot, *, from_user_id=7):
    return Message(
        message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=7, type="private"),
        from_user=User(id=from_user_id, is_bot=from_user_id != 7, first_name="Пользователь"),
    ).as_(bot)


def consent_click(bot, data, *, user_id=7):
    return CallbackQuery(
        id="callback", from_user=User(id=user_id, is_bot=False, first_name="Пользователь"),
        chat_instance="chat", message=private_message(bot, from_user_id=999), data=data,
    ).as_(bot)


async def test_start_shows_the_full_policy_in_a_closed_block(onboarding):
    module, models, bot = onboarding
    await module.command_start(private_message(bot), SimpleNamespace(clear=AsyncMock()))

    sent = bot.send_rich_message.await_args.kwargs
    assert sent["chat_id"] == 7
    assert "<details><summary>Читать полный текст согласия</summary>" in sent["rich_message"].html
    assert module.POLICY_HTML in sent["rich_message"].html
    assert sent["reply_markup"].inline_keyboard[0][0].callback_data == module.CONSENT_CALLBACK
    models.accept_consent.assert_not_awaited()


@pytest.mark.parametrize("old_callback", ["user_consent_accept", "user_consent_accept:consent-old"])
async def test_old_button_shows_current_policy_without_approving_it(onboarding, old_callback):
    module, models, bot = onboarding
    await module.user_consent_accept(consent_click(bot, old_callback))

    models.accept_consent.assert_not_awaited()
    models.get_consent.assert_awaited_once_with(7)
    assert bot.send_rich_message.await_args.kwargs["chat_id"] == 7


async def test_acceptance_saves_version_and_continues_onboarding(onboarding):
    module, models, bot = onboarding
    await module.user_consent_accept(consent_click(bot, module.CONSENT_CALLBACK))

    user_id, version, accepted_at = models.accept_consent.await_args.args
    assert (user_id, version) == (7, module.POLICY_VERSION)
    assert isinstance(accepted_at, datetime)
    actions = [call.args[0] for call in bot.await_args_list]
    assert [type(action) for action in actions] == [AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage]
    assert actions[-1].text == module.TZ_QUESTION


async def test_consent_button_cannot_be_used_by_another_user(onboarding):
    module, models, bot = onboarding
    await module.user_consent_accept(consent_click(bot, module.CONSENT_CALLBACK, user_id=123))
    models.accept_consent.assert_not_awaited()


async def test_start_with_consent_displays_calendar_and_business_status(onboarding, monkeypatch):
    module, models, bot = onboarding
    models.has_active_consent.return_value = True
    models.get_user.return_value = SimpleNamespace(timezone="Europe/Moscow")
    models.get_buss_conn.return_value = SimpleNamespace(business_token="business", google_token="token", status=True)
    bot.get_business_connection.return_value = SimpleNamespace(is_enabled=True, user=SimpleNamespace(id=7))
    monkeypatch.setattr(module, "check_connection", AsyncMock(return_value={"status": "success"}))

    await module.command_start(private_message(bot), SimpleNamespace(clear=AsyncMock()))

    assert "Google Календарь: подключён" in bot.send_message.await_args.kwargs["text"]
    assert "Бизнес-чаты: подключены" in bot.send_message.await_args.kwargs["text"]
    bot.send_rich_message.assert_not_awaited()


def _all_connected(models, bot, *, google_token="token"):
    models.has_active_consent.return_value = True
    models.get_user.return_value = SimpleNamespace(timezone="Europe/Moscow")
    models.get_buss_conn.return_value = SimpleNamespace(
        user_id=7, business_token="business", google_token=google_token, status=True)
    bot.get_business_connection.return_value = SimpleNamespace(is_enabled=True, user=SimpleNamespace(id=7))


async def test_business_connect_with_google_shows_start_message(onboarding, monkeypatch):
    module, models, bot = onboarding
    _all_connected(models, bot)
    monkeypatch.setattr(module, "check_connection", AsyncMock(return_value={"status": "success"}))

    await module.continue_setup(7, business_just_connected=True)

    texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert len(texts) == 1 and "✅ Всё подключено" in texts[0]


async def test_business_connect_without_google_confirms_and_asks_for_calendar(onboarding):
    module, models, bot = onboarding
    _all_connected(models, bot, google_token=None)

    await module.continue_setup(7, business_just_connected=True)

    calls = bot.send_message.await_args_list
    assert [call.kwargs["text"] for call in calls][0] == module.BUSINESS_CONNECTED
    assert "google-календарём" in calls[1].kwargs["text"]
    assert calls[1].kwargs["reply_markup"] is not None


async def test_business_connect_before_timezone_asks_for_timezone(onboarding):
    module, models, bot = onboarding
    _all_connected(models, bot)
    models.get_user.return_value = SimpleNamespace(timezone=None)

    await module.continue_setup(7, business_just_connected=True)

    calls = bot.send_message.await_args_list
    assert [call.kwargs["text"] for call in calls] == [module.BUSINESS_CONNECTED, module.TZ_QUESTION]
    models.get_buss_conn.assert_not_awaited()
