"""Этап 3: отчёты «Сегодня» и «Месяц», «Последние», удаление, возврат, смена категории и способа (Т3.1-Т3.5)."""
import collections
import random
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest


from kassa_api import db, records, reports
from kassa_api.records import RecordError

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)      # локально 15:00 14.10
TODAY = "2026-10-14"
ACTOR = 1001


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


_seq = [0]


def add(conn, direction="out", cents=10000, cur="TRY", day=TODAY, usd=None, category="кафе", method=None,
        status="active", created=None, note=None, via="webapp"):
    _seq[0] += 1
    created = created or "%sT09:%02d:00Z" % (day, _seq[0] % 60)
    cur_ = conn.execute(
        "INSERT INTO transactions(idem_key, direction, amount_cents, currency, usd_cents, category, note, method, "
        "occurred_on, created_at, status, cancelled_at, source, via, actor_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'kassa', ?, '1')",
        ("t%d-%s" % (_seq[0], uuid.uuid4().hex[:6]), direction, cents, cur, usd, category, note, method, day,
         created, status, created if status == "cancelled" else None, via))
    return cur_.lastrowid


def by_cur(rep):
    return {c["currency"]: c for c in rep["currencies"]}


# --- Т3.1: сегодня ----------------------------------------------------------------------------------------------
def test_day_report_totals_per_currency_and_usd(conn):
    add(conn, "out", 10000, "TRY", usd=300)
    add(conn, "out", 2550, "TRY", usd=75)
    add(conn, "out", 1000, "USD", usd=1000)
    add(conn, "in", 50000, "USDT", usd=50000, method="bybit", category="доход")
    add(conn, "in", 7000, "TRY", usd=None, method="cash", category="доход")           # без курса
    rep = reports.report(conn, "day", None, TODAY)
    c = by_cur(rep)
    assert (c["TRY"]["out_cents"], c["TRY"]["in_cents"], c["TRY"]["net_cents"]) == (12550, 7000, 7000 - 12550)
    assert (c["USD"]["out_cents"], c["USD"]["in_cents"], c["USD"]["net_cents"]) == (1000, 0, -1000)
    assert (c["USDT"]["out_cents"], c["USDT"]["in_cents"], c["USDT"]["net_cents"]) == (0, 50000, 50000)
    assert rep["usd"] == {"out_cents": 300 + 75 + 1000, "in_cents": 50000, "net_cents": 50000 - 1375,
                          "records_without_usd": 1}
    assert rep["count"] == 5 and rep["period"] == "day" and rep["from"] == rep["to"] == TODAY


def test_day_report_ignores_cancelled_other_days_and_future(conn):
    add(conn, "out", 100)
    add(conn, "out", 900, status="cancelled")
    add(conn, "out", 700, day="2026-10-13")
    add(conn, "out", 800, day="2026-10-15")
    rep = reports.report(conn, "day", None, TODAY)
    assert by_cur(rep)["TRY"]["out_cents"] == 100 and rep["count"] == 1
    assert [r["amount_cents"] for r in rep["records"]] == [100]


def test_day_report_by_explicit_date(conn):
    add(conn, "out", 700, day="2026-10-13")
    assert by_cur(reports.report(conn, "day", "2026-10-13", TODAY))["TRY"]["out_cents"] == 700


def test_day_report_empty(conn):
    rep = reports.report(conn, "day", None, TODAY)
    assert rep["currencies"] == [] and rep["records"] == [] and rep["count"] == 0
    assert rep["usd"] == {"out_cents": 0, "in_cents": 0, "net_cents": 0, "records_without_usd": 0}


def test_day_records_carry_method_title_and_order(conn):
    a = add(conn, "in", 100, "USD", method="bybit", category="доход", created="2026-10-14T08:00:00Z")
    b = add(conn, "out", 200, "TRY", created="2026-10-14T09:00:00Z")
    rep = reports.report(conn, "day", None, TODAY)
    assert [r["id"] for r in rep["records"]] == [b, a]                       # свежие сверху
    assert rep["records"][1]["method_title"] == "Bybit" and rep["records"][0]["method_title"] is None


def test_refund_recorded_as_negative_expense_reduces_total(conn):
    add(conn, "out", 10000)
    add(conn, "out", -3000, category="возврат")
    assert by_cur(reports.report(conn, "day", None, TODAY))["TRY"]["out_cents"] == 7000


