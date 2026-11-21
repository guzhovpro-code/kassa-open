"""Этап 5: курсы (usd_cents при записи и правке, кэш, фоновый дорасчёт) и ночной бэкап базы.
Сеть не используется: загрузчик курсов подменяется."""
import http.client
import importlib.util
import json
import os
import sqlite3
import stat
import threading
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helpers import BOT_ID, OWNER, make_init_data, new_key
from kassa_api import db, rates, records, reports
from kassa_api.server import Config, make_servers

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
TODAY = "2026-10-14"
ACTOR = 1001
RATES = {"TRY": 40.0, "RUB": 80.0, "EUR": 0.5, "KZT": 500.0}

ECB = {"amount": 1.0, "base": "USD", "date": "2026-10-14", "rates": {"TRY": 40.0, "EUR": 0.9, "JPY": 150.0}}
CBR = {"Valute": {
    "USD": {"Nominal": 1, "Value": 80.0},
    "EUR": {"Nominal": 1, "Value": 88.0},            # у ECB EUR есть: приоритет у ECB
    "KZT": {"Nominal": 100, "Value": 16.0},          # 1 KZT = 0,16 RUB -> 500 KZT за доллар
    "GEL": {"Nominal": 1, "Value": 40.0},            # 2 GEL за доллар
    "BAD": {"Nominal": 0, "Value": 5.0},
    "xx": {"Nominal": 1, "Value": 5.0},
}}


class Net:
    """Подмена сети: отдаёт заготовленные ответы по адресу, умеет «отключаться»."""

    def __init__(self, ecb=ECB, cbr=CBR):
        self.answers = {rates.ECB_URL: ecb, rates.CBR_URL: cbr}
        self.up = True
        self.calls = []

    def __call__(self, url, timeout=None):
        self.calls.append(url)
        if not self.up:
            raise urllib.error.URLError("offline")
        return self.answers[url]


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


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
    base = {"client_id": str(uuid.uuid4()), "direction": "out", "amount": "120", "currency": "USD", "date": TODAY, "method": "cash"}
    base.update(kw)
    return base


def create(conn, fx_rates=None, **kw):
    return records.create_record(conn, ACTOR, body(**kw), now=NOW, fx_rates=fx_rates)


def row(conn, rid):
    return dict(conn.execute("SELECT * FROM transactions WHERE id=?", (rid,)).fetchone())


def seed(conn, cents=10000, cur="TRY", usd=None, source="kassa", status="active", direction="out"):
    cur_ = conn.execute(
        "INSERT INTO transactions(idem_key, direction, amount_cents, currency, usd_cents, category, occurred_on, "
        "created_at, status, source, via, actor_id) VALUES (?,?,?,?,?,'кафе',?,?,?,?,?,?)",
        ("s-" + uuid.uuid4().hex, direction, cents, cur, usd, TODAY, TODAY + "T09:00:00Z", status, source,
         "finbot-import" if source != "kassa" else "webapp", None if source != "kassa" else "1"))
    return cur_.lastrowid


# --- пересчёт суммы ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("cur,cents,expected", [
    ("USD", 12000, 12000), ("USDT", 12000, 12000), ("USDC", 12000, 12000),     # 1:1 без запроса, курсов не нужно
    ("TRY", 4000, 100), ("TRY", 12345, 309),        # 308,625 -> половина вверх
    ("TRY", 20, 1), ("TRY", 19, 0), ("TRY", 1, 0),  # 0,5 -> 1; 0,475 -> 0
    ("TRY", -4000, -100), ("TRY", -20, -1),         # по модулю
    ("RUB", 8000, 100), ("EUR", 1000, 2000), ("KZT", 50000, 100),
    ("XYZ", 100, None), ("BTC", 100, None), ("SCR", 100, None),
])
def test_convert(cur, cents, expected):
    assert rates.convert(cur, cents, RATES) == expected


@pytest.mark.parametrize("bad", [0, -1, -40.0, float("nan"), float("inf"), "40", None, True, [40]])
def test_convert_ignores_broken_rate(bad):
    assert rates.convert("TRY", 4000, {"TRY": bad}) is None


