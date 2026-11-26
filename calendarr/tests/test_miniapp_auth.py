import hmac
from urllib.parse import urlencode

import pytest

from calendarr import miniapp_auth

TOKEN = "12345:fixture"
NOW = 1_800_000_000


def authorization(user='{"id":123,"first_name":"Анна"}', when=NOW):
    values = {"auth_date": str(when), "user": user, "query_id": "test", "signature": "fixture-signature"}
    canonical = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    key = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    values["hash"] = hmac.new(key, canonical.encode(), "sha256").hexdigest()
    return "tma " + urlencode(values)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(miniapp_auth.time, "time", lambda: NOW)


def test_valid_telegram_data():
    assert miniapp_auth.verified_user_id(authorization(), TOKEN) == 123


@pytest.mark.parametrize("data", [
    "",
    authorization().replace("123", "456"),
    authorization() + "&auth_date=" + str(NOW),
    authorization(when=NOW - miniapp_auth.SESSION_TTL - 1),
    authorization(when=NOW + 31),
    authorization(user='{"id":true}'),
])
def test_rejects_missing_tampered_stale_and_malformed_telegram_data(data):
    with pytest.raises(ValueError):
        miniapp_auth.verified_user_id(data, TOKEN)
