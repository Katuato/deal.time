import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from calendarr.tests.fake_db import install
from logging_setup import LoggingContextMiddleware, ctx_event, ctx_user, setup_logging


@pytest.fixture
def handlers(monkeypatch):
    install()
    config = ModuleType("config")
    config.bot = SimpleNamespace(
        get_business_connection=AsyncMock(return_value=SimpleNamespace(
            id="business-connection", is_enabled=True, user=SimpleNamespace(id=7),
        )),
        download=AsyncMock(),
    )
    config.logger = logging.getLogger("privacy-test")
    monkeypatch.setitem(sys.modules, "config", config)

    models = ModuleType("database.models")
    for name in ("add_buss_conn", "add_event", "get_buss_conn", "get_event", "get_user", "update_event"):
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
    mod._stt = SimpleNamespace(transcribe=Mock(return_value="тестовая фраза"))
    return mod


@pytest.fixture
def voice_message():
    return SimpleNamespace(
        chat=SimpleNamespace(id=1, type="private"),
        business_connection_id="business-connection",
        voice=SimpleNamespace(file_id="voice-file"),
        text=None,
        date=None,
    )


@pytest.mark.parametrize("enabled,consent", [(True, False), (False, True)])
async def test_voice_is_ignored_without_consent_or_enabled_grant(handlers, voice_message, enabled, consent):
    handlers.bot.get_business_connection.return_value.is_enabled = enabled
    handlers.has_active_consent.return_value = consent

    await handlers.new_message(voice_message)

    handlers.bot.download.assert_not_awaited()
    handlers._stt.transcribe.assert_not_called()
    handlers.process_dialogue_message.assert_not_called()


@pytest.mark.parametrize("failure", ["download", "transcribe"])
async def test_voice_failure_closes_buffer_without_disk_or_log_leak(
    handlers, voice_message, monkeypatch, tmp_path, caplog, failure,
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
        handlers._stt.transcribe.side_effect = RuntimeError(private_text)

    await handlers.new_message(voice_message)

    assert len(buffers) == 1 and buffers[0].closed
    assert list(tmp_path.iterdir()) == []
    assert private_text not in caplog.text
    handlers.process_dialogue_message.assert_not_called()


async def test_voice_passes_bytes_to_transcriber_and_text_to_nlp(handlers, voice_message):
    async def download(voice, destination):
        destination.write(b"test-encoded-audio")

    handlers.bot.download.side_effect = download
    handlers.get_user.return_value = None

    await handlers.new_message(voice_message)

    handlers._stt.transcribe.assert_called_once_with(b"test-encoded-audio")
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