def test_convert_without_rates_still_does_parity():
    assert rates.convert("USDT", 5, None) == 5 and rates.convert("USDC", 5, {}) == 5
    assert rates.convert("TRY", 5, None) is None and rates.convert("TRY", 5, {}) is None


# --- разбор ответов ----------------------------------------------------------------------------------------------
def test_parse_ecb_keeps_only_good_values():
    data = {"rates": {"TRY": 40, "EUR": 0.9, "USD": 1, "BAD": 0, "XXX": -1, "ZZZ": "5", "try": 3, "TOOLONG": 4,
                      "NAN": float("nan"), "B": 5}}
    assert rates.parse_ecb(data) == {"TRY": 40.0, "EUR": 0.9}
    assert rates.parse_ecb([]) == {} and rates.parse_ecb({"rates": []}) == {} and rates.parse_ecb(None) == {}


def test_parse_cbr_gives_rub_and_cross_rates_with_nominal():
    out = rates.parse_cbr(CBR)
    assert out["RUB"] == 80.0
    assert out["KZT"] == pytest.approx(500.0) and out["GEL"] == pytest.approx(2.0)
    assert out["EUR"] == pytest.approx(80.0 / 88.0)
    assert "BAD" not in out and "xx" not in out and "USD" not in out
    assert rates.parse_cbr({}) == {} and rates.parse_cbr({"Valute": {"EUR": {"Nominal": 1, "Value": 1}}}) == {}


# --- кэш курсов ----------------------------------------------------------------------------------------------------
def make_store(tmp_path, net=None, clock=None, logs=None):
    return rates.RateStore(tmp_path / "rates-cache.json", fetch=net or Net(), clock=clock or Clock(),
                           log=(logs.append if logs is not None else None))


def test_refresh_fills_cache_and_ecb_wins_over_cbr(tmp_path):
    store = make_store(tmp_path)
    assert store.needs_refresh() and store.usable() == {}
    assert store.refresh_if_stale() == {"ecb": True, "cbr": True}
    r = store.usable()
    assert r["TRY"] == 40.0 and r["EUR"] == 0.9 and r["JPY"] == 150.0           # ECB, где он есть
    assert r["RUB"] == 80.0 and r["KZT"] == pytest.approx(500.0) and r["GEL"] == pytest.approx(2.0)   # ЦБ РФ
    assert not store.needs_refresh()


@pytest.mark.skipif(os.name == "nt", reason="права файла проверяются на сервере")
def test_cache_file_is_private(tmp_path):
    make_store(tmp_path).refresh_if_stale()
    assert stat.S_IMODE((tmp_path / "rates-cache.json").stat().st_mode) == 0o600


def test_cache_survives_restart(tmp_path):
    make_store(tmp_path).refresh_if_stale()
    again = rates.RateStore(tmp_path / "rates-cache.json", fetch=Net(), clock=Clock())
    assert again.usable()["TRY"] == 40.0 and not again.needs_refresh()


def test_fresh_cache_does_not_touch_the_network(tmp_path):
    net, clock = Net(), Clock()
    store = make_store(tmp_path, net, clock)
    store.refresh_if_stale()
    n = len(net.calls)
    clock.t = NOW + timedelta(hours=11)
    assert store.refresh_if_stale() == {"ecb": None, "cbr": None} and len(net.calls) == n
    clock.t = NOW + timedelta(hours=13)
    assert store.needs_refresh() and store.refresh_if_stale() == {"ecb": True, "cbr": True}


def test_no_network_keeps_old_cache_and_does_not_raise(tmp_path):
    net, clock, logs = Net(), Clock(), []
    store = make_store(tmp_path, net, clock, logs)
    store.refresh_if_stale()
    net.up = False
    clock.t = NOW + timedelta(hours=13)
    assert store.refresh_if_stale() == {"ecb": False, "cbr": False}
    assert store.usable()["TRY"] == 40.0                    # просроченный, но не старше 3 суток: ещё годится
    assert any("URLError" in m for m in logs) and not any("40" in m for m in logs)


def test_cache_older_than_three_days_is_not_used(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock=clock)
    store.refresh_if_stale()
    clock.t = NOW + timedelta(days=3, minutes=1)
    assert store.usable() == {}
    clock.t = NOW + timedelta(days=2, hours=23)
    assert store.usable()["TRY"] == 40.0


