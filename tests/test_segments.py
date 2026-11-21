"""Задание 21.10.2026, этап A: плательщик и сегмент поступления (схема 6). Запись с payer / segment, проверка значений и
направления, правка и audit, отчёт «Месяц» по сегментам, миграция 5 -> 6 (данные целы, повтор безвреден, откат при сбое)."""
import hashlib
import http.client
import json
import threading
from datetime import datetime, timezone

import pytest

from helpers import BOT_ID, OWNER, make_init_data, new_key
from kassa_api import db, records, reports
from kassa_api.server import Config, make_servers

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
TODAY = "2026-10-21"
PRIV, PUB = new_key()
TOKEN = "G" * 8 + "abcdefghijklmnopqrstuvwxyz012345"


@pytest.fixture()
def path(tmp_path):
    p = str(tmp_path / "kassa-test.db")
    db.init_db(p)
    return p


@pytest.fixture()
def conn(path):
    c = db.connect(path)
    yield c
    c.close()


_n = [0]


def add(conn, direction="in", amount="100", currency="USDT", method="bybit", **kw):
    _n[0] += 1
    body = {"direction": direction, "amount": amount, "currency": currency, "date": TODAY, "method": method,
            "client_id": "seg-%016d" % _n[0], "confirm": ["possible_duplicate", "outlier"]}
    if direction == "out" and method == "bybit":
        body["method"] = "cash"
    body.update(kw)
    return records.create_record(conn, OWNER, body, now=NOW, fx_rates={})


def row(conn, rec_id):
    return dict(conn.execute("SELECT * FROM transactions WHERE id=?", (rec_id,)).fetchone())


def audit(conn, rec_id):
    return [dict(r) for r in conn.execute("SELECT * FROM audit WHERE entity='transaction' AND entity_id=? ORDER BY id",
                                          (str(rec_id),))]


# --- запись ----------------------------------------------------------------------------------------------------------
def test_income_with_payer_and_segment_is_stored(conn):
    r = row(conn, add(conn, payer="  Клиент   Один ", segment="b2b")["id"])
    assert (r["payer"], r["segment"]) == ("Клиент Один", "b2b")


def test_income_without_segment_is_unmarked_by_default(conn):
    r = row(conn, add(conn)["id"])
    assert (r["payer"], r["segment"]) == (None, None)
    r = row(conn, add(conn, segment="", payer="")["id"])
    assert (r["payer"], r["segment"]) == (None, None)


@pytest.mark.parametrize("bad", ["B2B", "b2x", "c2c", 1, True, ["b2b"], " b2b"])
def test_bad_segment_values_are_refused(conn, bad):
    with pytest.raises(records.RecordError) as e:
        add(conn, segment=bad)
    assert (e.value.status, e.value.code) == (422, "bad_segment")
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_expense_cannot_have_segment_or_payer(conn):
    with pytest.raises(records.RecordError) as e:
        add(conn, direction="out", segment="b2b")
    assert e.value.code == "bad_segment"
    with pytest.raises(records.RecordError) as e:
        add(conn, direction="out", payer="Кто-то")
    assert e.value.code == "bad_payer"
    r = row(conn, add(conn, direction="out", segment=None, payer=None)["id"])          # пустые значения - не ошибка
    assert (r["payer"], r["segment"]) == (None, None)


@pytest.mark.parametrize("bad", ["x" * 31, "а\u0000б", 5])
def test_bad_payer_text_is_refused(conn, bad):
    with pytest.raises(records.RecordError) as e:
        add(conn, payer=bad)
    assert e.value.code == "bad_payer"


def test_database_itself_refuses_segment_on_expense_or_wrong_value(conn):
    base = ("INSERT INTO transactions(idem_key, direction, amount_cents, currency, occurred_on, created_at, source, via, "
            "actor_id, segment, method) VALUES (?,?,100,'USD','2026-10-01','2026-10-01T00:00:00Z','kassa','webapp','1',?,'cash')")
    with pytest.raises(Exception, match="CHECK"):
        conn.execute(base, ("k1", "out", "b2b"))
    with pytest.raises(Exception, match="CHECK"):
        conn.execute(base, ("k2", "in", "b2z"))
    conn.execute(base, ("k3", "in", "b2c"))


def test_same_form_retry_is_idempotent_but_different_segment_conflicts(conn):
    a = add(conn, client_id="retry-0000000000001", segment="b2b", payer="Х")
    b = add(conn, client_id="retry-0000000000001", segment="b2b", payer="Х")
    assert b["duplicate_request"] and b["id"] == a["id"]
    with pytest.raises(records.RecordError) as e:
        add(conn, client_id="retry-0000000000001", segment="b2c", payer="Х")
    assert e.value.code == "idem_conflict"