def test_currencies_sorted_by_activity(conn):
    for _ in range(3):
        add(conn, "out", 1, "KZT")
    add(conn, "out", 1, "USD")
    add(conn, "out", 1, "AED")
    assert [c["currency"] for c in reports.report(conn, "day", None, TODAY)["currencies"]] == ["KZT", "AED", "USD"]


# --- Т3.1: месяц ------------------------------------------------------------------------------------------------
def test_month_bounds_include_first_and_last_day_only(conn):
    add(conn, "out", 1, day="2026-09-30")
    add(conn, "out", 10, day="2026-10-01")
    add(conn, "out", 100, day="2026-10-31")
    add(conn, "out", 1000, day="2026-11-01")
    rep = reports.report(conn, "month", "2026-10", TODAY)
    assert (rep["from"], rep["to"]) == ("2026-10-01", "2026-10-31")
    assert by_cur(rep)["TRY"]["out_cents"] == 110 and rep["count"] == 2


def test_month_default_is_current_and_february_leap():
    assert reports.period_bounds("month", None, "2026-10-14") == ("2026-10-01", "2026-10-31")
    assert reports.period_bounds("month", "2028-02", "2028-03-01") == ("2028-02-01", "2028-02-29")
    assert reports.period_bounds("month", "2027-02", "2028-03-01") == ("2027-02-01", "2027-02-28")


def test_month_by_category_and_method(conn):
    add(conn, "out", 1000, "TRY", category="кафе", usd=30)
    add(conn, "out", 500, "TRY", category="кафе", usd=15)
    add(conn, "out", 200, "USD", category="кафе", usd=200)
    add(conn, "out", 9999, "TRY", category="бар", usd=None)
    add(conn, "in", 70000, "USDT", category="доход", method="bybit", usd=70000)
    add(conn, "in", 5000, "RUB", category="доход", method=None, usd=60)
    add(conn, "in", 1000, "RUB", category="доход", method="tinkoff", usd=12)
    rep = reports.report(conn, "month", "2026-10", TODAY)
    cats = {g["name"]: g for g in rep["by_category"]}
    assert cats["кафе"]["count"] == 3 and cats["кафе"]["usd_cents"] == 245 and cats["кафе"]["without_usd"] == 0
    assert cats["кафе"]["currencies"] == [{"currency": "TRY", "cents": 1500}, {"currency": "USD", "cents": 200}]
    assert cats["бар"]["without_usd"] == 1 and cats["бар"]["usd_cents"] == 0
    methods = {g["name"]: g for g in rep["by_method"]}
    assert set(methods) == {"Bybit", "Тинькофф", "без способа"}
    assert methods["без способа"]["currencies"] == [{"currency": "RUB", "cents": 5000}]
    assert [g["name"] for g in rep["by_method"]] == ["Bybit", "без способа", "Тинькофф"]    # по USD, затем по числу
    assert "records" not in rep                                                              # у месяца нет списка


def test_month_groups_ignore_wrong_direction_and_cancelled(conn):
    add(conn, "in", 100, category="доход", method="cash")
    add(conn, "out", 50, category="кафе", status="cancelled")
    rep = reports.report(conn, "month", "2026-10", TODAY)
    assert rep["by_category"] == []
    assert [g["name"] for g in rep["by_method"]] == ["Наличные"]


@pytest.mark.parametrize("period,key", [
    ("day", "2026-10-15"), ("day", "2099-01-01"), ("day", "2026-02-30"), ("day", "abc"), ("day", "2026-10-14\n"),
    ("day", "٢٠٢٦-١٠-١٤"), ("day", "1999-12-31"),
    ("month", "2026-11"), ("month", "2026-13"), ("month", "2026-00"), ("month", "2026-1"), ("month", "x"),
    ("month", "2026-10\n"), ("month", "1999-12"), ("week", None),
])
def test_bad_period_is_422(conn, period, key):
    with pytest.raises(RecordError) as e:
        reports.report(conn, period, key, TODAY)
    assert (e.value.status, e.value.code) == (422, "bad_period")


