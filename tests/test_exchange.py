"""Обмен валют (схема 7, ТЗ docs/exchange/tz.md): миграция, разбор фразы, доли и оценка, комиссия, закрытый месяц,
идемпотентность, запись только подписью Telegram, отчёты не считают обмен. Браузерная форма и список - test_ui_exchange.py."""
import http.client
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helpers import OWNER, STRANGER, make_init_data
from kassa_api import cards, db, exchanges, exchangetext, records, reports
from kassa_api.records import RecordError
from test_api import INTERNAL, PRIV, TOKEN, req
from test_stage8 import ireq, phrase, pub_get, pub_post, srv  # noqa: F401  (фикстура srv)

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)      # локально 15:00 14.10 (UTC+3)
TODAY = "2026-10-14"
RATES = {"TRY": 40.0, "RUB": 80.0, "EUR": 0.9}                  # единиц за 1 USD (кэш курсов, сеть не нужна)
EXAMPLE = "поменял 201 230 рублей на 75 000 лир и 700 долларов, комиссия 758,65"
URL = "https://example.test/tok/"


@pytest.fixture()
def path(tmp_path):
    p = tmp_path / "kassa-test.db"
    db.init_db(p)
    return p


@pytest.fixture()
def conn(path):
    c = db.connect(path)
    yield c
    c.close()


def body(**kw):
    """Обмен из примера владельца: 201 230 RUB -> 75 000 TRY + 700 USD, комиссия 758,65 RUB, отдано с Тинькофф."""
    base = {"client_id": str(uuid.uuid4()), "date": TODAY,
            "give": {"amount": "201230", "currency": "RUB", "method": "tinkoff"},
            "get": [{"amount": "75000", "currency": "TRY"}, {"amount": "700", "currency": "USD"}],
            "fee": {"amount": "758,65"}, "note": "обмен у знакомого"}
    base.update(kw)
    return base


def make(conn, payload=None, rates=RATES, **kw):
    return exchanges.create_exchange(conn, OWNER, payload or body(), now=NOW, fx_rates=rates, **kw)


