import importlib.util
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User


@pytest.fixture
def settings(monkeypatch):
    bot = AsyncMock()
    models = SimpleNamespace(
        edit_user=AsyncMock(return_value=True),
        get_business_chats=AsyncMock(return_value=[]),
        get_buss_conn=AsyncMock(return_value=SimpleNamespace(business_token="test-connection")),
        has_active_consent=AsyncMock(return_value=True),
        notifications_enabled=AsyncMock(return_value=True),
    )
    bot.get_business_connection.return_value = SimpleNamespace(
        is_enabled=True, user=SimpleNamespace(id=7),
    )
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(bot=bot, logger=logging.getLogger("test-settings")))
    monkeypatch.setitem(sys.modules, "database.models", models)
    path = Path(__file__).resolve().parents[2] / "tg_bot" / "settings.py"
    spec = importlib.util.spec_from_file_location("_test_bot_settings", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, models, bot


def message(bot):
    return Message(
        message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=7, type="private"),
        from_user=User(id=7, is_bot=False, first_name="Пользователь"),
    ).as_(bot)


def click(bot, data, *, user_id=7):
    return CallbackQuery(
        id="callback", from_user=User(id=user_id, is_bot=False, first_name="Пользователь"),
        chat_instance="chat", message=message(bot), data=data,
    ).as_(bot)


async def test_settings_displays_both_sections(settings):
    module, _, bot = settings
    await module.command_settings(message(bot))
    sent = bot.await_args.args[0]
    assert isinstance(sent, SendMessage)
    assert [row[0].text for row in sent.reply_markup.inline_keyboard] == ["Уведомления", "Доступ к чатам"]


@pytest.mark.parametrize("enabled", [False, True])
async def test_notification_choice_is_saved_for_the_owner(settings, enabled):
    module, models, bot = settings
    models.notifications_enabled.return_value = enabled
    button = "on" if enabled else "off"
    await module.settings_action(click(bot, f"settings:notifications:{button}"))

    models.edit_user.assert_awaited_once_with(7, notification=enabled)
    edited = bot.await_args.args[0]
    assert isinstance(edited, EditMessageText)
    assert ("включены" if enabled else "выключены") in edited.text


async def test_chat_page_shows_observed_names_and_telegram_permissions_link(settings):
    module, models, bot = settings
    models.get_business_chats.return_value = [
        SimpleNamespace(chat_id=n, chat_title=f"<Контакт {n}>") for n in range(module.PAGE_SIZE + 1)
    ]
    await module.settings_action(click(bot, "settings:chats:0"))

    models.get_business_chats.assert_awaited_once_with(7, offset=0, limit=module.PAGE_SIZE + 1)
    edited = bot.await_args.args[0]
    assert "получал сообщения" in edited.text
    assert "&lt;Контакт 0&gt;" in edited.text
    assert f"Контакт {module.PAGE_SIZE}&gt;" not in edited.text
    buttons = [row[0] for row in edited.reply_markup.inline_keyboard]
    assert any(button.url == "tg://settings/business" for button in buttons)
    assert any(button.callback_data == "settings:chats:1" for button in buttons)


async def test_disconnected_business_connection_hides_observed_chats(settings):
    module, models, bot = settings
    bot.get_business_connection.return_value.is_enabled = False
    await module.settings_action(click(bot, "settings:chats:0"))

    models.get_business_chats.assert_not_awaited()
    assert "отключено" in bot.await_args.args[0].text


async def test_connected_account_can_have_no_observed_chats(settings):
    module, _, bot = settings
    await module.settings_action(click(bot, "settings:chats:0"))
    assert "пока нет известных чатов" in bot.await_args.args[0].text


async def test_another_user_cannot_change_settings_through_the_button(settings):
    module, models, bot = settings
    await module.settings_action(click(bot, "settings:notifications:off", user_id=123))
    models.edit_user.assert_not_awaited()