def test_report_matches_independent_sql_on_random_data(conn):
    """Т3.1: отчёт против своего SQL по occurred_on и status='active' - на случайных данных, по всем дням месяца."""
    rnd = random.Random(20261014)
    for _ in range(400):
        add(conn, rnd.choice(["in", "out"]), rnd.choice([100, 250, 999, 12345, 5000000, -700]),
            rnd.choice(["TRY", "USD", "USDT", "RUB", "EUR", "KZT"]),
            day="2026-10-%02d" % rnd.randint(1, 14) if rnd.random() < .8 else "2026-09-%02d" % rnd.randint(1, 30),
            usd=rnd.choice([None, 0, 100, 3333, -50]), status=rnd.choice(["active"] * 4 + ["cancelled"]),
            category=rnd.choice(["кафе", "бар", "доход"]), method=rnd.choice([None, "bybit", "cash"]))
    for period, key, first, last in (("month", "2026-10", "2026-10-01", "2026-10-31"),
                                     ("month", "2026-09", "2026-09-01", "2026-09-30")) + tuple(
            ("day", "2026-10-%02d" % d, "2026-10-%02d" % d, "2026-10-%02d" % d) for d in range(1, 15)):
        rep = reports.report(conn, period, key, TODAY)
        want = {}
        for r in conn.execute("SELECT currency, direction, SUM(amount_cents) s, COUNT(*) n FROM transactions "
                              "WHERE status='active' AND occurred_on BETWEEN ? AND ? GROUP BY currency, direction",
                              (first, last)):
            w = want.setdefault(r["currency"], {"out_cents": 0, "in_cents": 0, "count": 0})
            w[r["direction"] + "_cents"] = r["s"]
            w["count"] += r["n"]
        got = {c["currency"]: (c["out_cents"], c["in_cents"], c["net_cents"], c["count"]) for c in rep["currencies"]}
        assert got == {k: (v["out_cents"], v["in_cents"], v["in_cents"] - v["out_cents"], v["count"])
                       for k, v in want.items()}
        u = conn.execute("SELECT COALESCE(SUM(CASE WHEN direction='out' THEN usd_cents END),0) o, "
                         "COALESCE(SUM(CASE WHEN direction='in' THEN usd_cents END),0) i, "
                         "SUM(usd_cents IS NULL) nn FROM transactions "
                         "WHERE status='active' AND occurred_on BETWEEN ? AND ?", (first, last)).fetchone()
        assert rep["usd"] == {"out_cents": u["o"], "in_cents": u["i"], "net_cents": u["i"] - u["o"],
                              "records_without_usd": u["nn"] or 0}


# --- Последние --------------------------------------------------------------------------------------------------
def test_recent_is_20_newest_with_cancelled_and_order(conn):
    ids = [add(conn, created="2026-09-%02dT10:00:00Z" % (i + 1), day=TODAY) for i in range(25)]
    gone = add(conn, status="cancelled", created="2026-10-14T23:00:00Z")
    res = reports.recent(conn)
    assert len(res["records"]) == 20
    assert res["records"][0]["id"] == gone and res["records"][0]["status"] == "cancelled"
    assert [r["id"] for r in res["records"][1:]] == list(reversed(ids))[:19]


def test_recent_no_method_filter(conn):
    a = add(conn, "in", category="доход", method=None)
    add(conn, "in", category="доход", method="cash")
    add(conn, "out")
    add(conn, "in", category="доход", method=None, status="cancelled")
    res = reports.recent(conn, no_method=True)
    assert [r["id"] for r in res["records"]] == [a] and res["no_method_total"] == 1


# --- Т3.2: удаление ---------------------------------------------------------------------------------------------
def test_cancel_soft_deletes_and_disappears_from_reports_and_view(conn):
    keep = add(conn, "out", 100)
    gone = add(conn, "out", 900)
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    legacy = conn.execute("SELECT COUNT(*) FROM v_legacy").fetchone()[0]
    res = records.cancel_record(conn, ACTOR, gone, now=NOW)
    assert res == {"ok": True, "id": gone, "status": "cancelled", "changed": True}
    row = conn.execute("SELECT status, cancelled_at, amount_cents FROM transactions WHERE id=?", (gone,)).fetchone()
    assert (row["status"], row["cancelled_at"], row["amount_cents"]) == ("cancelled", "2026-10-14T12:00:00Z", 900)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == total          # строка на месте
    assert conn.execute("SELECT COUNT(*) FROM v_legacy").fetchone()[0] == legacy - 1
    assert conn.execute("SELECT COUNT(*) FROM v_legacy WHERE id=?", (gone,)).fetchone()[0] == 0
    rep = reports.report(conn, "day", None, TODAY)
    assert by_cur(rep)["TRY"]["out_cents"] == 100 and [r["id"] for r in rep["records"]] == [keep]
    assert by_cur(reports.report(conn, "month", "2026-10", TODAY))["TRY"]["out_cents"] == 100
    audit = conn.execute("SELECT actor_id, action, entity_id FROM audit WHERE action='cancel'").fetchall()
    assert [(a["actor_id"], a["entity_id"]) for a in audit] == [(str(ACTOR), str(gone))]


