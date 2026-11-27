import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from calendarr.tests.fake_db import install
from modules import speech_runtime
from logging_setup import LoggingContextMiddleware, ctx_event, ctx_user, setup_logging


@pytest.fixture
def speech_processor(monkeypatch):
    processor = SimpleNamespace(transcribe=Mock(return_value="тестовая фраза"))
    monkeypatch.setattr(speech_runtime, "initialize_stt", AsyncMock(return_value=processor))
    return processor


@pytest.fixture
def handlers(monkeypatch, speech_processor):
    install()
    config = ModuleType("config")
    config.bot = SimpleNamespace(
        get_business_connection=AsyncMock(return_value=SimpleNamespace(
            id="business-connection", is_enabled=True, user=SimpleNamespace(id=7),
        )),
        download=AsyncMock(),
        send_message=AsyncMock(),
    )
    config.logger = logging.getLogger("privacy-test")
    monkeypatch.setitem(sys.modules, "config", config)

    models = ModuleType("database.models")
    for name in ("add_buss_conn", "add_event", "get_buss_conn", "get_event", "get_user",
                 "notifications_enabled", "record_business_chat", "update_event"):
        setattr(models, name, AsyncMock())
    models.has_active_consent = AsyncMock(return_value=True)
    monkeypatch.setitem(sys.modules, "database.models", models)

    oauth = ModuleType("calendarr.oauth")
    oauth.build_auth_url = Mock()
    monkeypatch.setitem(sys.modules, "calendarr.oauth", oauth)

    path = Path(__file__).resolve().parents[2] / "tg_bot" / "handlers.py"
    spec = importlib.util.spec_from_file_location("_privacy_handlers", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.process_dialogue_message = Mock(return_value={"status": "no_event"})
    return mod


@pytest.fixture
def voice_message():
    return SimpleNamespace(
        chat=SimpleNamespace(id=1, type="private", full_name="Имя контакта", username=None),
        business_connection_id="business-connection",
        voice=SimpleNamespace(file_id="voice-file"),
        text=None,
        date=None,
    )


@pytest.mark.parametrize("enabled,consent", [(True, False), (False, True)])
async def test_voice_is_ignored_without_consent_or_enabled_grant(handlers, voice_message, speech_processor, enabled, consent):
    handlers.bot.get_business_connection.return_value.is_enabled = enabled
    handlers.has_active_consent.return_value = consent

    await handlers.new_message(voice_message)

    handlers.bot.download.assert_not_awaited()
    speech_processor.transcribe.assert_not_called()
    handlers.process_dialogue_message.assert_not_called()
    handlers.record_business_chat.assert_not_awaited()


async def test_business_update_records_chat_under_the_connection_owner(handlers, voice_message):
    handlers.get_user.return_value = SimpleNamespace(timezone=None)
    voice_message.voice = None

    await handlers.new_message(voice_message)

    handlers.record_business_chat.assert_awaited_once_with(7, 1, "Имя контакта")
    handlers.process_dialogue_message.assert_not_called()


@pytest.mark.parametrize("enabled", [True, False])
async def test_event_card_respects_notification_sound_preference(handlers, enabled):
    handlers.notifications_enabled.return_value = enabled
    handlers.add_event.return_value = 12
    handlers.get_event.return_value = SimpleNamespace(
        title="Тестовая встреча", starts_at=None, location=None, rrule=None,
    )
    event = handlers.NlpEvent(
        intent="create", summary="Тестовая встреча",
        starts_at="2026-11-27T15:00:00+03:00", ends_at="2026-11-27T15:30:00+03:00",
    ).model_dump()

    await handlers.new_event({"status": "ok", "event": event}, user_id=7, chat_id=1, user_tz="Europe/Moscow")

    handlers.notifications_enabled.assert_awaited_once_with(7)
    sent = handlers.bot.send_message.await_args.kwargs
    assert sent["chat_id"] == 7
    assert sent["disable_notification"] is not enabled
    assert sent["reply_markup"].inline_keyboard[0][0].callback_data == "ev:add:12"


@pytest.mark.parametrize("failure", ["download", "transcribe"])
async def test_voice_failure_closes_buffer_without_disk_or_log_leak(
    handlers, voice_message, speech_processor, monkeypatch, tmp_path, caplog, failure,
):
    monkeypatch.chdir(tmp_path)
    buffers = []
    private_text = "private-audio-sentinel"

    async def download(voice, destination):
        buffers.append(destination)
        destination.write(b"test-encoded-audio")
        if failure == "download":
            raise RuntimeError(private_text)

    handlers.bot.download.side_effect = download
    if failure == "transcribe":
        speech_processor.transcribe.side_effect = RuntimeError(private_text)

    await handlers.new_message(voice_message)

    assert len(buffers) == 1 and buffers[0].closed
    assert list(tmp_path.iterdir()) == []
    assert private_text not in caplog.text
    handlers.process_dialogue_message.assert_not_called()


async def test_voice_passes_bytes_to_transcriber_and_text_to_nlp(handlers, voice_message, speech_processor):
    async def download(voice, destination):
        destination.write(b"test-encoded-audio")

    handlers.bot.download.side_effect = download
    handlers.get_user.return_value = None

    await handlers.new_message(voice_message)

    speech_processor.transcribe.assert_called_once_with(b"test-encoded-audio")
    assert handlers.process_dialogue_message.call_args.kwargs["text"] == "тестовая фраза"


async def test_update_logging_uses_event_type_and_resets_context():
    event = SimpleNamespace(
        event_type="business_message",
        message=SimpleNamespace(text="private-message", caption="private-caption"),
    )
    data = {"event_from_user": SimpleNamespace(id=7, username="private-username")}

    async def handler(update, payload):
        assert ctx_event.get() == "business_message"
        assert ctx_user.get() == "7"
        raise RuntimeError

    with pytest.raises(RuntimeError):
        await LoggingContextMiddleware()(handler, event, data)
    assert ctx_event.get() == ctx_user.get() == "-"


def test_console_logging_hides_exception_details_and_disables_file_logging(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(logging, "setLogRecordFactory", Mock())
    basic_config = Mock()
    monkeypatch.setattr(logging, "basicConfig", basic_config)
    setup_logging()
    [console] = basic_config.call_args.kwargs["handlers"]

    private_text = "private-exception-detail"
    try:
        raise RuntimeError(private_text)
    except RuntimeError:
        record = logging.LogRecord(
            "aiogram.event", logging.ERROR, "", 0, "SDK error %s",
            (sys.exc_info()[1],), sys.exc_info(),
        )
    record.user = record.event = "-"
    console.handle(record)

    output = capsys.readouterr().err
    assert "RuntimeError" in output
    assert private_text not in output
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def business_update():
    return SimpleNamespace(id="business-connection", is_enabled=True, user=SimpleNamespace(id=7))


async def test_business_connect_continues_setup_like_google(handlers, business_update, monkeypatch):
    setup = SimpleNamespace(continue_setup=AsyncMock())
    monkeypatch.setitem(sys.modules, "tg_bot.user_end", setup)
    handlers.get_buss_conn.return_value = None
    handlers.add_buss_conn.return_value = True

    await handlers.check_business_conn(business_update)

    handlers.add_buss_conn.assert_awaited_once_with(user_id=7, business_token="business-connection", status=True)
    setup.continue_setup.assert_awaited_once_with(7, business_just_connected=True)
    handlers.bot.send_message.assert_not_awaited()


async def test_repeated_business_update_does_not_repeat_setup(handlers, business_update, monkeypatch):
    setup = SimpleNamespace(continue_setup=AsyncMock())
    monkeypatch.setitem(sys.modules, "tg_bot.user_end", setup)
    handlers.get_buss_conn.return_value = SimpleNamespace(business_token="business-connection", status=True)

    await handlers.check_business_conn(business_update)

    handlers.add_buss_conn.assert_not_awaited()
    setup.continue_setup.assert_not_awaited()


async def test_chat_correction_updates_the_shown_card(handlers):
    """Переписка из чата: «Пойдем в бильярд?» / «В 18» / «Завтра в бильярд в 18» /
    «давай лучше в 20» / «Хорошо!». Модель подменена, правила и диалог — настоящие."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from modules import nlp_module as nlp

    answers = {
        "Пойдем в бильярд?": {"intent": "create", "summary": "Бильярд"},
        "В 18": {"intent": "create", "summary": "Встреча"},
        "Завтра в бильярд в 18": {"intent": "create", "summary": "Бильярд"},
        "давай лучше в 20": {"intent": "create", "summary": "Встреча"},
        "Хорошо!": {"intent": None},
    }

    def extractor(text, context=None, message_time=None, tz=nlp.DEFAULT_TZ):
        ev = nlp.postprocess(text, answers[text], context, message_time, tz)
        return {"status": "ok", "event": ev.model_dump() if ev.intent else None}

    handlers.process_dialogue_message = (
        lambda **kw: nlp.process_dialogue_message(extractor=extractor, **kw))
    handlers.get_user.return_value = SimpleNamespace(timezone="Europe/Moscow")
    handlers.add_event.return_value = 42
    handlers.update_event.return_value = True
    stored = {}

    async def add_event(**fields):
        stored.update(fields, status="draft")
        return 42

    async def update_event(event_id, draft_user_id=None, **fields):
        stored.update(fields)
        return True

    handlers.add_event.side_effect = add_event
    handlers.update_event.side_effect = update_event
    handlers.get_event.side_effect = lambda event_id: SimpleNamespace(rrule=None, **{
        "title": stored["title"], "starts_at": stored["starts_at"], "location": stored.get("location"),
        "status": stored["status"]})
    handlers.bot.send_message.return_value = SimpleNamespace(message_id=100)
    handlers.bot.edit_message_text = AsyncMock()

    when = datetime(2026, 11, 27, 9, 59, tzinfo=ZoneInfo("UTC"))   # 12:59 по Москве
    for text in answers:
        await handlers.new_message(SimpleNamespace(
            chat=SimpleNamespace(id=1, type="private", full_name="Контакт", username=None),
            business_connection_id="business-connection", voice=None, text=text, date=when))

    # «В 18» без дня — сегодня в 18:00; затем карточка уточняется, а не создаётся заново
    assert handlers.add_event.await_count == 1
    assert handlers.add_event.await_args.kwargs["starts_at"] == datetime(2026, 11, 27, 18, 0)
    assert stored["starts_at"] == datetime(2026, 11, 28, 20, 0)
    assert stored["title"] == "Бильярд"
    edits = handlers.bot.edit_message_text.await_args_list
    assert [e.kwargs["message_id"] for e in edits] == [100, 100]   # завтра в 18, затем в 20; «Хорошо!» не трогает
    assert "28.11.2026 в 20:00" in edits[-1].kwargs["text"]
    assert handlers.bot.send_message.await_count == 1


async def test_processed_card_is_forgotten_and_reply_updates_new_card(handlers):
    """Старая карточка уже отклонена. «Привет! Пойдешь на пары к 12?» — новая карточка
    без даты старой, без вопроса «00:00 или 12:00?»; «Не, я пойду к 18» — правит её же,
    хотя модель назвала это «Перенос на 18»."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from modules import nlp_module as nlp

    answers = {
        "Давай встретимся 12.12 в 15:00": {"intent": "create", "summary": "Встреча"},
        "Привет! Пойдешь на пары к 12?": {"intent": "create", "summary": "Пары"},
        "Не, я пойду к 18": {"intent": "move", "summary": "Перенос на 18"},
    }

    def extractor(text, context=None, message_time=None, tz=nlp.DEFAULT_TZ):
        ev = nlp.postprocess(text, answers[text], context, message_time, tz)
        return {"status": "ok", "event": ev.model_dump() if ev.intent else None,
                "clarify": nlp.clarify_question(ev)}

    handlers.process_dialogue_message = (
        lambda **kw: nlp.process_dialogue_message(extractor=extractor, **kw))
    handlers.get_user.return_value = SimpleNamespace(timezone="Europe/Moscow")
    events = {}

    async def add_event(**fields):
        event_id = len(events) + 1
        events[event_id] = dict(fields, status="draft")
        return event_id

    async def update_event(event_id, draft_user_id=None, **fields):
        if events[event_id]["status"] != "draft":
            return False
        events[event_id].update(fields)
        return True

    handlers.add_event.side_effect = add_event
    handlers.update_event.side_effect = update_event
    handlers.get_event.side_effect = lambda event_id: SimpleNamespace(
        rrule=None, location=None, **{k: events[event_id][k] for k in ("title", "starts_at", "status")})
    handlers.bot.send_message.return_value = SimpleNamespace(message_id=100)
    handlers.bot.edit_message_text = AsyncMock()

    def message(text):
        return SimpleNamespace(
            chat=SimpleNamespace(id=1, type="private", full_name="Контакт", username=None),
            business_connection_id="business-connection", voice=None, text=text,
            date=datetime(2026, 11, 27, 10, 48, tzinfo=ZoneInfo("UTC")))   # 13:48 по Москве

    await handlers.new_message(message("Давай встретимся 12.12 в 15:00"))
    events[1]["status"] = "declined"   # пользователь нажал «Не добавлять»
    await handlers.new_message(message("Привет! Пойдешь на пары к 12?"))
    await handlers.new_message(message("Не, я пойду к 18"))

    assert len(events) == 2
    assert events[2]["title"] == "Пары"
    assert events[2]["starts_at"] == datetime(2026, 11, 28, 18, 0)   # не 12.12
    card = handlers.bot.send_message.await_args_list[-1].kwargs["text"]
    assert "Уточните время" not in card
    assert "28.11.2026 в 18:00" in handlers.bot.edit_message_text.await_args.kwargs["text"]


async def test_added_card_is_not_duplicated_by_agreement_reply(handlers):
    """«Пойдешь с нами в бильярд в 18?» / «давай лучше в 20!!» / «Хорошо, договорились»;
    «Добавить» нажали, пока разбиралось последнее сообщение, а модель назвала его «Дорогие»."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from modules import nlp_module as nlp

    events = {}
    answers = {
        "Привет! Пойдешь в с нами в бильярд в 18?": {"intent": "create", "summary": "Встреча", "location": "бильярд"},
        "давай лучше в 20!!": {"intent": "create", "summary": "Встреча"},
        "Хорошо, договорились": {"intent": "create", "summary": "Дорогие"},
    }

    def extractor(text, context=None, message_time=None, tz=nlp.DEFAULT_TZ):
        if text == "Хорошо, договорились":
            events[1]["status"] = "queued"   # пользователь нажал «Добавить» во время разбора
        ev = nlp.postprocess(text, answers[text], context, message_time, tz)
        return {"status": "ok", "event": ev.model_dump() if ev.intent else None}

    async def add_event(**fields):
        events[len(events) + 1] = dict(fields, status="draft")
        return len(events)

    async def update_event(event_id, draft_user_id=None, **fields):
        if events[event_id]["status"] != "draft":
            return False
        events[event_id].update(fields)
        return True

    handlers.process_dialogue_message = (
        lambda **kw: nlp.process_dialogue_message(extractor=extractor, **kw))
    handlers.get_user.return_value = SimpleNamespace(timezone="Europe/Moscow")
    handlers.add_event.side_effect = add_event
    handlers.update_event.side_effect = update_event
    handlers.get_event.side_effect = lambda event_id: SimpleNamespace(
        rrule=None, **{k: events[event_id].get(k) for k in ("title", "starts_at", "status", "location")})
    handlers.bot.send_message.return_value = SimpleNamespace(message_id=100)
    handlers.bot.edit_message_text = AsyncMock()

    for text in answers:
        await handlers.new_message(SimpleNamespace(
            chat=SimpleNamespace(id=1, type="private", full_name="Аня", username="anya"),
            business_connection_id="business-connection", voice=None, text=text,
            date=datetime(2026, 11, 27, 11, 13, tzinfo=ZoneInfo("UTC"))))   # 14:13 по Москве

    assert len(events) == 1                                   # без второй карточки «Дорогие»
    assert events[1]["title"] == "Встреча"
    assert events[1]["starts_at"] == datetime(2026, 11, 27, 20, 0)
    assert handlers.bot.send_message.await_count == 1
    first_card = handlers.bot.send_message.await_args.kwargs["text"]
    assert "💬 Чат: @anya" in first_card
    assert "💬 Чат: @anya" in handlers.bot.edit_message_text.await_args.kwargs["text"]