def test_create_audit_has_segment_but_not_payer_name(conn):
    rid = add(conn, payer="Секретный Клиент", segment="b2c")["id"]
    detail = audit(conn, rid)[0]["detail"]
    assert json.loads(detail)["segment"] == "b2c" and json.loads(detail)["payer"] is True
    assert "Секретный" not in detail


def test_old_style_create_audit_is_unchanged(conn):
    detail = json.loads(audit(conn, add(conn)["id"])[0]["detail"])
    assert set(detail) == {"new_method", "backdated", "category_auto"}


# --- правка ----------------------------------------------------------------------------------------------------------
def update(conn, rec_id, **body):
    return records.update_record(conn, OWNER, rec_id, body, now=NOW, fx_rates={})


def test_update_sets_and_clears_segment_with_audit(conn):
    rid = add(conn)["id"]
    res = update(conn, rid, segment="b2c")
    assert res["changed"] == ["segment"] and row(conn, rid)["segment"] == "b2c"
    ch = json.loads(audit(conn, rid)[-1]["detail"])["changed"]
    assert ch == {"segment": [None, "b2c"]}
    update(conn, rid, segment="b2b")
    assert json.loads(audit(conn, rid)[-1]["detail"])["changed"] == {"segment": ["b2c", "b2b"]}
    update(conn, rid, segment=None)
    assert row(conn, rid)["segment"] is None
    assert json.loads(audit(conn, rid)[-1]["detail"])["changed"] == {"segment": ["b2b", None]}


def test_update_same_segment_changes_nothing_and_writes_no_audit(conn):
    rid = add(conn, segment="b2b")["id"]
    before = len(audit(conn, rid))
    assert update(conn, rid, segment="b2b")["changed"] == []
    assert len(audit(conn, rid)) == before


def test_update_payer_and_segment_together(conn):
    rid = add(conn)["id"]
    res = update(conn, rid, payer="Новый", segment="b2b")
    assert res["changed"] == ["payer", "segment"]
    assert (row(conn, rid)["payer"], row(conn, rid)["segment"]) == ("Новый", "b2b")
    update(conn, rid, payer="")
    assert row(conn, rid)["payer"] is None


@pytest.mark.parametrize("bad", ["B2B", "zzz", 7])
def test_update_bad_segment_is_refused_and_nothing_changes(conn, bad):
    rid = add(conn, segment="b2b")["id"]
    with pytest.raises(records.RecordError) as e:
        update(conn, rid, segment=bad)
    assert e.value.code == "bad_segment" and row(conn, rid)["segment"] == "b2b"


def test_update_expense_segment_refused_but_null_is_fine(conn):
    rid = add(conn, direction="out")["id"]
    with pytest.raises(records.RecordError) as e:
        update(conn, rid, segment="b2b")
    assert e.value.code == "bad_segment"
    assert update(conn, rid, segment=None)["changed"] == []


def test_update_cancelled_or_closed_record_is_refused(conn):
    rid = add(conn)["id"]
    records.cancel_record(conn, OWNER, rid, now=NOW)
    with pytest.raises(records.RecordError) as e:
        update(conn, rid, segment="b2b")
    assert e.value.code == "record_cancelled"
    rid2 = add(conn)["id"]
    conn.execute("UPDATE transactions SET closed_by='2026-10' WHERE id=?", (rid2,))
    with pytest.raises(records.RecordError) as e:
        update(conn, rid2, segment="b2b")
    assert e.value.code == "record_closed"


def test_update_keeps_working_for_old_fields(conn):
    rid = add(conn, amount="50")["id"]
    assert update(conn, rid, amount="75", segment="b2b")["changed"] == ["amount", "segment"]
    assert row(conn, rid)["amount_cents"] == 7500


# --- отчёты ----------------------------------------------------------------------------------------------------------
def test_month_report_groups_income_by_segment_with_unmarked(conn):
    add(conn, amount="300", segment="b2b")
    add(conn, amount="100", segment="b2b")
    add(conn, amount="50", segment="b2c")
    add(conn, amount="20")
    add(conn, direction="out", amount="999")
    rep = reports.report(conn, "month", "2026-10", TODAY)
    seg = {g["name"]: (g["count"], g["usd_cents"]) for g in rep["by_segment"]}
    assert seg == {"B2B": (2, 40000), "B2C": (1, 5000), "Без пометки": (1, 2000)}
    assert [g["name"] for g in rep["by_segment"]] == ["B2B", "B2C", "Без пометки"]       # по убыванию суммы
    assert sum(g["usd_cents"] for g in rep["by_segment"]) == rep["usd"]["in_cents"]      # сумма сегментов = итог поступлений


def test_month_report_without_income_has_empty_segments(conn):
    add(conn, direction="out")
    assert reports.report(conn, "month", "2026-10", TODAY)["by_segment"] == []


