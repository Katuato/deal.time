"""Validate Telegram Mini App launch data using the bot's token."""

import hmac
import json
import time
from urllib.parse import parse_qsl

SESSION_TTL = 3600
MAX_TELEGRAM_ID = 2**52 - 1


def verified_user_id(authorization: str, token: str) -> int:
    scheme, _, data = authorization.partition(" ")
    if scheme.lower() != "tma" or not data or len(data) > 8192:
        raise ValueError("Invalid Telegram authorization")

    pairs = parse_qsl(data, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=32)
    fields = dict(pairs)
    signature = fields.pop("hash", "")
    if len(fields) + 1 != len(pairs) or len(signature) != 64 or not signature.isascii():
        raise ValueError("Invalid Telegram launch data")
    if any(not key.isascii() or not key.isidentifier() or "\n" in value or "\r" in value
           for key, value in fields.items()):
        raise ValueError("Invalid Telegram launch fields")

    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.digest(b"WebAppData", token.encode(), "sha256")
    expected = hmac.new(key, check_string.encode(), "sha256").hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid Telegram signature")

    try:
        issued = int(fields.get("auth_date", ""))
        user = json.loads(fields.get("user", "null"))
    except (ValueError, RecursionError):
        raise ValueError("Invalid Telegram user data") from None
    if not -30 <= time.time() - issued <= SESSION_TTL or not isinstance(user, dict):
        raise ValueError("Expired Telegram launch data")
    user_id = user.get("id")
    if type(user_id) is not int or not 0 < user_id <= MAX_TELEGRAM_ID or user.get("is_bot") is True:
        raise ValueError("Invalid Telegram user")
    return user_id