def test_ecb_down_cbr_up_gives_what_cbr_has(tmp_path):
    class HalfNet(Net):
        def __call__(self, url, timeout=None):
            if url == rates.ECB_URL:
                raise urllib.error.URLError("ecb offline")
            return super().__call__(url)

    store = make_store(tmp_path, HalfNet())
    assert store.refresh_if_stale() == {"ecb": False, "cbr": True}
    r = store.usable()
    assert "TRY" not in r and r["RUB"] == 80.0 and r["KZT"] == pytest.approx(500.0)
    assert store.needs_refresh()                              # ECB ещё не получен: на следующем такте попытка снова


def test_garbage_answers_and_garbage_cache_are_harmless(tmp_path):
    (tmp_path / "rates-cache.json").write_text("{не json", encoding="utf-8")
    store = make_store(tmp_path, Net(ecb=["мусор"], cbr={"Valute": 5}))
    assert store.usable() == {}
    assert store.refresh_if_stale() == {"ecb": False, "cbr": False}
    assert store.usable() == {}
    (tmp_path / "rates-cache.json").write_text('{"ecb": {"at": "вчера", "rates": {"TRY": 40}}}', encoding="utf-8")
    assert make_store(tmp_path).usable() == {}               # время не разобрать - кэш не используется


def test_usable_does_not_wait_for_network(tmp_path):
    """Сеть вне замка: пока идёт долгий запрос, чтение курсов на пути записи не блокируется."""
    gate, started = threading.Event(), threading.Event()

    def slow(url, timeout=None):
        started.set()
        gate.wait(5)
        return ECB if url == rates.ECB_URL else CBR

    store = make_store(tmp_path, slow)
    t = threading.Thread(target=store.refresh_if_stale)
    t.start()
    assert started.wait(5)
    done = []
    reader = threading.Thread(target=lambda: done.append(store.usable()))
    reader.start()
    reader.join(2)
    assert done == [{}]
    gate.set()
    t.join(5)
    assert store.usable()["TRY"] == 40.0


# --- запись и правка ---------------------------------------------------------------------------------------------
def test_new_usd_record_is_one_to_one_even_without_rates(conn):
    res = create(conn)                                                # 120 USD, курсов нет совсем
    assert row(conn, res["id"])["usd_cents"] == 12000


@pytest.mark.parametrize("cur", ["USDT", "USDC", "usdc"])
def test_stable_coins_are_one_to_one(conn, cur):
    assert row(conn, create(conn, currency=cur)["id"])["usd_cents"] == 12000


def test_try_record_gets_usd_by_cached_rate(conn):
    res = create(conn, RATES, amount="400", currency="TRY")
    assert row(conn, res["id"])["usd_cents"] == 1000


def test_income_gets_usd_too(conn):
    res = create(conn, RATES, direction="in", amount="800", currency="RUB", method="bybit", category="доход")
    assert row(conn, res["id"])["usd_cents"] == 1000


def test_no_rate_record_is_saved_with_null(conn):
    res = create(conn, None, amount="400", currency="TRY")
    assert res["ok"] and row(conn, res["id"])["usd_cents"] is None
    res = create(conn, RATES, amount="1", currency="BTC")
    assert res["ok"] and row(conn, res["id"])["usd_cents"] is None


def test_repeated_request_does_not_recompute(conn):
    cid = str(uuid.uuid4())
    first = create(conn, RATES, client_id=cid, amount="400", currency="TRY")
    again = create(conn, {"TRY": 80.0}, client_id=cid, amount="400", currency="TRY")
    assert again["duplicate_request"] and again["id"] == first["id"]
    assert row(conn, first["id"])["usd_cents"] == 1000


def upd(conn, rid, payload, fx_rates=None):
    return records.update_record(conn, ACTOR, rid, payload, now=NOW, fx_rates=fx_rates)


def test_edit_currency_to_usd_gives_one_to_one(conn):
    rid = seed(conn, 10000, "TRY", usd=300)
    upd(conn, rid, {"currency": "USD"})
    assert row(conn, rid)["usd_cents"] == 10000


