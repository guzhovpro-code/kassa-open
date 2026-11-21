"""Проверка подписи initData мини-приложения Telegram (Ed25519, без токена бота).

Источник: https://core.telegram.org/bots/webapps, раздел «Validating data for Third-Party Use».
Строка проверки: "<bot_id>:WebAppData" + "\n" + поля initData (кроме hash и signature),
отсортированные по ключу, в виде key=value, через "\n". Подпись - base64url в поле signature.
"""
import base64
import binascii
import json
import time
from urllib.parse import parse_qsl

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Открытые ключи Telegram (там же, в документации). Тестовый ключ серверу не нужен.
TELEGRAM_PUBLIC_KEY_PROD_HEX = "e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d"

MAX_AGE_SECONDS = 24 * 3600      # срок жизни initData
MAX_FUTURE_SECONDS = 300         # допуск на разницу часов
MAX_INIT_DATA_LEN = 8192


class AuthError(Exception):
    """Подпись не прошла. Причина - только для тестов и журнала, наружу не уходит."""


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + pad)
    except (binascii.Error, ValueError) as exc:
        raise AuthError("signature_encoding") from exc


def build_data_check_string(bot_id: str, fields: dict) -> str:
    rows = [f"{k}={fields[k]}" for k in sorted(fields) if k not in ("hash", "signature")]
    return f"{bot_id}:WebAppData\n" + "\n".join(rows)


def verify_init_data(init_data: str, bot_id: str, public_key_hex: str = TELEGRAM_PUBLIC_KEY_PROD_HEX,
                     now: float | None = None) -> dict:
    """Возвращает {'user': {...}, 'auth_date': int} или бросает AuthError."""
    if not init_data or len(init_data) > MAX_INIT_DATA_LEN:
        raise AuthError("empty_or_too_long")
    pairs = parse_qsl(init_data, keep_blank_values=True)
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise AuthError("duplicate_field")
        fields[key] = value
    signature = fields.get("signature")
    if not signature:
        raise AuthError("no_signature")
    sig_bytes = _b64url_decode(signature)
    check_string = build_data_check_string(bot_id, fields).encode("utf-8")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(sig_bytes, check_string)
    except InvalidSignature as exc:
        raise AuthError("bad_signature") from exc
    except ValueError as exc:
        raise AuthError("bad_key") from exc

    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError) as exc:
        raise AuthError("no_auth_date") from exc
    current = time.time() if now is None else now
    if current - auth_date > MAX_AGE_SECONDS:
        raise AuthError("expired")
    if auth_date - current > MAX_FUTURE_SECONDS:
        raise AuthError("from_future")

    try:
        user = json.loads(fields.get("user", ""))
    except (ValueError, TypeError) as exc:
        raise AuthError("no_user") from exc
    if not isinstance(user, dict) or not isinstance(user.get("id"), int) or isinstance(user.get("id"), bool):
        raise AuthError("no_user_id")
    return {"user": user, "auth_date": auth_date}


def is_allowed(user: dict, allowed_ids: frozenset) -> bool:
    return (not user.get("is_bot")) and user.get("id") in allowed_ids
