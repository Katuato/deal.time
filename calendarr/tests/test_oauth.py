"""Тесты oauth: state/PKCE. config подменяется заглушкой (без создания aiogram Bot)."""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def oauth(monkeypatch):
    stub = types.ModuleType("config")
    stub.GOOGLE_CLIENT_ID = "cid"
    stub.GOOGLE_CLIENT_SECRET = "sec"
    stub.GOOGLE_REDIRECT_URI = "http://localhost:8080/oauth2callback"
    monkeypatch.setitem(sys.modules, "config", stub)
    monkeypatch.delitem(sys.modules, "calendarr.oauth", raising=False)
    from calendarr import oauth as mod
    mod._pending_states.clear()
    yield mod
    mod._pending_states.clear()


def _state_from(url: str) -> str:
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(url).query)["state"][0]


def test_state_random_and_stored(oauth):
    s1 = _state_from(oauth.build_auth_url(42))
    s2 = _state_from(oauth.build_auth_url(42))
    assert s1 != s2 and s1 != "42"
    assert oauth._pending_states[s1][0] == 42


async def test_unknown_state_raises(oauth):
    with pytest.raises(ValueError):
        await oauth.exchange_code("code", "nope")


async def test_expired_state_raises(oauth):
    state = _state_from(oauth.build_auth_url(1))
    uid, ver, ts = oauth._pending_states[state]
    oauth._pending_states[state] = (uid, ver, ts - oauth.STATE_TTL_SECONDS - 1)
    with pytest.raises(ValueError):
        await oauth.exchange_code("code", state)


async def test_one_time_and_code_verifier_passed(oauth):
    state = _state_from(oauth.build_auth_url(7))
    verifier = oauth._pending_states[state][1]
    assert verifier

    fake_flow = MagicMock()
    fake_flow.credentials.refresh_token = "rt"
    fake_flow.credentials.to_json.return_value = '{"refresh_token": "rt"}'
    with patch.object(oauth.Flow, "from_client_config", return_value=fake_flow) as fcc:
        uid, token = await oauth.exchange_code("the-code", state)
    assert uid == 7 and token == {"refresh_token": "rt"}
    assert fcc.call_args.kwargs["code_verifier"] == verifier
    fake_flow.fetch_token.assert_called_once_with(code="the-code")

    with pytest.raises(ValueError):  # повторное использование
        await oauth.exchange_code("the-code", state)
