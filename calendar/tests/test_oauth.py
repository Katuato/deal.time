import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ── Мокаем config ДО импорта oauth ──
_fake_config = SimpleNamespace(
    GOOGLE_CLIENT_ID="test-client-id.apps.googleusercontent.com",
    GOOGLE_CLIENT_SECRET="test-secret",
    GOOGLE_REDIRECT_URI="http://localhost:8080/oauth/google/callback",
    token="test-token",
    bot=None,
)
sys.modules["config"] = _fake_config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from oauth import build_auth_url, exchange_code


def test_build_auth_url_contains_required_params():
    url = build_auth_url(user_id=12345)

    assert "accounts.google.com/o/oauth2/auth" in url
    assert "client_id=test-client-id" in url
    assert "redirect_uri=" in url
    assert "state=12345" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "calendar.events" in url


def test_build_auth_url_without_credentials_raises():
    with patch("oauth.GOOGLE_CLIENT_ID", None):
        with pytest.raises(RuntimeError, match="не сконфигурирован"):
            build_auth_url(user_id=1)


def test_exchange_code_success():
    with patch("oauth.Flow") as mock_flow_class:
        mock_flow = MagicMock()
        mock_creds = MagicMock()
        mock_creds.refresh_token = "refresh-abc"
        mock_creds.to_json.return_value = (
            '{"token": "access-xyz", "refresh_token": "refresh-abc"}'
        )
        mock_flow.credentials = mock_creds
        mock_flow_class.from_client_config.return_value = mock_flow

        user_id, token_data = exchange_code("code123", "12345")

        assert user_id == 12345
        assert token_data == {"token": "access-xyz", "refresh_token": "refresh-abc"}
        mock_flow.fetch_token.assert_called_once_with(code="code123")


def test_exchange_code_invalid_state_raises():
    with patch("oauth.Flow") as mock_flow_class:
        mock_flow = MagicMock()
        mock_creds = MagicMock()
        mock_creds.refresh_token = "refresh"
        mock_creds.to_json.return_value = '{"token": "t"}'
        mock_flow.credentials = mock_creds
        mock_flow_class.from_client_config.return_value = mock_flow

        with pytest.raises(ValueError, match="Некорректный state"):
            exchange_code("code", "not-a-number")


def test_exchange_code_without_refresh_token_raises():
    with patch("oauth.Flow") as mock_flow_class:
        mock_flow = MagicMock()
        mock_creds = MagicMock()
        mock_creds.refresh_token = None
        mock_creds.to_json.return_value = '{"token": "t"}'
        mock_flow.credentials = mock_creds
        mock_flow_class.from_client_config.return_value = mock_flow

        with pytest.raises(RuntimeError, match="refresh_token"):
            exchange_code("code", "12345")