def test_cancel_twice_is_idempotent_and_audited_once(conn):
    rid = add(conn)
    records.cancel_record(conn, ACTOR, rid, now=NOW)
    again = records.cancel_record(conn, ACTOR, rid, now=NOW + timedelta(hours=1))
    assert again["changed"] is False
    assert conn.execute("SELECT cancelled_at FROM transactions WHERE id=?", (rid,)).fetchone()[0] == "2026-10-14T12:00:00Z"
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action='cancel'").fetchone()[0] == 1




@pytest.mark.parametrize("rec_id", [0, -1, 99999999, "3", None, True, 1.5])
def test_cancel_unknown_ids_are_404(conn, rec_id):
    add(conn)
    for fn in (records.cancel_record, records.restore_record):
        with pytest.raises(RecordError) as e:
            fn(conn, ACTOR, rec_id, now=NOW)
        assert (e.value.status, e.value.code) == (404, "not_found")
    assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0


# --- Т3.3: возврат ----------------------------------------------------------------------------------------------
def test_restore_brings_record_back_everywhere(conn):
    rid = add(conn, "out", 900)
    records.cancel_record(conn, ACTOR, rid, now=NOW)
    legacy_cancelled = conn.execute("SELECT COUNT(*) FROM v_legacy").fetchone()[0]
    res = records.restore_record(conn, ACTOR, rid, now=NOW + timedelta(minutes=5))
    assert res == {"ok": True, "id": rid, "status": "active", "changed": True}
    row = conn.execute("SELECT status, cancelled_at FROM transactions WHERE id=?", (rid,)).fetchone()
    assert (row["status"], row["cancelled_at"]) == ("active", None)
    assert conn.execute("SELECT COUNT(*) FROM v_legacy").fetchone()[0] == legacy_cancelled + 1
    assert by_cur(reports.report(conn, "day", None, TODAY))["TRY"]["out_cents"] == 900
    assert by_cur(reports.report(conn, "month", "2026-10", TODAY))["TRY"]["out_cents"] == 900
    assert [r[0] for r in conn.execute("SELECT action FROM audit ORDER BY id")] == ["cancel", "restore"]


def test_restore_active_record_is_noop(conn):
    rid = add(conn)
    assert records.restore_record(conn, ACTOR, rid, now=NOW)["changed"] is False
    assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0


def test_cancel_restore_cycle_keeps_all_money_fields(conn):
    rid = add(conn, "in", 12345, "USDT", usd=12345, method="bybit", category="доход", note="n")
    before = dict(conn.execute("SELECT * FROM transactions WHERE id=?", (rid,)).fetchone())
    records.cancel_record(conn, ACTOR, rid, now=NOW)
    records.restore_record(conn, ACTOR, rid, now=NOW)
    assert dict(conn.execute("SELECT * FROM transactions WHERE id=?", (rid,)).fetchone()) == before


# --- Т3.4: смена категории и способа ----------------------------------------------------------------------------
def snapshot(conn, rid):
    return dict(conn.execute("SELECT * FROM transactions WHERE id=?", (rid,)).fetchone())


def test_update_category_writes_audit_and_keeps_amount_and_currency(conn):
    rid = add(conn, "out", 12345, "KZT", usd=2400, category="кафе", note="n")
    before = snapshot(conn, rid)
    res = records.update_record(conn, ACTOR, rid, {"category": "бар"}, now=NOW)
    assert res == {"ok": True, "id": rid, "changed": ["category"]}
    after = snapshot(conn, rid)
    assert after["category"] == "бар"
    assert {k: v for k, v in after.items() if k != "category"} == {k: v for k, v in before.items() if k != "category"}
    a = conn.execute("SELECT actor_id, action, entity_id, detail FROM audit").fetchall()
    assert len(a) == 1 and (a[0]["actor_id"], a[0]["action"], a[0]["entity_id"]) == (str(ACTOR), "update", str(rid))
    assert "12345" not in a[0]["detail"] and "KZT" not in a[0]["detail"]            # в audit нет сумм и валют


def test_update_method_income(conn):
    rid = add(conn, "in", 500, "USD", category="доход", method=None)
    records.update_record(conn, ACTOR, rid, {"method": "sber"}, now=NOW)
    assert snapshot(conn, rid)["method"] == "sber"
    records.update_record(conn, ACTOR, rid, {"new_method": "Мой банк"}, now=NOW)
    code = snapshot(conn, rid)["method"]
    assert code == "x:мой банк" and conn.execute("SELECT title FROM methods WHERE code=?", (code,)).fetchone()[0] == "Мой банк"
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action='update'").fetchone()[0] == 2


