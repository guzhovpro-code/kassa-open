"""Подписывает синтетический initData тестовым ключом (как это делает Telegram, но не Telegram)."""
import base64
import json
import time
from urllib.parse import urlencode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kassa_api import tgauth

BOT_ID = "123456789"
OWNER = 1001          # синтетические id, не настоящие
STRANGER = 2002


def new_key():
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return priv, pub_hex


def make_init_data(priv, user_id=OWNER, auth_date=None, bot_id=BOT_ID, is_bot=False, extra=None,
                   drop_signature=False, tamper=None):
    """tamper подменяет поля уже ПОСЛЕ подписи (имитация подделки)."""
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAH-test",
        "user": json.dumps({"id": user_id, "first_name": "T", "is_bot": is_bot}, separators=(",", ":")),
    }
    fields.update(extra or {})
    check = tgauth.build_data_check_string(bot_id, fields).encode()
    sig = base64.urlsafe_b64encode(priv.sign(check)).rstrip(b"=").decode()
    fields["hash"] = "0" * 64     # у Telegram есть и старый hash; на проверку Ed25519 он не влияет
    if not drop_signature:
        fields["signature"] = sig
    if tamper:
        fields.update(tamper)
    return urlencode(fields)


# --- замороженные часы (тесты, зависящие от «сейчас»: время суток и дата) -----------------------------------------------
def freeze_local(monkeypatch, local_text):
    """Подменяет «сейчас» в модулях бэкенда на местное время местного времени 'YYYY-MM-DD HH:MM' (UTC+3). Часы сервера в
    потоках теста тоже заморожены (подмена на уровне модулей). time.time() не трогается: подпись initData живая."""
    from datetime import datetime, timedelta, timezone
    from kassa_api import cards, db, records
    fixed = datetime.strptime(local_text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=3)))

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    for module in (db, records, cards):
        monkeypatch.setattr(module, "datetime", Frozen)
    return fixed


# Выдуманный набор способов для тестов (способы - данные базы, в коде их нет; conftest кладёт его в каждую новую базу).
# Порядок и sort задают порядок на форме; ledger - только для поступлений.
TEST_METHODS_SEED = [
    ("cash", "Наличные", 10, "both"),
    ("monzo", "Карта Monzo", 20, "both"),
    ("revolut", "Карта Revolut", 30, "both"),
    ("n26", "Карта N26", 40, "both"),
    ("okx", "Биржа OKX", 50, "both"),
    ("wise", "Карта Wise", 60, "both"),
    ("ledger", "Кошелёк Ledger", 70, "in"),
]
METHODS = [{"code": c, "title": t, "scope": sc} for c, t, _, sc in TEST_METHODS_SEED]