def one(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def tup(conn, sql, args=()):
    return tuple(conn.execute(sql, args).fetchone())


def err(conn, payload, now=NOW, rates=RATES):
    with pytest.raises(RecordError) as e:
        exchanges.create_exchange(conn, OWNER, payload, now=now, fx_rates=rates)
    return e.value


def close_month(conn, month="2026-10"):
    conn.execute("INSERT INTO closed_months(month, closed_at, source_name, source_sha, expected, rows_in, rows_out) "
                 "VALUES (?, '2026-11-01T00:00:00Z', 'x', 'y', '{}', 0, 0)", (month,))


# --- схема 7 ---------------------------------------------------------------------------------------------------------
def make_v6(p):
    """База схемы 6, как на сервере до этапа: данные, закрытый месяц, способы."""
    c = db.connect(p)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("BEGIN IMMEDIATE")
    db._create_v1(c, True)
    for step in (db._migrate_v2, db._migrate_v3, db._migrate_v4, db._migrate_v5, db._migrate_v6):
        step(c)
    c.execute("PRAGMA user_version=6")
    c.execute("COMMIT")
    for i, (d, cur) in enumerate((("in", "USDT"), ("out", "TRY")), 1):
        c.execute("INSERT INTO transactions(idem_key, direction, amount_cents, currency, usd_cents, category, note, "
                  "method, occurred_on, created_at, source, via, actor_id) VALUES (?,?,?,?,?,?,?,?, '2026-10-0%d', "
                  "'2026-10-0%dT00:00:00Z','kassa','webapp','1')" % (i, i),
                  ("old-%d" % i, d, 1000 * i, cur, 500 * i, "доход" if d == "in" else "кафе", "заметка %d" % i,
                   "bybit" if d == "in" else "cash"))
    c.close()


def test_migration_6_to_7_keeps_data_and_adds_tables(tmp_path):
    p = str(tmp_path / "old-test.db")
    make_v6(p)
    c = db.connect(p, readonly=True)
    old_rows = [tuple(r) for r in c.execute("SELECT * FROM transactions ORDER BY id")]
    old_methods = [tuple(r) for r in c.execute("SELECT * FROM methods ORDER BY code")]
    c.close()
    db.init_db(p)
    c = db.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 7
    assert [r["name"] for r in c.execute("PRAGMA table_info(transactions)")][-2:] == ["exchange_id", "exchange_removed"]
    new_rows = [tuple(r) for r in c.execute("SELECT * FROM transactions ORDER BY id")]
    assert [r[:-2] for r in new_rows] == old_rows and all(r[-2:] == (None, None) for r in new_rows)
    assert [tuple(r) for r in c.execute("SELECT * FROM methods ORDER BY code")] == old_methods
    assert [r["name"] for r in c.execute("PRAGMA table_info(exchanges)")] == [
        "id", "idem_key", "occurred_on", "created_at", "give_cents", "give_currency", "give_method", "note", "status",
        "cancelled_at", "via", "actor_id"]
    assert [r["name"] for r in c.execute("PRAGMA table_info(exchange_legs)")] == [
        "id", "exchange_id", "get_cents", "get_currency", "get_method", "give_part_cents", "part_estimated"]
    assert c.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0] == 0
    schema_1 = [tuple(r) for r in c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name")]
    c.close()
    db.init_db(p)                                                     # повторный запуск безвреден
    c = db.connect(p, readonly=True)
    assert [tuple(r) for r in c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name")] == schema_1
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
    c.close()


def test_fresh_and_migrated_databases_have_same_exchange_shape(tmp_path):
    a, b = str(tmp_path / "a-test.db"), str(tmp_path / "b-test.db")
    db.init_db(a)
    make_v6(b)
    db.init_db(b)

    def shape(p):
        c = db.connect(p, readonly=True)
        try:
            return {t: [tuple(r)[1:5] for r in c.execute("PRAGMA table_info(%s)" % t)]
                    for t in ("transactions", "exchanges", "exchange_legs")}
        finally:
            c.close()
    assert shape(a) == shape(b)


def test_failed_migration_7_rolls_back_to_version_6(tmp_path, monkeypatch):
    p = str(tmp_path / "old-test.db")
    make_v6(p)
    monkeypatch.setattr(db, "SCHEMA_V7_STEPS", db.SCHEMA_V7_STEPS + ["CREATE TABLE exchanges (x)"])   # занято: сбой в конце
    with pytest.raises(Exception):
        db.init_db(p)
    c = db.connect(p, readonly=True)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    assert "exchange_id" not in [r["name"] for r in c.execute("PRAGMA table_info(transactions)")]
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='exchange_legs'").fetchone()[0] == 0
    c.close()
    monkeypatch.undo()
    db.init_db(p)
    c = db.connect(p, readonly=True)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 7
    c.close()


def test_database_constraints_refuse_nonsense(conn):
    ex = make(conn)["id"]
    with pytest.raises(Exception):
        conn.execute("UPDATE exchanges SET give_cents=0 WHERE id=?", (ex,))
    with pytest.raises(Exception):
        conn.execute("UPDATE exchange_legs SET get_cents=-5 WHERE exchange_id=?", (ex,))
    with pytest.raises(Exception):
        conn.execute("UPDATE exchanges SET status='deleted' WHERE id=?", (ex,))
    with pytest.raises(Exception):
        conn.execute("INSERT INTO exchange_legs(exchange_id, get_cents, get_currency) VALUES (9999, 1, 'USD')")


# --- разбор фразы ------------------------------------------------------------------------------------------------------
def parse(text, today=TODAY):
    return exchangetext.parse(text, today)


def shape(text, today=TODAY):
    r = parse(text, today)
    return (r["give"]["amount"], r["give"]["currency"], r["give"]["method"],
            [(g["amount"], g["currency"], g["method"], g["part"]) for g in r["get"]],
            None if r["fee"] is None else (r["fee"]["amount"], r["fee"]["currency"]), r["date"])


def test_example_is_parsed():
    r = parse(EXAMPLE)
    assert r["kind"] == "exchange" and r["note"] == EXAMPLE and r["date"] == TODAY
    assert r["give"] == {"amount": "201230", "currency": "RUB", "method": None}
    assert r["get"] == [{"amount": "75000", "currency": "TRY", "method": None, "part": None},
                        {"amount": "700", "currency": "USD", "method": None, "part": None}]
    assert r["fee"] == {"amount": "758.65", "currency": "RUB"}              # без валюты - валюта отдачи


PARSED = [
    ("Поменял 5000 рублей на 100 долларов", ("5000", "RUB", None, [("100", "USD", None, None)], None, TODAY)),
    ("обменял 100 usd на 3 500 лир", ("100", "USD", None, [("3500", "TRY", None, None)], None, TODAY)),
    ("сменяла 200 евро на 8000 лир", ("200", "EUR", None, [("8000", "TRY", None, None)], None, TODAY)),
    ("конвертировал 100 долларов в 4000 лир вчера", ("100", "USD", None, [("4000", "TRY", None, None)], None,
                                                    "2026-10-13")),
    ("поменяли 2000 долларов на 80 000 лир позавчера", ("2000", "USD", None, [("80000", "TRY", None, None)], None,
                                                       "2026-10-12")),
    ("купил 700 долларов за 30000 рублей", ("30000", "RUB", None, [("700", "USD", None, None)], None, TODAY)),
    ("купила 100 евро за 4500 лир, комиссия 50 лир", ("4500", "TRY", None, [("100", "EUR", None, None)],
                                                    ("50", "TRY"), TODAY)),
    ("продал 500 usd за 20 000 лир", ("500", "USD", None, [("20000", "TRY", None, None)], None, TODAY)),
    ("продал 300 евро за 12000 лир, комиссия 100 лир", ("300", "EUR", None, [("12000", "TRY", None, None)],
                                                       ("100", "TRY"), TODAY)),
    ("обменял 5000 рублей на 100 долларов с тинькофф", ("5000", "RUB", "tinkoff", [("100", "USD", None, None)], None,
                                                       TODAY)),
    ("обменял 5000 рублей на 100 долларов со сбера", ("5000", "RUB", "sber", [("100", "USD", None, None)], None,
                                                     TODAY)),
    ("поменял 5000 рублей на 100 долларов наличными", ("5000", "RUB", None, [("100", "USD", "cash", None)], None,
                                                      TODAY)),
    ("купил 700 долларов за 30000 рублей налом", ("30000", "RUB", "cash", [("700", "USD", None, None)], None, TODAY)),
    ("поменял 201230 рублей на 75000 лир и 700 долларов, из них 150 000 на лиры",
     ("201230", "RUB", None, [("75000", "TRY", None, "150000"), ("700", "USD", None, None)], None, TODAY)),
    ("обменял 100 евро на 4000 лир и 50 долларов, из них 2000 на лиры",
     ("100", "EUR", None, [("4000", "TRY", None, "2000"), ("50", "USD", None, None)], None, TODAY)),
    ("поменял 300 евро на 5000 лир, 100 долларов и 2000 рублей",
     ("300", "EUR", None, [("5000", "TRY", None, None), ("100", "USD", None, None), ("2000", "RUB", None, None)],
      None, TODAY)),
    ("поменял 100 долларов на 3 500,50 лир, комиссия банка 15 рублей",
     ("100", "USD", None, [("3500.5", "TRY", None, None)], ("15", "RUB"), TODAY)),
    ("обменял 100 usd на 3000 лир, комиссии нет", ("100", "USD", None, [("3000", "TRY", None, None)], None, TODAY)),
    ("обменял 100 usd на 3000 лир, комиссия 0", ("100", "USD", None, [("3000", "TRY", None, None)], None, TODAY)),
    ("поменял 100 евро на 4000 лир и 50 долларов налом",
     ("100", "EUR", None, [("4000", "TRY", None, None), ("50", "USD", "cash", None)], None, TODAY)),
]


@pytest.mark.parametrize("text,expected", PARSED)
def test_exchange_phrases_are_parsed(text, expected):
    assert shape(text) == expected


REJECTED = [
    "купил доллары за 5000 рублей",                    # сколько долларов - не сказано
    "обменял 500 на 20 лир",                          # нет валюты у суммы
    "поменял 5000 рублей на 5000 рублей",             # одна валюта с двух сторон
    "поменял 100 долларов на лиры",                   # валюта получения названа без суммы
    "поменял 100 долларов на 4000 лир, из них 50 на евро",    # доля на валюту, которой нет в получении
    "поменял 100 долларов на 4000 лир и 20 лир",     # два раза одна валюта в получении
    "конвертировал 100 долларов в 3 500 лир, из них 50 евро на лиры",   # валюта доли не равна валюте отдачи
]
NOT_EXCHANGE = [
    "купил кофе за 300 лир", "поменял лампочку за 300 лир", "перевёл 5000 рублей маме", "кофе 150",
    "обменял 100 долларов 200 евро", "потратил 20 лир на обед", "заплатил 100 долларов за квартиру", "пришло 5000 рублей за урок",
    "поменял масло в машине, заплатил 800 лир", "продал телефон за 5000 рублей", "перевёл 100 долларов на карту",
]


@pytest.mark.parametrize("text", REJECTED)
def test_unsure_exchange_phrases_are_refused(text):
    with pytest.raises(exchangetext.ExchangeReject):
        parse(text)


@pytest.mark.parametrize("text", NOT_EXCHANGE)
def test_ordinary_money_phrases_are_not_exchanges(text):
    assert parse(text) is None


def test_exchange_phrase_wins_over_money_parse_and_ordinary_flow_is_unchanged(conn):
    r = cards.create_from_text(conn, OWNER, OWNER, "phrase", EXAMPLE, "1", now=NOW)
    d = json.loads(one(conn, "SELECT draft FROM cards WHERE id=?", (r["card_id"],))[0])
    assert d["kind"] == "exchange" and d["give"]["currency"] == "RUB"
    r2 = cards.create_from_text(conn, OWNER, OWNER, "phrase", "потратил 20 лир на обед", "2", now=NOW)
    d2 = json.loads(one(conn, "SELECT draft FROM cards WHERE id=?", (r2["card_id"],))[0])
    assert "kind" not in d2 and d2["direction"] == "out" and d2["amount"] == "20"


def test_unsure_exchange_gives_no_card_and_a_reply(conn):
    r = cards.create_from_text(conn, OWNER, OWNER, "phrase", "обменял 500 на 20 лир", "3", now=NOW)
    assert r["ok"] is False and r["reject"] == "unsure_exchange" and "ничего не записано" in r["reply"]
    assert one(conn, "SELECT COUNT(*) FROM cards")[0] == 0 and one(conn, "SELECT COUNT(*) FROM exchanges")[0] == 0




def test_methods_not_fitting_direction_are_dropped_from_draft(conn):
    r = cards.create_from_text(conn, OWNER, OWNER, "phrase", "поменял 100 долларов на 4000 лир с холодного кошелька", "4",
                               now=NOW)
    d = json.loads(one(conn, "SELECT draft FROM cards WHERE id=?", (r["card_id"],))[0])
    assert d["give"]["method"] is None                      # холодный кошелёк - только поступления


# --- доли и оценка ----------------------------------------------------------------------------------------------------
def legs(conn, ex_id):
    return [dict(r) for r in conn.execute("SELECT * FROM exchange_legs WHERE exchange_id=? ORDER BY id", (ex_id,))]


def test_create_exchange_stores_rows_and_estimates_shares_by_usd_value(conn):
    res = make(conn)
    assert res["ok"] and not res["duplicate_request"] and res["estimated"] is True and res["fee_id"]
    row = dict(one(conn, "SELECT * FROM exchanges WHERE id=?", (res["id"],)))
    assert (row["give_cents"], row["give_currency"], row["give_method"], row["status"], row["via"]) == \
           (20123000, "RUB", "tinkoff", "active", "webapp")
    assert row["actor_id"] == str(OWNER) and row["occurred_on"] == TODAY and row["idem_key"].startswith("webapp:")
    a, b = legs(conn, res["id"])
    assert (a["get_cents"], a["get_currency"], b["get_cents"], b["get_currency"]) == (7500000, "TRY", 70000, "USD")
    assert a["part_estimated"] == b["part_estimated"] == 1
    assert a["give_part_cents"] + b["give_part_cents"] == 20123000          # сумма долей равна отданному точно
    # TRY: 75000/40 = 1875 USD, USD: 700 -> доля TRY = 1875/(1875+700) отданного
    assert a["give_part_cents"] in (20123000 * 187500 // 257500, 20123000 * 187500 // 257500 + 1)
    assert a["give_part_cents"] > b["give_part_cents"] > 0


def test_single_leg_gets_everything_and_is_not_estimated(conn):
    res = make(conn, body(get=[{"amount": "5000", "currency": "USD"}], fee=None))
    (leg,) = legs(conn, res["id"])
    assert leg["give_part_cents"] == 20123000 and leg["part_estimated"] == 0 and res["estimated"] is False


def test_specified_part_is_kept_and_the_rest_goes_to_the_other_leg(conn):
    payload = body(get=[{"amount": "75000", "currency": "TRY", "part": "150 000"}, {"amount": "700", "currency": "USD"}])
    a, b = legs(conn, make(conn, payload)["id"])
    assert (a["give_part_cents"], a["part_estimated"]) == (15000000, 0)
    assert (b["give_part_cents"], b["part_estimated"]) == (5123000, 1)


def test_all_parts_specified_must_add_up_exactly(conn):
    ok = body(get=[{"amount": "75000", "currency": "TRY", "part": "150000"},
                   {"amount": "700", "currency": "USD", "part": "51230"}])
    assert [l["part_estimated"] for l in legs(conn, make(conn, ok)["id"])] == [0, 0]
    bad = body(get=[{"amount": "75000", "currency": "TRY", "part": "150000"},
                    {"amount": "700", "currency": "USD", "part": "50000"}])
    assert err(conn, bad).code == "parts_mismatch"
    over = body(get=[{"amount": "75000", "currency": "TRY", "part": "250000"}, {"amount": "700", "currency": "USD"}])
    assert err(conn, over).code == "part_too_large"
    nothing_left = body(get=[{"amount": "75000", "currency": "TRY", "part": "201230"}, {"amount": "700", "currency": "USD"}])
    assert err(conn, nothing_left).code == "parts_mismatch"


def test_no_rate_for_a_currency_means_shares_not_computed_but_exchange_is_saved(conn):
    res = make(conn, rates={"TRY": 40.0})                    # у рубля курса нет, у доллара он всегда есть (1:1)
    a, b = legs(conn, res["id"])
    assert one(conn, "SELECT COUNT(*) FROM exchanges")[0] == 1
    assert a["give_part_cents"] and b["give_part_cents"]      # TRY и USD оценены: курс отданной валюты для долей не нужен
    res2 = make(conn, body(get=[{"amount": "5000", "currency": "KZT"}, {"amount": "700", "currency": "USD"}]), rates={})
    a, b = legs(conn, res2["id"])
    assert a["give_part_cents"] is None and b["give_part_cents"] is None and a["part_estimated"] == 1
    item = exchanges.get_item(conn, res2["id"])
    assert item["legs"][0]["rate"] is None and item["legs"][0]["estimated"] is True


def test_rate_is_computed_on_read_not_stored(conn):
    res = make(conn, body(get=[{"amount": "5000", "currency": "USD"}], fee=None))
    item = exchanges.get_item(conn, res["id"])
    (leg,) = item["legs"]
    assert leg["rate"] == pytest.approx(201230 / 5000, rel=1e-6) and leg["rate_inv"] == pytest.approx(5000 / 201230, rel=1e-5)
    assert "rate" not in [r["name"] for r in conn.execute("PRAGMA table_info(exchange_legs)")]


@pytest.mark.parametrize("change,code", [
    ({"give": {"amount": "0", "currency": "RUB"}}, "amount_not_positive"),
    ({"give": {"amount": "1,234", "currency": "RUB"}}, "too_many_decimals"),
    ({"give": {"amount": "100", "currency": "рубль"}}, "bad_currency"),
    ({"give": "100"}, "bad_give"),
    ({"get": []}, "bad_get"),
    ({"get": [{"amount": "1", "currency": "USD"}] * 2}, "duplicate_get_currency"),
    ({"get": [{"amount": "1", "currency": c} for c in ("USD", "TRY", "EUR", "KZT")]}, "too_many_legs"),
    ({"get": [{"amount": "100", "currency": "RUB"}]}, "same_currency"),
    ({"get": [{"amount": "100", "currency": "USD", "method": "nonexistent"}]}, "unknown_method"),
    ({"get": [{"amount": "100", "currency": "USD", "part": "abc"}]}, "bad_part"),
    ({"date": "2026-10-15"}, "date_in_future"),
    ({"date": "2025-01-01"}, "date_too_old"),
    ({"date": "14.10.2026"}, "bad_date"),
    ({"client_id": "short"}, "bad_client_id"),
    ({"fee": {"amount": "5", "method": "nonexistent"}}, "unknown_method"),
    ({"note": "x" * 201}, "bad_note"),
])
def test_bad_requests_are_refused_and_nothing_is_written(conn, change, code):
    assert err(conn, body(**change)).code == code
    assert one(conn, "SELECT COUNT(*) FROM exchanges")[0] == one(conn, "SELECT COUNT(*) FROM transactions")[0] == 0


def test_give_method_must_fit_direction_and_fee_needs_a_method(conn):
    assert err(conn, body(give={"amount": "100", "currency": "RUB", "method": "cold_wallet"})).code == "unknown_method"
    assert err(conn, body(give={"amount": "100", "currency": "RUB"})).code == "method_required"      # комиссия без способа
    ok = body(give={"amount": "100", "currency": "RUB"}, fee=None)
    assert make(conn, ok)["fee_id"] is None                                                           # без комиссии способ не обязателен


# --- комиссия ---------------------------------------------------------------------------------------------------------
def fee_row(conn, ex_id):
    return one(conn, "SELECT * FROM transactions WHERE exchange_id=? AND exchange_removed IS NULL", (ex_id,))


def test_fee_is_an_ordinary_expense_linked_to_the_exchange(conn):
    res = make(conn)
    t = fee_row(conn, res["id"])
    assert t["id"] == res["fee_id"] and (t["direction"], t["amount_cents"], t["currency"]) == ("out", 75865, "RUB")
    assert (t["category"], t["method"], t["status"], t["source"], t["via"]) == (
        exchanges.FEE_CATEGORY, "tinkoff", "active", "kassa", "webapp")
    assert t["occurred_on"] == TODAY and t["usd_cents"] == 948                # 758,65 RUB по курсу кэша 80 за доллар
    assert one(conn, "SELECT COUNT(*) FROM v_legacy")[0] == 1               # в v_legacy только комиссия, обмена там нет


def test_fee_in_other_currency_and_method(conn):
    res = make(conn, body(fee={"amount": "15", "currency": "usd", "method": "cash"}))
    t = fee_row(conn, res["id"])
    assert (t["amount_cents"], t["currency"], t["method"], t["usd_cents"]) == (1500, "USD", "cash", 1500)


def test_cancel_and_restore_move_the_fee_with_the_exchange(conn):
    ex = make(conn)["id"]
    r = exchanges.cancel_exchange(conn, OWNER, ex, now=NOW)
    assert r["status"] == "cancelled" and r["changed"] is True
    assert one(conn, "SELECT status, cancelled_at FROM exchanges WHERE id=?", (ex,))[0] == "cancelled"
    assert fee_row(conn, ex)["status"] == "cancelled" and fee_row(conn, ex)["cancelled_at"]
    assert one(conn, "SELECT COUNT(*) FROM v_legacy")[0] == 0
    assert exchanges.cancel_exchange(conn, OWNER, ex, now=NOW)["changed"] is False                    # повтор безвреден
    exchanges.restore_exchange(conn, OWNER, ex, now=NOW)
    assert tup(conn, "SELECT status, cancelled_at FROM exchanges WHERE id=?", (ex,)) == ("active", None)
    assert fee_row(conn, ex)["status"] == "active" and fee_row(conn, ex)["cancelled_at"] is None
    actions = [r[0] for r in conn.execute("SELECT action FROM audit WHERE entity='exchange' ORDER BY id")]
    assert actions == ["create", "cancel", "restore"]                                                # повтор audit не пишет


def test_fee_can_not_be_changed_through_ordinary_record_endpoints(conn):
    res = make(conn)
    for fn in (lambda: records.cancel_record(conn, OWNER, res["fee_id"]), lambda: records.restore_record(conn, OWNER, res["fee_id"]),
               lambda: records.update_record(conn, OWNER, res["fee_id"], {"amount": "1"})):
        with pytest.raises(RecordError) as e:
            fn()
        assert e.value.code == "linked_to_exchange" and e.value.status == 409
    assert fee_row(conn, res["id"])["amount_cents"] == 75865


def test_unknown_exchange_and_bad_id(conn):
    for bad in (0, -1, 999, True, "5"):
        with pytest.raises(RecordError) as e:
            exchanges.cancel_exchange(conn, OWNER, bad)
        assert e.value.code == "not_found"


# --- закрытый месяц ---------------------------------------------------------------------------------------------------
def test_closed_month_refuses_create_cancel_restore_and_update(conn):
    ex = make(conn)["id"]
    close_month(conn)
    assert err(conn, body()).code == "month_closed" and err(conn, body()).status == 422
    for fn in (lambda: exchanges.cancel_exchange(conn, OWNER, ex, now=NOW), lambda: exchanges.restore_exchange(conn, OWNER, ex, now=NOW),
               lambda: exchanges.update_exchange(conn, OWNER, ex, {"note": "x"}, now=NOW)):
        with pytest.raises(RecordError) as e:
            fn()
        assert e.value.code == "month_closed"
    assert one(conn, "SELECT status FROM exchanges WHERE id=?", (ex,))[0] == "active"
    assert exchanges.get_item(conn, ex)["closed"] is True


def test_update_into_a_closed_month_is_refused(conn):
    ex = make(conn)["id"]
    close_month(conn, "2026-09")
    with pytest.raises(RecordError) as e:
        exchanges.update_exchange(conn, OWNER, ex, {"date": "2026-09-30"}, now=NOW)
    assert e.value.code == "month_closed"


# --- идемпотентность --------------------------------------------------------------------------------------------------
def test_same_client_id_returns_the_same_exchange_without_a_second_row(conn):
    p = body()
    a, b = make(conn, p), make(conn, p)
    assert a["id"] == b["id"] and a["duplicate_request"] is False and b["duplicate_request"] is True
    assert one(conn, "SELECT COUNT(*) FROM exchanges")[0] == 1 and one(conn, "SELECT COUNT(*) FROM exchange_legs")[0] == 2
    assert one(conn, "SELECT COUNT(*) FROM transactions")[0] == 1 and b["fee_id"] == a["fee_id"]


def test_same_client_id_with_other_data_is_a_conflict(conn):
    p = body()
    make(conn, p)
    other = dict(p, give={"amount": "201231", "currency": "RUB", "method": "tinkoff"})
    assert err(conn, other).code == "idem_conflict" and err(conn, other).status == 409
    no_fee = dict(p, fee=None)
    assert err(conn, no_fee).code == "idem_conflict"
    assert one(conn, "SELECT COUNT(*) FROM exchanges")[0] == 1


def test_backend_key_for_transfer_of_old_exchange(conn):
    res = exchanges.create_exchange(conn, "splinter", body(), via="splinter", now=NOW, fx_rates=RATES, idem_key="splinter:ex-2026-11-10")
    assert tup(conn, "SELECT via, idem_key FROM exchanges WHERE id=?", (res["id"],)) == ("splinter", "splinter:ex-2026-11-10")
    again = exchanges.create_exchange(conn, "splinter", body(), via="splinter", now=NOW, fx_rates=RATES, idem_key="splinter:ex-2026-11-10")
    assert again["duplicate_request"] is True and again["id"] == res["id"]


# --- правка ------------------------------------------------------------------------------------------------------------
def test_update_recalculates_estimated_shares_and_edits_the_fee(conn):
    ex = make(conn)["id"]
    r = exchanges.update_exchange(conn, OWNER, ex, {"give": {"amount": "200000", "currency": "RUB", "method": "tinkoff"},
                                                   "fee": {"amount": "800"}, "note": "поправил"}, now=NOW, fx_rates=RATES)
    assert sorted(r["changed"]) == ["fee", "give", "note"]
    a, b = legs(conn, ex)
    assert a["give_part_cents"] + b["give_part_cents"] == 20000000
    assert fee_row(conn, ex)["amount_cents"] == 80000 and one(conn, "SELECT note FROM exchanges WHERE id=?", (ex,))[0] == "поправил"
    assert exchanges.update_exchange(conn, OWNER, ex, {"note": "поправил"}, now=NOW)["changed"] == []   # то же значение - без изменений


def test_update_can_remove_and_readd_the_fee(conn):
    ex = make(conn)["id"]
    fee_id = fee_row(conn, ex)["id"]
    exchanges.update_exchange(conn, OWNER, ex, {"fee": None}, now=NOW)
    assert fee_row(conn, ex) is None
    t = one(conn, "SELECT status, exchange_id, exchange_removed FROM transactions WHERE id=?", (fee_id,))
    assert tuple(t) == ("cancelled", ex, 1)                      # убранная комиссия отменена и помечена убранной
    exchanges.update_exchange(conn, OWNER, ex, {"fee": {"amount": "10"}}, now=NOW)
    assert fee_row(conn, ex)["amount_cents"] == 1000
    exchanges.cancel_exchange(conn, OWNER, ex, now=NOW)
    assert one(conn, "SELECT status FROM transactions WHERE id=?", (fee_id,))[0] == "cancelled"
    assert fee_row(conn, ex)["status"] == "cancelled"


def test_update_date_moves_the_fee_and_cancelled_exchange_can_not_be_edited(conn):
    ex = make(conn)["id"]
    exchanges.update_exchange(conn, OWNER, ex, {"date": "2026-10-10"}, now=NOW)
    assert fee_row(conn, ex)["occurred_on"] == "2026-10-10"
    exchanges.cancel_exchange(conn, OWNER, ex, now=NOW)
    with pytest.raises(RecordError) as e:
        exchanges.update_exchange(conn, OWNER, ex, {"note": "x"}, now=NOW)
    assert e.value.code == "exchange_cancelled"
    for bad in ({}, {"status": "active"}, {"idem_key": "x"}, []):
        with pytest.raises(RecordError):
            exchanges.update_exchange(conn, OWNER, ex, bad, now=NOW)


# --- audit без сумм ---------------------------------------------------------------------------------------------------
def test_audit_has_no_amounts(conn):
    ex = make(conn)["id"]
    exchanges.update_exchange(conn, OWNER, ex, {"give": {"amount": "200000", "currency": "RUB"}, "fee": {"amount": "800", "method": "cash"}},
                              now=NOW, fx_rates=RATES)
    exchanges.cancel_exchange(conn, OWNER, ex, now=NOW)
    details = " ".join(r[0] or "" for r in conn.execute("SELECT detail FROM audit WHERE entity='exchange'"))
    for n in ("201230", "20123000", "75000", "700", "758", "75865", "200000", "800"):
        assert n not in details, n
    assert json.loads(one(conn, "SELECT detail FROM audit WHERE action='create' AND entity='exchange'")[0]) == {
        "legs": 2, "fee": True, "estimated": True, "backdated": False}


# --- карточка и черновик ----------------------------------------------------------------------------------------------
def card(conn, text=EXAMPLE, mid="10"):
    return cards.create_from_text(conn, OWNER, OWNER, "phrase", text, mid, now=NOW)["card_id"]


def test_card_text_has_amounts_rate_fee_and_signed_buttons(conn):
    cid = card(conn)
    v = cards.view(conn, cid, URL, True, NOW, fx_rates=RATES)
    t = v["text"]
    assert "Обмен: отдал 201 230 ₽ → получил 75 000 ₺, 700 $ · сегодня" in t and "Комиссия: 758,65 ₽" in t
    assert "Курс: 1 ₺ ≈" in t and "1 $ ≈" in t and "доли не указаны" in t and "ТЕСТ" in t
    flat = {b["text"]: b for row in v["buttons"] for b in row}
    for name in ("✅ Записать", "✏️ Исправить"):
        assert flat[name]["web_app"].startswith(URL + "?draft=%d" % cid) and "cb" not in flat[name]
    assert [b["cb"] for b in flat.values() if "cb" in b] == ["kassa:notmoney:%d" % cid]
    assert one(conn, "SELECT COUNT(*) FROM exchanges")[0] == one(conn, "SELECT COUNT(*) FROM transactions")[0] == 0


def test_card_without_rates_says_rate_not_computed(conn):
    t = cards.view(conn, card(conn), URL, False, NOW, fx_rates={})["text"]
    assert "курс не посчитан" in t and "ТЕСТ" not in t


def test_voice_card_has_verno_and_cancel(conn):
    cid = cards.create_from_text(conn, OWNER, OWNER, "voice", EXAMPLE, "11", now=NOW)["card_id"]
    flat = [b for row in cards.view(conn, cid, URL, True, NOW)["buttons"] for b in row]
    assert {b["text"] for b in flat} == {"✅ Верно", "✏️ Исправить", "✖ Отмена"}
    assert [b["cb"] for b in flat if "cb" in b] == ["kassa:no:%d" % cid]


def test_confirm_exchange_draft_writes_closes_card_and_edits_the_chat(conn):
    cid = card(conn)
    conn.execute("UPDATE cards SET message_id='55' WHERE id=?", (cid,))
    data = body(give={"amount": "201230", "currency": "RUB", "method": "tinkoff"})
    res = cards.confirm_exchange_draft(conn, OWNER, cid, data, URL, True, fx_rates=RATES, now=NOW)
    assert res["ok"] and not res["duplicate_request"]
    assert tup(conn, "SELECT status, tx_id FROM cards WHERE id=?", (cid,)) == ("confirmed", res["id"])
    assert one(conn, "SELECT idem_key FROM exchanges")[0] == "card:%d" % cid
    out = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM outbox WHERE kind='edit'")]
    assert out and out[-1]["text"].startswith("✅ Записан обмен №%d" % res["id"]) and "Тинькофф" in out[-1]["text"]
    again = cards.confirm_exchange_draft(conn, OWNER, cid, data, URL, True, fx_rates=RATES, now=NOW)
    assert again["duplicate_request"] is True and one(conn, "SELECT COUNT(*) FROM exchanges")[0] == 1
    with pytest.raises(RecordError) as e:                       # тот же черновик с другими данными - не вторая запись
        cards.confirm_exchange_draft(conn, OWNER, cid, dict(data, note="другая"), URL, True, now=NOW)
    assert e.value.code == "idem_conflict" and one(conn, "SELECT COUNT(*) FROM exchanges")[0] == 1


def test_confirm_checks_owner_status_and_kind(conn):
    cid = card(conn)
    with pytest.raises(cards.CardError) as e:
        cards.confirm_exchange_draft(conn, STRANGER, cid, body(), URL, True, now=NOW)
    assert e.value.code == "not_yours"
    record_cid = card(conn, "потратил 20 лир на обед", "12")
    with pytest.raises(cards.CardError) as e:
        cards.confirm_exchange_draft(conn, OWNER, record_cid, body(), URL, True, now=NOW)
    assert e.value.code == "draft_not_exchange"
    with pytest.raises(cards.CardError) as e:
        cards.confirm_draft(conn, OWNER, cid, {"client_id": str(uuid.uuid4()), "direction": "out", "amount": "1", "currency": "TRY",
                                              "date": TODAY, "method": "cash"}, URL, True, now=NOW)
    assert e.value.code == "draft_is_exchange"
    assert one(conn, "SELECT COUNT(*) FROM exchanges")[0] == one(conn, "SELECT COUNT(*) FROM transactions")[0] == 0
    cards.cancel_draft(conn, OWNER, cid, URL, now=NOW)
    with pytest.raises(cards.CardError) as e:
        cards.confirm_exchange_draft(conn, OWNER, cid, body(), URL, True, now=NOW)
    assert e.value.code == "draft_closed"


def test_draft_for_form_and_inbox_show_the_exchange(conn):
    cid = card(conn)
    f = cards.draft_for_form(conn, OWNER, cid, NOW)
    assert f["kind"] == "exchange" and f["give"]["amount"] == "201230" and len(f["get"]) == 2 and f["fee"]["amount"] == "758.65"
    (item,) = cards.inbox(conn, OWNER, NOW)
    assert item["kind"] == "exchange" and "201 230 ₽" in item["summary"] and "75 000 ₺" in item["summary"]




# --- отчёты ------------------------------------------------------------------------------------------------------------
def test_reports_do_not_count_the_exchange_only_the_fee_and_a_counter(conn):
    make(conn)
    rep = reports.report(conn, "month", "2026-10", TODAY)
    assert rep["exchanges"] == 1 and rep["count"] == 1
    assert [(c["currency"], c["out_cents"], c["in_cents"]) for c in rep["currencies"]] == [("RUB", 75865, 0)]
    day = reports.report(conn, "day", TODAY, TODAY)
    assert day["exchanges"] == 1 and [r["category"] for r in day["records"]] == ["Комиссия обмена"]
    assert reports.report(conn, "month", "2026-09", "2026-10-14")["exchanges"] == 0
    inc = reports.income(conn, "month", "2026-10", TODAY)
    assert inc["income"]["count"] == 0                      # полученные валюты - не поступления
    exchanges.cancel_exchange(conn, OWNER, 1, now=NOW)
    assert reports.report(conn, "month", "2026-10", TODAY)["exchanges"] == 0


def test_recent_marks_the_fee_as_linked(conn):
    ex = make(conn)["id"]
    (rec,) = reports.recent(conn)["records"]
    assert rec["exchange_id"] == ex and rec["category"] == "Комиссия обмена"


def test_list_month_has_items_with_rates_and_cancelled_ones(conn):
    a = make(conn)["id"]
    b = make(conn, body(date="2026-10-02", get=[{"amount": "5000", "currency": "USD"}], fee=None))["id"]
    exchanges.cancel_exchange(conn, OWNER, b, now=NOW)
    res = exchanges.list_month(conn, "2026-10", TODAY)
    assert [i["id"] for i in res["items"]] == [a, b] and res["count"] == 1 and res["month"] == "2026-10"
    assert [i["status"] for i in res["items"]] == ["active", "cancelled"]
    assert res["items"][0]["fee"]["cents"] == 75865 and res["items"][1]["fee"] is None
    assert exchanges.list_month(conn, "2026-09", TODAY)["items"] == []
    with pytest.raises(RecordError):
        exchanges.list_month(conn, "2026-12", TODAY)


# --- API: запись только подписью Telegram -----------------------------------------------------------------------------
def api_body(**kw):
    p = body(**kw)
    p["date"] = db.today_local()
    return p


def exchange_count(path):
    c = db.connect(path, readonly=True)
    try:
        return c.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
    finally:
        c.close()


def test_api_write_needs_a_telegram_signature(srv):
    pub, internal, path = srv
    url = "/%s/api/exchanges" % TOKEN
    assert req(pub, "POST", url, body=api_body())[0] == 401                                        # без подписи
    assert req(pub, "POST", url, body=api_body(), init="garbage")[0] == 401
    assert req(pub, "POST", url, body=api_body(), headers={"Authorization": "Bearer " + INTERNAL})[0] == 401   # токен бота
    assert req(pub, "POST", url, body=api_body(), init=make_init_data(PRIV, user_id=STRANGER))[0] == 403       # чужой id
    assert exchange_count(path) == 0
    status, out = pub_post(srv, "api/exchanges", api_body())
    assert status == 200 and out["ok"] and out["id"] and out["fee_id"] and out["duplicate_request"] is False
    assert exchange_count(path) == 1
    s2, again = pub_post(srv, "api/exchanges", api_body(client_id=one_client(path)))
    assert s2 == 200 and again["id"] == out["id"] and again["duplicate_request"] is True and exchange_count(path) == 1


def one_client(path):
    c = db.connect(path, readonly=True)
    try:
        return c.execute("SELECT idem_key FROM exchanges").fetchone()[0].split(":", 1)[1]
    finally:
        c.close()


def retry(fn, *args, **kw):
    """Машина под нагрузкой: сокет тестового сервера иногда не успевает за 5 с («cannot read from timed out object»).
    Повторяем только сетевой сбой; ответы сервера и проверки не смягчены. Повтор безопасен: карточка по message_id идемпотентна."""
    for attempt in range(4):
        try:
            return fn(*args, **kw)
        except (TimeoutError, OSError, http.client.HTTPException):
            if attempt == 3:
                raise
            time.sleep(1.5)


def test_api_internal_port_has_no_exchange_routes_and_makes_only_drafts(srv):
    pub, internal, path = srv
    for method, route in (("POST", "/internal/exchanges"), ("GET", "/internal/exchanges"), ("POST", "/internal/exchange"),
                          ("POST", "/internal/exchanges/1/cancel")):
        assert retry(ireq, srv, method, route, {"user_id": OWNER} if method == "POST" else None)[0] == 404
    s, out = retry(phrase, srv, EXAMPLE, mid=31)
    assert s == 200 and out["ok"] and "Обмен: отдал 201 230 ₽" in out["text"] and out["card_id"]
    assert exchange_count(path) == 0
    c = db.connect(path, readonly=True)
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    c.close()
    s, out = retry(ireq, srv, "POST", "/internal/voice-text", {"user_id": OWNER, "text": "обменял 100 долларов на 3500 лир", "message_id": 32})
    assert s == 200 and out["ok"] and "Обмен" in out["text"]
    assert exchange_count(path) == 0
    s, out = retry(ireq, srv, "POST", "/internal/phrase", {"user_id": OWNER, "text": "обменял 500 на 20 лир", "message_id": 33})
    assert s == 200 and out["ok"] is False and out["reject"] == "unsure_exchange" and out["reply"]


def test_api_draft_flow_confirms_only_with_signature_and_the_right_endpoint(srv):
    pub, internal, path = srv
    _, out = phrase(srv, EXAMPLE, mid=41)
    cid = out["card_id"]
    s, d = pub_get(srv, "api/draft?id=%d" % cid)
    assert s == 200 and d["kind"] == "exchange" and d["give"]["currency"] == "RUB"
    data = api_body(draft_id=cid)
    assert req(pub, "POST", "/%s/api/exchanges" % TOKEN, body=data, headers={"Authorization": "Bearer " + INTERNAL})[0] == 401
    assert ireq(srv, "POST", "/internal/card-action", {"user_id": OWNER, "action": "ok", "card_id": cid})[1]["edit"] is None
    assert exchange_count(path) == 0
    s, bad = pub_post(srv, "api/records", {"client_id": str(uuid.uuid4()), "direction": "out", "amount": "1", "currency": "TRY",
                                           "date": db.today_local(), "method": "cash", "draft_id": cid})
    assert s == 409 and bad["error"] == "draft_is_exchange"
    s, ok = pub_post(srv, "api/exchanges", data)
    assert s == 200 and ok["ok"] and exchange_count(path) == 1
    s, rep = pub_post(srv, "api/exchanges", data)
    assert s == 200 and rep["duplicate_request"] is True and exchange_count(path) == 1
    _, rec_card = phrase(srv, "потратил 20 лир на обед", mid=42)
    s, bad = pub_post(srv, "api/exchanges", api_body(draft_id=rec_card["card_id"]))
    assert s == 409 and bad["error"] == "draft_not_exchange"
    s, bad = pub_post(srv, "api/exchanges", api_body(draft_id="1"))
    assert s == 422 and bad["error"] == "bad_draft"


def test_api_list_cancel_restore_update_and_closed_month(srv):
    pub, internal, path = srv
    s, made = pub_post(srv, "api/exchanges", api_body())
    ex = made["id"]
    s, lst = pub_get(srv, "api/exchanges")
    assert s == 200 and lst["count"] == 1 and lst["items"][0]["id"] == ex and lst["items"][0]["fee"]["currency"] == "RUB"
    assert req(pub, "GET", "/%s/api/exchanges" % TOKEN)[0] == 401
    assert pub_get(srv, "api/exchanges?month=2999-01")[0] == 422
    s, r = pub_post(srv, "api/exchanges/%d/cancel" % ex, {})
    assert s == 200 and r["status"] == "cancelled" and r["exchange"]["status"] == "cancelled"
    assert pub_get(srv, "api/exchanges")[1]["count"] == 0
    s, r = pub_post(srv, "api/exchanges/%d/restore" % ex, {})
    assert s == 200 and r["exchange"]["status"] == "active" and r["exchange"]["fee"]["status"] == "active"
    s, r = req(pub, "PATCH", "/%s/api/exchanges/%d" % (TOKEN, ex), body={"note": "через PATCH"}, init=make_init_data(PRIV))
    assert s == 200 and r["changed"] == ["note"] and r["exchange"]["note"] == "через PATCH"
    assert req(pub, "PATCH", "/%s/api/exchanges/%d" % (TOKEN, ex), body={"note": "x"})[0] == 401
    assert req(pub, "PATCH", "/%s/api/exchanges/%d" % (TOKEN, ex), body={"note": "x"},
               headers={"Authorization": "Bearer " + INTERNAL})[0] == 401
    assert pub_post(srv, "api/exchanges/999/cancel", {})[0] == 404
    fee = r["exchange"]["fee"]["tx_id"]
    s, bad = pub_post(srv, "api/records/%d/cancel" % fee, {})
    assert s == 409 and bad["error"] == "linked_to_exchange"
    c = db.connect(path)
    close_month(c, db.today_local()[:7])
    c.close()
    s, bad = pub_post(srv, "api/exchanges/%d/cancel" % ex, {})
    assert s == 422 and bad["error"] == "month_closed"
    s, bad = pub_post(srv, "api/exchanges", api_body())
    assert s == 422 and bad["error"] == "month_closed"


def test_server_log_has_no_amounts(srv, capsys):
    pub_post(srv, "api/exchanges", api_body())
    phrase(srv, EXAMPLE, mid=51)
    logged = capsys.readouterr().err
    assert "exchange ok" in logged
    for n in ("201230", "201 230", "75000", "758"):
        assert n not in logged


# --- «купил / продал»: обмен только когда покупают саму валюту ---------------------------------------------------------
PURCHASES_TWO_CURRENCIES = [
    "купил кофе на 3 доллара за 120 лир",
    "купил такси за 150 лир, это 4 доллара",
    "купил билет на концерт за 1200 лир, это 30 долларов",
    "заплатил 120 лир, это 3 доллара",
    "купил на 50 долларов продуктов за 1600 лир",
    "купила продукты на 20 евро за 800 лир",
    "купил подарок на 100 долларов за 3500 лир",
    "купил себе кофе на 3 евро за 120 лир",
    "купил сим-карту за 200 лир, 5 долларов",
    "купил проездной за 400 лир на 10 евро",
    "продал телефон за 5000 рублей и 20 долларов",
]
PURCHASES_ONE_CURRENCY = [
    ("купил билет за 120 лир", "120", "TRY"), ("купил кофе за 120 лир", "120", "TRY"),
    ("купил обед за 300 лир", "300", "TRY"), ("купил книгу за 15 долларов", "15", "USD"),
    ("купил 2 билета за 500 лир", "500", "TRY"), ("продал велосипед за 200 долларов", "200", "USD"),
    ("продал телефон за 5000 рублей", "5000", "RUB"),
]


@pytest.mark.parametrize("text", PURCHASES_TWO_CURRENCIES)
def test_purchase_with_two_currencies_is_never_an_exchange_card(conn, text):
    assert exchangetext.parse(text, TODAY) is None
    r = cards.create_from_text(conn, OWNER, OWNER, "phrase", text, "p-" + text[:12], now=NOW)
    assert r.get("reject") != "unsure_exchange"                          # обычный разбор, а не карточка обмена
    assert one(conn, "SELECT COUNT(*) FROM exchanges")[0] == 0
    for (draft,) in conn.execute("SELECT draft FROM cards"):
        assert "kind" not in json.loads(draft)


@pytest.mark.parametrize("text,amount,currency", PURCHASES_ONE_CURRENCY)
def test_ordinary_purchase_goes_the_expense_way(conn, text, amount, currency):
    assert exchangetext.parse(text, TODAY) is None
    r = cards.create_from_text(conn, OWNER, OWNER, "phrase", text, "q-" + text[:12], now=NOW)
    assert r["ok"] is True
    d = json.loads(one(conn, "SELECT draft FROM cards WHERE id=?", (r["card_id"],))[0])
    assert "kind" not in d and d["direction"] == "out" and (d["amount"], d["currency"]) == (amount, currency)


@pytest.mark.parametrize("text,give,get", [
    ("купил 700 долларов за 30000 рублей", ("30000", "RUB"), [("700", "USD")]),
    ("купил себе 300 долларов за 12000 лир", ("12000", "TRY"), [("300", "USD")]),
    ("купила 100 евро за 4500 лир", ("4500", "TRY"), [("100", "EUR")]),
    ("купил 700 usdt за 60000 рублей", ("60000", "RUB"), [("700", "USDT")]),
    ("продал 500 долларов за 20 000 лир", ("500", "USD"), [("20000", "TRY")]),
    ("продал 1000 евро за 40000 лир наличными", ("1000", "EUR"), [("40000", "TRY")]),
])
def test_buying_or_selling_a_currency_is_still_an_exchange(text, give, get):
    r = exchangetext.parse(text, TODAY)
    assert r is not None and (r["give"]["amount"], r["give"]["currency"]) == give
    assert [(g["amount"], g["currency"]) for g in r["get"]] == get


def test_buying_a_currency_without_amount_is_refused_not_a_spend():
    with pytest.raises(exchangetext.ExchangeReject):
        parse("купил доллары за 5000 рублей")


# --- доли, округлённые до нуля центов -----------------------------------------------------------------------------------
def test_tiny_leg_still_gets_one_cent_and_shares_add_up(conn):
    payload = body(give={"amount": "0.03", "currency": "RUB", "method": "tinkoff"}, fee=None,
                   get=[{"amount": "1000000", "currency": "TRY"}, {"amount": "1", "currency": "USD"}])
    a, b = legs(conn, make(conn, payload)["id"])
    assert a["give_part_cents"] + b["give_part_cents"] == 3
    assert a["give_part_cents"] > 0 and b["give_part_cents"] > 0 and a["part_estimated"] == b["part_estimated"] == 1


def test_not_enough_cents_for_every_leg_leaves_shares_empty_but_saves(conn):
    payload = body(give={"amount": "0.01", "currency": "RUB", "method": "tinkoff"}, fee=None,
                   get=[{"amount": "1000", "currency": "TRY"}, {"amount": "10", "currency": "USD"}])
    a, b = legs(conn, make(conn, payload)["id"])
    assert a["give_part_cents"] is None and b["give_part_cents"] is None and one(conn, "SELECT COUNT(*) FROM exchanges")[0] == 1


def test_shares_always_add_up_to_the_rest(conn):
    for give in ("1", "17,31", "201230", "0,07", "999999,99"):
        payload = body(give={"amount": give, "currency": "RUB", "method": "tinkoff"}, fee=None, client_id=str(uuid.uuid4()),
                       get=[{"amount": "75000", "currency": "TRY"}, {"amount": "700", "currency": "USD"}, {"amount": "3", "currency": "EUR"}])
        ex = make(conn, payload)["id"]
        total = sum(l["give_part_cents"] for l in legs(conn, ex))
        assert total == one(conn, "SELECT give_cents FROM exchanges WHERE id=?", (ex,))[0], give
        assert all(l["give_part_cents"] > 0 for l in legs(conn, ex))


# --- перенос обмена: привязка уже записанной комиссии ----------------------------------------------------------------------
def old_fee(conn, **kw):
    row = {"idem_key": "old-fee-774", "direction": "out", "amount_cents": 75865, "currency": "RUB", "usd_cents": 948,
           "category": "прочее", "note": "комиссия", "method": "tinkoff", "occurred_on": TODAY,
           "created_at": "2026-10-14T09:00:00Z", "status": "active", "source": "kassa", "via": "webapp", "actor_id": "1001"}
    row.update(kw)
    cols = ", ".join(row)
    cur = conn.execute("INSERT INTO transactions(%s) VALUES (%s)" % (cols, ", ".join("?" * len(row))), list(row.values()))
    return cur.lastrowid


def link(conn, fee_id, payload=None, key="splinter:ex-1"):
    payload = payload or body()
    payload.pop("client_id", None)
    return exchanges.create_exchange(conn, "splinter", payload, via="splinter", now=NOW, fx_rates=RATES, idem_key=key,
                                     existing_fee_id=fee_id)


def test_existing_fee_is_linked_atomically_and_no_second_fee_is_created(conn):
    fee_id = old_fee(conn)
    before = dict(one(conn, "SELECT * FROM transactions WHERE id=?", (fee_id,)))
    res = link(conn, fee_id)
    assert res["fee_id"] == fee_id and res["duplicate_request"] is False
    assert one(conn, "SELECT COUNT(*) FROM transactions")[0] == 1                    # новая комиссия не создана
    after = dict(one(conn, "SELECT * FROM transactions WHERE id=?", (fee_id,)))
    assert after["exchange_id"] == res["id"]
    assert {k: v for k, v in after.items() if k != "exchange_id"} == {k: v for k, v in before.items() if k != "exchange_id"}
    assert json.loads(one(conn, "SELECT detail FROM audit WHERE action='create' AND entity='exchange'")[0])["linked_fee"] is True
    assert link(conn, fee_id)["duplicate_request"] is True                           # повтор безвреден
    assert one(conn, "SELECT COUNT(*) FROM exchanges")[0] == 1
    exchanges.cancel_exchange(conn, "splinter", res["id"], now=NOW)                    # дальше комиссия живёт вместе с обменом
    assert one(conn, "SELECT status FROM transactions WHERE id=?", (fee_id,))[0] == "cancelled"


@pytest.mark.parametrize("setup,code", [
    (dict(status="cancelled"), "fee_row_not_linkable"), (dict(direction="in"), "fee_row_not_linkable"),
    (dict(closed_by="2026-10"), "fee_row_not_linkable"), (dict(source="month-close"), "fee_row_not_linkable"),
    (dict(amount_cents=75866), "fee_row_mismatch"), (dict(currency="TRY"), "fee_row_mismatch"),
])
def test_existing_fee_is_refused_when_it_does_not_fit_and_nothing_is_written(conn, setup, code):
    fee_id = old_fee(conn, **setup)
    with pytest.raises(RecordError) as e:
        link(conn, fee_id)
    assert e.value.code == code
    assert one(conn, "SELECT COUNT(*) FROM exchanges")[0] == 0 and one(conn, "SELECT COUNT(*) FROM exchange_legs")[0] == 0
    assert one(conn, "SELECT exchange_id FROM transactions WHERE id=?", (fee_id,))[0] is None


def test_existing_fee_refused_if_missing_already_linked_or_payload_has_no_fee(conn):
    with pytest.raises(RecordError) as e:
        link(conn, 424242)
    assert e.value.code == "fee_row_not_found"
    fee_id = old_fee(conn)
    first = link(conn, fee_id)
    with pytest.raises(RecordError) as e:
        link(conn, fee_id, key="splinter:ex-2")                                     # уже привязана к первому обмену
    assert e.value.code == "fee_row_not_linkable" and one(conn, "SELECT COUNT(*) FROM exchanges")[0] == 1
    assert one(conn, "SELECT exchange_id FROM transactions WHERE id=?", (fee_id,))[0] == first["id"]
    other = old_fee(conn, idem_key="old-fee-2")
    with pytest.raises(RecordError) as e:
        link(conn, other, body(fee=None), key="splinter:ex-3")
    assert e.value.code == "fee_row_mismatch"


def test_link_existing_fee_cli_dry_run_then_apply(path, conn, tmp_path, capsys):
    fee_id = old_fee(conn)
    payload = body()
    payload.pop("client_id")
    f = tmp_path / "payload.json"
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    argv = ["link-existing-fee", "--db", str(path), "--fee-id", str(fee_id), "--payload", str(f), "--idem-key", "splinter:ex-cli"]
    assert exchanges.main(argv) == 0                                                # сухой прогон: база не меняется
    assert "сухой прогон ок" in capsys.readouterr().out
    assert one(conn, "SELECT COUNT(*) FROM exchanges")[0] == 0
    assert one(conn, "SELECT exchange_id FROM transactions WHERE id=?", (fee_id,))[0] is None
    assert exchanges.main(argv + ["--apply"]) == 0
    out = capsys.readouterr().out
    assert "записано: обмен №1, комиссия №%d привязана" % fee_id in out
    for n in ("201230", "75865", "758"):
        assert n not in out
    assert one(conn, "SELECT exchange_id FROM transactions WHERE id=?", (fee_id,))[0] == 1
    assert tup(conn, "SELECT via, idem_key FROM exchanges") == ("splinter", "splinter:ex-cli")
    assert exchanges.main(argv + ["--apply"]) == 0 and "повтор" in capsys.readouterr().out
    assert one(conn, "SELECT COUNT(*) FROM exchanges")[0] == 1
    bad = ["link-existing-fee", "--db", str(path), "--fee-id", "999999", "--payload", str(f), "--idem-key", "splinter:x", "--apply"]
    assert exchanges.main(bad) == 1 and "fee_row_not_found" in capsys.readouterr().out


def test_link_existing_fee_cli_refuses_a_non_test_database(tmp_path):
    with pytest.raises(SystemExit):
        exchanges.main(["link-existing-fee", "--db", str(tmp_path / "kassa.db"), "--fee-id", "1", "--payload", "x",
                        "--idem-key", "k"])


# --- убранная комиссия не воскресает через /api/records -------------------------------------------------------------------
def test_removed_fee_stays_dead_replacement_counts_once(conn):
    ex = make(conn)["id"]
    old = fee_row(conn, ex)["id"]
    exchanges.update_exchange(conn, OWNER, ex, {"fee": None}, now=NOW)
    exchanges.update_exchange(conn, OWNER, ex, {"fee": {"amount": "10"}}, now=NOW)
    new = fee_row(conn, ex)["id"]
    assert new != old and tup(conn, "SELECT exchange_id, exchange_removed, status FROM transactions WHERE id=?", (old,)) == (ex, 1, "cancelled")
    audit_before = one(conn, "SELECT COUNT(*) FROM audit")[0]
    for fn in (lambda: records.restore_record(conn, OWNER, old), lambda: records.cancel_record(conn, OWNER, old),
               lambda: records.update_record(conn, OWNER, old, {"amount": "99"})):
        with pytest.raises(RecordError) as e:
            fn()
        assert e.value.code == "linked_to_exchange" and e.value.status == 409
    assert one(conn, "SELECT COUNT(*) FROM audit")[0] == audit_before              # отказ ничего не пишет, сумм в audit нет
    assert one(conn, "SELECT status FROM transactions WHERE id=?", (old,))[0] == "cancelled"
    rep = reports.report(conn, "month", "2026-10", TODAY)
    assert [(c["currency"], c["out_cents"]) for c in rep["currencies"]] == [("RUB", 1000)] and rep["count"] == 1
    assert one(conn, "SELECT COUNT(*) FROM v_legacy")[0] == 1
    exchanges.cancel_exchange(conn, OWNER, ex, now=NOW)                               # отмена и возврат двигают только текущую
    exchanges.restore_exchange(conn, OWNER, ex, now=NOW)
    assert one(conn, "SELECT status FROM transactions WHERE id=?", (old,))[0] == "cancelled"
    assert one(conn, "SELECT status FROM transactions WHERE id=?", (new,))[0] == "active"
    assert one(conn, "SELECT COUNT(*) FROM v_legacy")[0] == 1
    assert exchanges.get_item(conn, ex)["fee"]["tx_id"] == new
    assert one(conn, "SELECT COUNT(*) FROM audit WHERE entity='transaction'")[0] == 0   # по комиссиям обмена audit транзакций пуст


def test_removed_fee_without_replacement_is_not_revived_by_exchange_restore(conn):
    ex = make(conn)["id"]
    old = fee_row(conn, ex)["id"]
    exchanges.update_exchange(conn, OWNER, ex, {"fee": None}, now=NOW)
    exchanges.cancel_exchange(conn, OWNER, ex, now=NOW)
    exchanges.restore_exchange(conn, OWNER, ex, now=NOW)
    assert one(conn, "SELECT status FROM transactions WHERE id=?", (old,))[0] == "cancelled"
    assert one(conn, "SELECT COUNT(*) FROM v_legacy")[0] == 0 and exchanges.get_item(conn, ex)["fee"] is None


def test_api_removed_fee_can_not_be_restored_or_edited(srv):
    pub, internal, path = srv
    s, made = pub_post(srv, "api/exchanges", api_body())
    ex, old = made["id"], made["fee_id"]
    req(pub, "PATCH", "/%s/api/exchanges/%d" % (TOKEN, ex), body={"fee": None}, init=make_init_data(PRIV))
    for action, body_ in (("restore", {}), ("cancel", {}), ("update", {"amount": "99"})):
        s, out = pub_post(srv, "api/records/%d/%s" % (old, action), body_)
        assert s == 409 and out["error"] == "linked_to_exchange"
    c = db.connect(path, readonly=True)
    assert c.execute("SELECT status FROM transactions WHERE id=?", (old,)).fetchone()[0] == "cancelled"
    assert c.execute("SELECT COUNT(*) FROM audit WHERE entity='transaction'").fetchone()[0] == 0
    c.close()


# --- журнал не получает произвольную строку действия ---------------------------------------------------------------------------
def test_card_action_log_never_contains_an_arbitrary_action_string(srv, capsys):
    s, out = retry(phrase, srv, "потратил 777 лир на секретное", mid=61)
    cid = out["card_id"]
    for action in ("777 лир секретно", "ok", "x" * 40):
        s, res = retry(ireq, srv, "POST", "/internal/card-action", {"user_id": OWNER, "action": action, "card_id": cid})
        assert s == 200 and "устарела" in res["answer"]
    s, res = retry(ireq, srv, "POST", "/internal/card-action", {"user_id": OWNER, "action": "no", "card_id": cid})
    assert s == 200 and res["answer"] == "Отменено"
    s, res = retry(ireq, srv, "POST", "/internal/card-action", {"user_id": OWNER, "action": "777 лир секретно", "card_id": 99999})
    assert s == 404
    logged = capsys.readouterr().err
    assert "card unknown ok" in logged and "card no ok" in logged
    for leak in ("777", "секретно", "x" * 10):
        assert leak not in logged, leak