def test_edit_currency_uses_cached_rate_or_null(conn):
    rid = seed(conn, 10000, "KZT", usd=20)
    upd(conn, rid, {"currency": "TRY"}, RATES)
    assert row(conn, rid)["usd_cents"] == 250
    upd(conn, rid, {"currency": "BTC"}, RATES)
    assert row(conn, rid)["usd_cents"] is None
    d = json.loads(conn.execute("SELECT detail FROM audit WHERE action='update' ORDER BY id DESC").fetchone()[0])
    assert d["changed"]["usd_cents"] == [250, None]


def test_edit_amount_keeps_the_record_rate_not_todays(conn):
    rid = seed(conn, 10000, "TRY", usd=300)               # курс записи 33,33, не сегодняшние 40
    upd(conn, rid, {"amount": "250"}, RATES)
    assert row(conn, rid)["usd_cents"] == 750


def test_edit_amount_without_old_rate_takes_cached(conn):
    rid = seed(conn, 10000, "TRY", usd=None)
    upd(conn, rid, {"amount": "400"}, RATES)
    assert row(conn, rid)["usd_cents"] == 1000
    rid2 = seed(conn, 10000, "TRY", usd=None)
    upd(conn, rid2, {"amount": "400"})                     # курсов нет: остаётся NULL
    assert row(conn, rid2)["usd_cents"] is None


def test_edit_amount_of_usd_record_with_null_snapshot_fixes_it(conn):
    rid = seed(conn, 12000, "USD", usd=None)
    upd(conn, rid, {"amount": "150"})
    assert row(conn, rid)["usd_cents"] == 15000


def test_edit_amount_of_stable_coin_record_is_always_one_to_one(conn):
    rid = seed(conn, 12000, "USDT", usd=11000, source="bot")        # у перенесённой записи снимок мог быть иным
    upd(conn, rid, {"amount": "150"})
    assert row(conn, rid)["usd_cents"] == 15000


def test_edit_negative_imported_amount_gets_fresh_rate(conn):
    rid = seed(conn, -30000, "TRY", usd=-700, source="bot")
    upd(conn, rid, {"amount": "300"}, RATES)
    assert row(conn, rid)["usd_cents"] == 750


# --- дорасчёт ------------------------------------------------------------------------------------------------------
def test_backfill_fills_only_kassa_records_with_a_rate(conn):
    a = seed(conn, 4000, "TRY")                            # курс есть
    b = seed(conn, 12000, "USD")                           # 1:1
    c = seed(conn, 100, "BTC")                             # курса нет
    d = seed(conn, 4000, "TRY", usd=999)                   # уже есть - не трогаем
    e = seed(conn, 4000, "TRY", source="bot")              # перенесённая без курса - не трогаем
    f = seed(conn, 4000, "TRY", status="cancelled")        # удалённая тоже получает курс: вернут - будет с курсом
    assert rates.backfill(conn, RATES, NOW) == 3
    got = {r: row(conn, r)["usd_cents"] for r in (a, b, c, d, e, f)}
    assert got == {a: 100, b: 12000, c: None, d: 999, e: None, f: 100}
    (audit,) = [dict(r) for r in conn.execute("SELECT * FROM audit WHERE action='usd_backfill'")]
    detail = json.loads(audit["detail"])
    assert detail["records"] == 3 and sorted(detail["ids"]) == sorted([a, b, f]) and audit["actor_id"] is None
    assert "usd" not in audit["detail"].replace("usd_backfill", "")          # сумм в аудите нет


def test_backfill_is_idempotent_and_quiet_when_nothing_to_do(conn):
    seed(conn, 4000, "TRY")
    assert rates.backfill(conn, RATES, NOW) == 1
    snap = [tuple(r) for r in conn.execute("SELECT * FROM transactions")]
    audits = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    assert rates.backfill(conn, RATES, NOW) == 0
    assert [tuple(r) for r in conn.execute("SELECT * FROM transactions")] == snap
    assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == audits


def test_backfill_without_rates_leaves_everything_but_parity(conn):
    a = seed(conn, 4000, "TRY")
    b = seed(conn, 4000, "USDT")
    assert rates.backfill(conn, {}, NOW) == 1
    assert row(conn, a)["usd_cents"] is None and row(conn, b)["usd_cents"] == 4000