def test_update_method_cannot_be_cleared_for_income_or_expense(conn):
    inc = add(conn, "in", 500, category="доход", method="bybit")
    exp = add(conn, "out", 500, method="redot")
    for rid, was in ((inc, "bybit"), (exp, "redot")):
        for bad in ({"method": None}, {"method": ""}):
            with pytest.raises(RecordError) as e:
                records.update_record(conn, ACTOR, rid, bad, now=NOW)
            assert e.value.code == "method_required" and snapshot(conn, rid)["method"] == was


def test_legacy_expense_without_method_can_be_edited_and_gets_a_method(conn):
    old = add(conn, "out", 500, method=None, category="прочее")          # импорт: способ пуст, это допустимо
    records.update_record(conn, ACTOR, old, {"category": "кафе"}, now=NOW)
    assert snapshot(conn, old)["method"] is None
    records.update_record(conn, ACTOR, old, {"method": "kaspi"}, now=NOW)
    assert snapshot(conn, old)["method"] == "kaspi"


def test_update_method_scope_is_checked(conn):
    exp = add(conn, "out")
    inc = add(conn, "in", category="доход", method="cash")
    for rid, method, code in ((exp, "cold_wallet", "unknown_method"), (exp, "card", "unknown_method"),
                              (inc, "card", "unknown_method"), (inc, "nope", "unknown_method")):
        with pytest.raises(RecordError) as e:
            records.update_record(conn, ACTOR, rid, {"method": method}, now=NOW)
        assert e.value.code == code
    assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0
    records.update_record(conn, ACTOR, exp, {"new_method": "Новый"}, now=NOW)         # «Другой» есть и у расхода
    code = snapshot(conn, exp)["method"]
    assert conn.execute("SELECT scope FROM methods WHERE code=?", (code,)).fetchone()[0] == "out"


@pytest.mark.parametrize("payload", [
    {"date": "2026-10-01"}, {"direction": "in"}, {"status": "cancelled"}, {"category": "бар", "date": "2026-10-01"},
    {"id": 5}, {"usd_cents": 1}, {"amount_cents": 5}, {"idem_key": "x"},
])
def test_update_cannot_touch_amount_currency_or_anything_else(conn, payload):
    rid = add(conn, "out", 12345, "KZT", usd=2400, note="n")
    before = snapshot(conn, rid)
    with pytest.raises(RecordError) as e:
        records.update_record(conn, ACTOR, rid, payload, now=NOW)
    assert (e.value.status, e.value.code) == (422, "field_not_editable")
    assert snapshot(conn, rid) == before and conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0


@pytest.mark.parametrize("payload,code", [
    ({}, "bad_payload"), ([], "bad_payload"), ("x", "bad_payload"), (None, "bad_payload"),
    ({"category": ""}, "missing_category"), ({"category": None}, "missing_category"),
    ({"category": "a" * 41}, "bad_category"), ({"category": 5}, "bad_category"),
])
def test_update_validation(conn, payload, code):
    rid = add(conn)
    with pytest.raises(RecordError) as e:
        records.update_record(conn, ACTOR, rid, payload, now=NOW)
    assert e.value.code == code and snapshot(conn, rid)["category"] == "кафе"


def test_update_cancelled_record_is_409(conn):
    rid = add(conn, status="cancelled")
    with pytest.raises(RecordError) as e:
        records.update_record(conn, ACTOR, rid, {"category": "бар"}, now=NOW)
    assert (e.value.status, e.value.code) == (409, "record_cancelled")


def test_update_same_values_is_noop_without_audit(conn):
    rid = add(conn, "in", category="доход", method="cash")
    res = records.update_record(conn, ACTOR, rid, {"category": "доход", "method": "cash"}, now=NOW)
    assert res["changed"] == [] and conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0


def test_update_sql_text_stays_text(conn):
    rid = add(conn)
    records.update_record(conn, ACTOR, rid, {"category": "x'); DROP TABLE transactions;--"}, now=NOW)
    assert snapshot(conn, rid)["category"].startswith("x')")


# --- Т3.5: представление ----------------------------------------------------------------------------------------
def test_v_legacy_counts_only_active(conn):
    add(conn)
    add(conn)
    add(conn, status="cancelled")
    assert conn.execute("SELECT COUNT(*) FROM v_legacy").fetchone()[0] == \
        conn.execute("SELECT COUNT(*) FROM transactions WHERE status='active'").fetchone()[0] == 2