def test_cancelled_income_is_not_in_segments(conn):
    rid = add(conn, amount="100", segment="b2b")["id"]
    records.cancel_record(conn, OWNER, rid, now=NOW)
    assert reports.report(conn, "month", "2026-10", TODAY)["by_segment"] == []


def test_day_report_and_recent_items_carry_payer_and_segment(conn):
    rid = add(conn, payer="Кто", segment="b2c")["id"]
    day = reports.report(conn, "day", None, TODAY)
    assert [(r["payer"], r["segment"]) for r in day["records"]] == [("Кто", "b2c")]
    assert "by_segment" not in day
    assert reports.recent(conn)["records"][0]["segment"] == "b2c"
    assert reports.get_item(conn, rid)["payer"] == "Кто"


def test_trend_and_totals_do_not_change_with_segments(conn):
    add(conn, amount="100", segment="b2b")
    add(conn, amount="100")
    trend = {m["month"]: m for m in reports.trend(conn, TODAY)["months"]}
    assert trend["2026-10"]["in_usd_cents"] == 20000


# --- миграция 5 -> 6 -------------------------------------------------------------------------------------------------
def make_v5(path):
    c = db.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("BEGIN IMMEDIATE")
    db._create_v1(c, True)
    db._migrate_v2(c)
    db._migrate_v3(c)
    db._migrate_v4(c)
    db._migrate_v5(c)
    c.execute("PRAGMA user_version=5")
    c.execute("COMMIT")
    for i, (d, cur) in enumerate((("in", "USDT"), ("out", "TRY"), ("in", "RUB")), 1):
        c.execute("INSERT INTO transactions(idem_key, direction, amount_cents, currency, usd_cents, category, note, method, "
                  "occurred_on, created_at, source, via, actor_id) VALUES (?,?,?,?,?,?,?,?, '2026-10-0%d', "
                  "'2026-10-0%dT00:00:00Z','kassa','webapp','1')" % (i, i),
                  ("old-%d" % i, d, 1000 * i, cur, 500 * i, "доход" if d == "in" else "кафе", "заметка %d" % i,
                   "bybit" if d == "in" else "cash"))
    c.execute("INSERT INTO bybit_seen(tx_id, first_seen, amount, coin, kind, status) VALUES ('t','x','1','USDT','internal','2')")
    c.close()


def snapshot(path):
    c = db.connect(path, readonly=True)
    try:
        rows = [tuple(r) for r in c.execute("SELECT * FROM transactions ORDER BY id")]
        return hashlib.sha256(repr(rows).encode()).hexdigest(), c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()










def test_newer_database_is_refused(tmp_path):
    p = str(tmp_path / "future-test.db")
    db.init_db(p)
    c = db.connect(p)
    c.execute("PRAGMA user_version=8")
    c.close()
    with pytest.raises(SystemExit):
        db.init_db(p)




# --- HTTP: правка сегмента подписанным запросом ---------------------------------------------------------------------
@pytest.fixture()
def server(tmp_path):
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), "I" * 40, PUB, str(tmp_path / "kassa-test.db"))
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0))
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield pub.server_address[1], str(tmp_path / "kassa-test.db")
    for s in (pub, internal):
        s.shutdown()
        s.server_close()


def call(port, method, rest, body=None, init=True):
    h = {}
    if init:
        h["Authorization"] = "tma " + make_init_data(PRIV)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, "/%s/%s" % (TOKEN, rest), body=data, headers=h)
    r = c.getresponse()
    out = json.loads(r.read())
    c.close()
    return r.status, out


def test_http_create_with_segment_edit_it_and_see_it_in_month_report(server):
    port, _ = server
    today = db.today_local()
    body = {"client_id": "http-seg-00000000001", "direction": "in", "amount": "200", "currency": "USD", "date": today,
            "method": "cash", "segment": "b2b"}
    s, out = call(port, "POST", "api/records", body)
    assert s == 200 and out["ok"]
    rid = out["id"]
    s, rep = call(port, "GET", "api/report?period=month")
    assert [(g["name"], g["usd_cents"]) for g in rep["by_segment"]] == [("B2B", 20000)]
    s, out = call(port, "POST", "api/records/%d/update" % rid, {"segment": "b2c"})
    assert s == 200 and out["changed"] == ["segment"] and out["record"]["segment"] == "b2c"
    s, rep = call(port, "GET", "api/report?period=month")
    assert [(g["name"], g["usd_cents"]) for g in rep["by_segment"]] == [("B2C", 20000)]
    s, out = call(port, "POST", "api/records/%d/update" % rid, {"segment": "zzz"})
    assert (s, out["error"]) == (422, "bad_segment")
    assert call(port, "POST", "api/records/%d/update" % rid, {"segment": "b2b"}, init=False)[0] == 401
    s, out = call(port, "POST", "api/records", dict(body, client_id="http-seg-00000000002", direction="out",
                                                    method="cash"))
    assert (s, out["error"]) == (422, "bad_segment")