def test_backfill_picks_up_imported_record_whose_rate_was_reset_by_edit(conn):
    imported = seed(conn, 4000, "TRY", usd=100, source="bot")
    other = seed(conn, 4000, "TRY", source="bot")                       # так и пришла без курса
    upd(conn, imported, {"currency": "BTC"})                            # курс сброшен правкой
    upd(conn, imported, {"currency": "TRY"})                            # курсов тогда не было: NULL
    assert row(conn, imported)["usd_cents"] is None
    assert rates.backfill(conn, RATES, NOW) == 1
    assert row(conn, imported)["usd_cents"] == 100 and row(conn, other)["usd_cents"] is None


def test_backfill_does_not_touch_amounts_or_other_columns(conn):
    rid = seed(conn, 12345, "TRY")
    before = row(conn, rid)
    rates.backfill(conn, RATES, NOW)
    after = row(conn, rid)
    assert {k: v for k, v in after.items() if k != "usd_cents"} == {k: v for k, v in before.items() if k != "usd_cents"}
    assert after["usd_cents"] == 309


def test_backfill_rolls_back_if_audit_fails(conn):
    rid = seed(conn, 4000, "TRY")
    conn.execute("CREATE TRIGGER no_audit BEFORE INSERT ON audit WHEN NEW.action='usd_backfill' "
                 "BEGIN SELECT RAISE(ABORT, 'нельзя'); END")
    with pytest.raises(sqlite3.DatabaseError):
        rates.backfill(conn, RATES, NOW)
    assert row(conn, rid)["usd_cents"] is None and not conn.in_transaction


# --- фоновый работник ------------------------------------------------------------------------------------------------
def test_worker_refreshes_cache_then_fills_missing_rates(tmp_path, path):
    net, logs = Net(), []
    store = make_store(tmp_path, net, logs=logs)
    c = db.connect(path)
    rid = create(c, None, amount="400", currency="TRY")["id"]          # записали без курсов
    assert row(c, rid)["usd_cents"] is None
    worker = rates.RateWorker(store, lambda: db.connect(path), log=logs.append)
    assert worker.tick() == 1
    assert row(c, rid)["usd_cents"] == 1000
    assert worker.tick() == 0                                          # второй такт: ничего не делает
    assert "usd backfill: 1" in logs
    assert not any("400" in m or "1000" in m for m in logs)            # в журнале нет сумм
    c.close()


def test_worker_survives_offline_and_catches_up_later(tmp_path, path):
    net, logs = Net(), []
    net.up = False
    store = make_store(tmp_path, net, logs=logs)
    c = db.connect(path)
    rid = create(c, None, amount="400", currency="TRY")["id"]
    worker = rates.RateWorker(store, lambda: db.connect(path), log=logs.append)
    assert worker.tick() == 0 and row(c, rid)["usd_cents"] is None
    net.up = True
    assert worker.tick() == 1 and row(c, rid)["usd_cents"] == 1000
    c.close()


def test_worker_logs_and_survives_database_trouble(tmp_path):
    logs = []
    store = make_store(tmp_path)

    def broken():
        raise sqlite3.OperationalError("база недоступна")

    worker = rates.RateWorker(store, broken, log=logs.append)
    assert worker.tick() == 0
    assert any("usd backfill error: OperationalError" in m for m in logs)


def test_worker_thread_runs_and_stops(tmp_path, path):
    store = make_store(tmp_path)
    c = db.connect(path)
    rid = create(c, None, amount="400", currency="TRY")["id"]
    worker = rates.RateWorker(store, lambda: db.connect(path), interval=0.05, log=lambda m: None)
    worker.start()
    for _ in range(100):
        if row(c, rid)["usd_cents"] is not None:
            break
        threading.Event().wait(0.05)
    worker.stop()
    worker.join(5)
    assert row(c, rid)["usd_cents"] == 1000 and not worker.is_alive()
    c.close()


# --- по HTTP: «Сегодня» и «≈ USD» --------------------------------------------------------------------------------------
PRIV, PUB = new_key()
TOKEN = "R" * 8 + "abcdefghijklmnopqrstuvwxyz012345"


def start(tmp_path, net):
    db_path = str(tmp_path / "kassa-test.db")
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), "J" * 40, PUB, db_path)
    store = rates.RateStore(tmp_path / "rates-cache.json", fetch=net, log=lambda m: None)
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0), rate_store=store)
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    return pub, internal, store, db_path


