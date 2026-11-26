import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import calendarr


@pytest.fixture
def server(monkeypatch):
    config = SimpleNamespace(GOOGLE_REDIRECT_URI=None, bot=SimpleNamespace(send_message=AsyncMock()))
    oauth = SimpleNamespace(exchange_code=AsyncMock(return_value=(7, {"access_token": "test-token"})))
    storage = SimpleNamespace(save_user_token=AsyncMock())
    monkeypatch.setitem(sys.modules, "config", config)
    monkeypatch.setattr(calendarr, "oauth", oauth, raising=False)
    monkeypatch.setitem(sys.modules, "calendarr.storage", SimpleNamespace(DBTokenStorage=lambda: storage))
    monkeypatch.setitem(sys.modules, "database.db", SimpleNamespace(check_database=AsyncMock()))
    monkeypatch.setitem(sys.modules, "database.models", SimpleNamespace(
        has_active_consent=AsyncMock(return_value=True),
        notifications_enabled=AsyncMock(return_value=True),
    ))

    path = Path(__file__).resolve().parents[1] / "oauth_server.py"
    spec = importlib.util.spec_from_file_location("_test_oauth_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, storage


def test_health_checks_database_and_preserves_plain_text(server):
    module, _ = server
    with TestClient(module.create_app()) as client:
        response = client.get("/healthz")
        assert (response.status_code, response.text) == (200, "ok")
        assert response.headers["content-type"].startswith("text/plain")
        assert client.head("/healthz").status_code == 200

        module.check_database.side_effect = RuntimeError("private detail")
        response = client.get("/healthz")
        assert (response.status_code, response.text) == (503, "unavailable")


def test_oauth_uses_configured_path_and_keeps_privacy_headers(server):
    module, storage = server
    module.config.GOOGLE_REDIRECT_URI = "https://example.org/oauth/google"
    with TestClient(module.create_app()) as client:
        assert client.get("/oauth2callback").status_code == 404
        incomplete = client.get("/oauth/google", params={"code": "test-code"})
        assert incomplete.status_code == 400
        module.oauth.exchange_code.assert_not_awaited()

        response = client.get("/oauth/google", params={"code": "test-code", "state": "test-state"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "test-token" not in response.text
        module.oauth.exchange_code.assert_awaited_once_with("test-code", "test-state")
        storage.save_user_token.assert_awaited_once_with(7, {"access_token": "test-token"})


def test_oauth_callback_requires_consent_before_saving_token(server):
    module, storage = server
    module.has_active_consent.return_value = False
    with TestClient(module.create_app()) as client:
        response = client.get("/oauthcallback", params={"code": "test-code", "state": "test-state"})
        assert response.status_code == 403
        storage.save_user_token.assert_not_awaited()
        module.config.bot.send_message.assert_not_awaited()


def test_oauth_respects_disabled_notifications(server):
    module, storage = server
    module.notifications_enabled.return_value = False
    with TestClient(module.create_app()) as client:
        response = client.get("/oauthcallback", params={"code": "test-code", "state": "test-state"})
        assert response.status_code == 200
    storage.save_user_token.assert_awaited_once()
    module.config.bot.send_message.assert_not_awaited()


def test_oauth_redirect_without_path_uses_root_route(server):
    module, _ = server
    module.config.GOOGLE_REDIRECT_URI = "https://example.org"
    with TestClient(module.create_app()) as client:
        assert client.get("/").status_code == 400
        assert client.get("/oauth2callback").status_code == 404


def test_frontend_root_coexists_with_legacy_and_dedicated_oauth_callback(server, tmp_path):
    module, _ = server
    (tmp_path / "index.html").write_text("Mini App", encoding="utf-8")
    module.config.GOOGLE_REDIRECT_URI = "https://example.org/"
    with TestClient(module.create_app(frontend=tmp_path)) as client:
        assert client.get("/").text == "Mini App"
        assert client.get("/?error=access_denied").status_code == 400

    module.config.GOOGLE_REDIRECT_URI = "https://example.org/oauthcallback"
    with TestClient(module.create_app(frontend=tmp_path)) as client:
        assert client.get("/").text == "Mini App"
        assert client.get("/oauthcallback?error=access_denied").status_code == 400
        assert client.get("/oauth2callback").status_code == 404
