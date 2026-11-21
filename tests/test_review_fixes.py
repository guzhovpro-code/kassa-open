"""Исправления по ревью этапа 2 (все P2). По одному блоку тестов на пункт.

1. records: повтор client_id сверяет и категорию, способ, заметку -> idem_conflict.
2. money: только ASCII-цифры.
3. records: fullmatch вместо «$»; валюта проверяется на ASCII до upper().
4. server: глубоко вложенный JSON -> 400, а не обрыв соединения.
5. import_finbot verify: построчная сверка.
"""
import http.client
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from helpers import BOT_ID, OWNER, make_init_data, new_key
from kassa_api import db, money, records
from kassa_api.records import RecordError
from kassa_api.server import Config, make_servers

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
ACTOR = 1001


@pytest.fixture()
def conn(tmp_path):
    p = tmp_path / "kassa-test.db"
    db.init_db(p)
    c = db.connect(p)
    yield c
    c.close()


def body(**kw):
    base = {"client_id": str(uuid.uuid4()), "direction": "out", "amount": "500", "currency": "TRY",
            "category": "кафе", "date": "2026-10-14", "method": "cash"}
    base.update(kw)
    return base


def count(conn, where="1=1"):
    return conn.execute("SELECT COUNT(*) FROM transactions WHERE " + where).fetchone()[0]


def err(conn, payload, now=NOW):
    with pytest.raises(RecordError) as e:
        records.create_record(conn, ACTOR, payload, now=now)
    return e.value


# --- 1. идемпотентность: повтор сверяется целиком ------------------------------------------------------------
@pytest.mark.parametrize("patch", [
    {"category": "бар"}, {"note": "другая заметка"}, {"note": None}, {"method": "monzo"}, {"date": "2026-10-13"},
    {"currency": "USD"}, {"amount": "501"}, {"direction": "in", "method": "monzo", "category": "доход"},
])
def test_idem_repeat_with_other_field_is_conflict(conn, patch):
    p = body(note="кофе")
    records.create_record(conn, ACTOR, p, now=NOW)
    e = err(conn, dict(p, **patch), now=NOW + timedelta(seconds=2))
    assert (e.status, e.code) == (409, "idem_conflict")
    assert count(conn) == 1


def test_idem_repeat_same_everything_is_still_repeat(conn):
    p = body(note="кофе", method="monzo")
    first = records.create_record(conn, ACTOR, p, now=NOW)
    again = records.create_record(conn, ACTOR, dict(p, confirm=["outlier"]), now=NOW + timedelta(minutes=30))
    assert again == {"ok": True, "id": first["id"], "duplicate_request": True, "category": "кафе"}


def test_idem_repeat_equal_after_whitespace_cleaning(conn):
    p = body(note="кофе  у дома", category=" кафе ")
    first = records.create_record(conn, ACTOR, p, now=NOW)
    again = records.create_record(conn, ACTOR, dict(p, note="кофе у дома", category="кафе"), now=NOW)
    assert again["id"] == first["id"] and again["duplicate_request"] is True


def test_idem_income_method_change_is_conflict(conn):
    p = body(direction="in", category="доход", method="monzo", currency="USD")
    records.create_record(conn, ACTOR, p, now=NOW)
    assert err(conn, dict(p, method="okx")).code == "idem_conflict"
    assert err(conn, dict(p, method=None, new_method="Мой банк")).code == "idem_conflict"
    assert count(conn) == 1


def test_idem_new_method_repeat_and_change(conn):
    p = body(direction="in", category="доход", method=None, new_method="Мой банк")
    first = records.create_record(conn, ACTOR, p, now=NOW)
    assert records.create_record(conn, ACTOR, dict(p, new_method="мой  БАНК"), now=NOW)["id"] == first["id"]
    assert err(conn, dict(p, new_method="Не мой банк")).code == "idem_conflict"
    assert err(conn, dict(p, new_method=None, method="monzo")).code == "idem_conflict"
    assert conn.execute("SELECT COUNT(*) FROM methods WHERE code LIKE 'x:%'").fetchone()[0] == 1
    assert count(conn) == 1


# --- 2. только ASCII-цифры ----------------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    "٥٠٠",            # арабо-индийские цифры
    "॥००",            # деванагари
    "５００",            # полноширинные
    "5٠٠",                 # смесь
    "1,٢٣", "1.٢٣٤",
])
def test_non_ascii_digits_are_rejected(raw):
    with pytest.raises(money.MoneyError) as e:
        money.parse_amount_to_cents(raw)
    assert e.value.args[0] == "bad_amount"


def test_non_ascii_digits_never_reach_the_database(conn):
    assert err(conn, body(amount="٥٠٠")).code == "bad_amount"
    assert count(conn) == 0