@pytest.fixture()
def online(tmp_path):
    net = Net()
    pub, internal, store, db_path = start(tmp_path, net)
    store.refresh_if_stale()
    yield pub.server_address[1], db_path, store, net
    for s in (pub, internal):
        s.shutdown()
        s.server_close()


@pytest.fixture()
def offline(tmp_path):
    net = Net()
    net.up = False
    pub, internal, store, db_path = start(tmp_path, net)
    yield pub.server_address[1], db_path, store, net
    for s in (pub, internal):
        s.shutdown()
        s.server_close()


def call(port, method, rest, payload=None):
    h = {"Authorization": "tma " + make_init_data(PRIV, user_id=OWNER)}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        h.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, "/%s/%s" % (TOKEN, rest), body=data, headers=h)
    r = c.getresponse()
    out = json.loads(r.read())
    c.close()
    return r.status, out


def post_rec(port, **kw):
    today = db.today_local()
    p = {"client_id": str(uuid.uuid4()), "direction": "out", "amount": "120", "currency": "USD", "date": today,
         "note": "кофе", "method": "cash"}
    p.update(kw)
    s, out = call(port, "POST", "api/records", p)
    assert s == 200, out
    return out["id"]


def test_complaint_120_usd_shows_in_today_usd_line(online):
    port, path, _, _ = online
    post_rec(port)
    rep = call(port, "GET", "api/report")[1]
    assert rep["usd"] == {"out_cents": 12000, "in_cents": 0, "net_cents": -12000, "records_without_usd": 0}
    assert [(c["currency"], c["out_cents"]) for c in rep["currencies"]] == [("USD", 12000)]


def test_try_record_over_http_gets_usd_by_rate(online):
    port, path, _, _ = online
    post_rec(port, amount="400", currency="TRY")
    rep = call(port, "GET", "api/report")[1]
    assert rep["usd"]["out_cents"] == 1000 and rep["usd"]["records_without_usd"] == 0


def test_edit_over_http_recomputes_usd(online):
    port, path, _, _ = online
    rid = post_rec(port, amount="400", currency="TRY")
    s, out = call(port, "POST", "api/records/%d/update" % rid, {"currency": "EUR"})        # ECB: 0,9 за доллар
    assert s == 200
    rep = call(port, "GET", "api/report")[1]
    assert rep["usd"]["out_cents"] == 44444                                                 # 400 / 0,9 = 444,44
    s, out = call(port, "POST", "api/records/%d/update" % rid, {"currency": "USD", "amount": "120"})
    assert call(port, "GET", "api/report")[1]["usd"]["out_cents"] == 12000


def test_offline_record_is_saved_and_report_counts_it_without_rate(offline, capfd):
    port, path, store, net = offline
    post_rec(port, amount="400", currency="TRY")                       # сети нет, кэша нет
    rep = call(port, "GET", "api/report")[1]
    assert rep["count"] == 1 and rep["usd"]["records_without_usd"] == 1
    assert rep["usd"]["out_cents"] == 0                                # нули есть, но UI их не показывает (test_ui5)
    assert [(c["currency"], c["out_cents"]) for c in rep["currencies"]] == [("TRY", 40000)]   # суммы по валюте видны
    # сеть вернулась: фоновый такт дописывает, отчёт обновился
    net.up = True
    rates.RateWorker(store, lambda: db.connect(path), log=lambda m: None).tick()
    rep = call(port, "GET", "api/report")[1]
    assert rep["usd"]["records_without_usd"] == 0 and rep["usd"]["out_cents"] == 1000
    err = capfd.readouterr().err
    assert "400" not in err and "кофе" not in err


def test_usd_record_offline_still_counts(offline):
    port, *_ = offline
    post_rec(port, amount="120", currency="USD")
    post_rec(port, amount="400", currency="TRY")
    rep = call(port, "GET", "api/report")[1]
    assert rep["usd"] == {"out_cents": 12000, "in_cents": 0, "net_cents": -12000, "records_without_usd": 1}


def make_source(path, n=5):
    c = db.connect(path)
    for i in range(n):
        seed(c, 1000 + i, "TRY")
    c.close()