def test_trailing_and_inner_newline():
    assert money.parse_amount_to_cents("500\n") == 50000          # пробельные края срезаются (как и раньше)
    for bad in ("5\n00", "1,5\n3"):
        with pytest.raises(money.MoneyError):
            money.parse_amount_to_cents(bad)


# --- 3. fullmatch и валюта ----------------------------------------------------------------------------------
@pytest.mark.parametrize("cid", ["a" * 16 + "\n", "a" * 16 + "\r\n", "\n" + "a" * 16, "a" * 16 + "\n" + "b"])
def test_client_id_with_newline_is_rejected(conn, cid):
    assert err(conn, body(client_id=cid)).code == "bad_client_id"
    assert count(conn) == 0


def test_date_with_newline_is_rejected(conn):
    assert err(conn, body(date="2026-10-14\n")).code == "bad_date"
    assert err(conn, body(date="٢٠٢٦-١٠-١٤")).code == "bad_date"


def test_module_patterns_have_no_dollar_anchor():
    for rx in (records.CLIENT_ID_RE, records.CURRENCY_RE, records.DATE_RE):
        assert not rx.pattern.endswith("$") and not rx.pattern.startswith("^")
        assert "\\d" not in rx.pattern


@pytest.mark.parametrize("cur", [
    "ſcr",                # «ſ» (длинная s): upper() даёт «S»
    "ﬁa",                 # лигатура «ﬁ»: upper() даёт «FI»
    "trı",                # «ı»: upper() даёт «I»
    "ＴＲＹ",      # полноширинные буквы
    "ТRY",                # кириллическая Т
    "TRÝ",               # комбинирующий знак
    "T R Y", "TRY1", "TR", "ABCDEF", "", " ", "TRY\nUSD",
])
def test_currency_non_ascii_tricks_rejected(conn, cur):
    assert err(conn, body(currency=cur)).code == "bad_currency"
    assert count(conn) == 0


def test_upper_tricks_really_produce_valid_looking_codes():
    """Контроль самого теста: без проверки на ASCII эти строки превращались бы в «хороший» код."""
    assert "ſcr".upper() == "SCR" and "ﬁa".upper() == "FIA" and "trı".upper() == "TRI"


def test_currency_ascii_case_and_spaces_still_ok(conn):
    res = records.create_record(conn, ACTOR, body(currency="  usdt "), now=NOW)
    assert conn.execute("SELECT currency FROM transactions WHERE id=?", (res["id"],)).fetchone()[0] == "USDT"


# --- 4. глубоко вложенный JSON ------------------------------------------------------------------------------
PRIV, PUB = new_key()
TOKEN = "R" * 8 + "abcdefghijklmnopqrstuvwxyz012345"


@pytest.fixture()
def env(tmp_path):
    db_path = str(tmp_path / "kassa-test.db")
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), "J" * 40, PUB, db_path)
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0))
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield pub.server_address[1], db_path
    for s in (pub, internal):
        s.shutdown()
        s.server_close()


def post(port, raw):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", f"/{TOKEN}/api/records", body=raw,
              headers={"Authorization": "tma " + make_init_data(PRIV), "Content-Type": "application/json",
                       "Content-Length": str(len(raw))})
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, json.loads(out)


@pytest.mark.parametrize("raw,unclosed", [
    (b"[" * 8000, True),                          # 8000 байт < MAX_BODY: json.loads -> RecursionError
    (b"[" * 8190, True),
    (b'{"a":' * 1600, True),
    (b'{"a":[' * 1300, True),
    (b"[" * 4000 + b"]" * 4000, False),           # закрытая вложенность: свой отказ (400 или 422), но не обрыв
], ids=["open-8000", "open-8190", "obj-1600", "mixed-1300", "closed-4000"])
def test_deeply_nested_json_is_400_not_dropped_connection(env, raw, unclosed):
    port, path = env
    assert len(raw) <= 8192
    status, out = post(port, raw)
    assert status in (400, 422) and out["ok"] is False
    if unclosed:
        assert (status, out["error"]) == (400, "bad_json")
    c = db.connect(path, readonly=True)
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    # сервер жив и принимает обычные запросы
    ok = json.dumps(body()).encode()
    assert post(port, ok)[0] == 200


def test_moderately_nested_json_is_a_normal_422(env):
    port, _ = env
    status, out = post(port, b"[" * 50 + b"]" * 50)
    assert (status, out["error"]) == (422, "bad_payload")
    status, out = post(port, json.dumps(dict(body(), note={"a": [[[1]]]})).encode())
    assert (status, out["error"]) == (422, "bad_note")






















