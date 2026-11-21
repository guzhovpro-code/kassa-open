"""Подведение месяца: загрузка файла закрытия, сверка, пометки, повтор и замена (выдуманные данные)."""
import json
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kassa_api import db, month_close, records, reports
from kassa_api.records import RecordError

TODAY = "2026-11-10"
NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def path(tmp_path):
    p = tmp_path / "kassa-test.db"
    db.init_db(p)
    return str(p)


def close_file(**over):
    d = {
        "month": "2026-10",
        "rows": [
            {"date": "2026-10-01", "type": "income", "amount": "3000", "currency": "USD", "method": "Bank A",
             "category": "доход", "note": "salary"},
            {"date": "2026-10-03", "type": "expense", "amount": "150", "currency": "TRY", "method": "Cash",
             "note": "кофе", "rate": "0.03"},
            {"date": "2026-10-04", "type": "expense", "amount": "20.5", "currency": "USD", "method": "Bank A",
             "category": "транспорт"},
            {"date": "2026-10-05", "type": "transfer", "amount": "300", "currency": "USD", "method": "Bank A",
             "to_method": "Cash"},
        ],
        "totals": {"income_usd": "3000", "expense_usd": "25.00", "by_category": {"кафе": "4.50", "транспорт": "20.50"}},
    }
    d.update(over)
    return json.dumps(d, ensure_ascii=False).encode()


def add_hand(path, day="2026-10-10", cents=5000):
    c = db.connect(path)
    try:
        r = records.create_record(c, 1001, {"direction": "out", "amount": "%d.%02d" % (cents // 100, cents % 100),
                                            "currency": "USD", "date": day, "new_method": "Cash", "category": "кафе",
                                            "client_id": uuid.uuid4().hex}, now=NOW, fx_rates={})
        return r["id"]
    finally:
        c.close()


def test_load_writes_rows_marks_hand_records_and_skips_transfers(path):
    hand = add_hand(path)
    res = month_close.load(path, close_file())
    assert res["status"] == "loaded" and res["inserted_in"] == 1 and res["inserted_out"] == 2
    assert res["transfers_not_counted"] == 1 and res["superseded"] == {"kassa": 1}
    assert res["new_methods"] == ["Bank A"]      # Cash уже создала ручная запись
    c = db.connect(path)
    assert c.execute("SELECT closed_by FROM transactions WHERE id=?", (hand,)).fetchone()[0] == "2026-10"
    assert {r[0] for r in c.execute("SELECT source FROM transactions WHERE closed_by IS NULL")} == {"month-close"}
    rep = reports.report(c, "month", "2026-10", TODAY)
    assert rep["count"] == 3                       # ручная запись и перевод в отчёт не вошли
    assert rep["usd"]["in_cents"] == 300000 and rep["usd"]["out_cents"] == 2500
    assert rep["closed"]["matches"] is True
    c.close()


def test_transfer_is_neither_income_nor_expense(path):
    only_transfer = json.dumps({"month": "2026-10", "rows": [
        {"date": "2026-10-05", "type": "transfer", "amount": "300", "currency": "USD", "method": "A"}]}).encode()
    month_close.load(path, only_transfer)
    c = db.connect(path)
    rep = reports.report(c, "month", "2026-10", TODAY)
    assert rep["count"] == 0 and rep["usd"]["in_cents"] == 0 and rep["usd"]["out_cents"] == 0
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    c.close()


def test_verify_ok_and_show_marks_closed(path):
    month_close.load(path, close_file())
    v = month_close.verify(path, "2026-10", close_file(), today=TODAY)
    assert v["ok"] and v["report_marks_closed"] and v["file_unchanged"] and v["transfers"] == 1
    s = month_close.show(path, "2026-10", today=TODAY)
    assert s["closed"] and s["closed_matches"] and s["usd"] == {"in": "3000.00", "out": "25.00", "net": "2975.00"}


def test_verify_not_loaded_and_detects_tampering(path):
    assert month_close.verify(path, "2026-10", today=TODAY)["error"] == "not_loaded"
    month_close.load(path, close_file())
    c = sqlite3.connect(path)
    c.execute("UPDATE transactions SET usd_cents=usd_cents+100 WHERE source='month-close' AND direction='out'")
    c.commit()
    c.close()
    v = month_close.verify(path, "2026-10", today=TODAY)
    assert not v["ok"] and not v["report_marks_closed"]


def test_idempotent_repeat_and_changed_file_needs_replace(path):
    month_close.load(path, close_file())
    assert month_close.load(path, close_file())["status"] == "already_loaded"
    changed = close_file(rows=[{"date": "2026-10-02", "type": "income", "amount": "10", "currency": "USD"}],
                         totals={"income_usd": "10", "expense_usd": "0"})
    with pytest.raises(month_close.CloseError, match="file_changed_use_replace"):
        month_close.load(path, changed)
    res = month_close.load(path, changed, replace=True)
    assert res["status"] == "replaced" and res["replaced_rows"] == 3
    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM transactions WHERE source='month-close'").fetchone()[0] == 1
    rep = reports.report(c, "month", "2026-10", TODAY)
    assert rep["usd"]["in_cents"] == 1000 and rep["closed"]["matches"]
    c.close()
    assert month_close.verify(path, "2026-10", today=TODAY)["ok"]


def test_replace_releases_hand_records_and_marks_again(path):
    hand = add_hand(path)
    month_close.load(path, close_file())
    month_close.load(path, close_file(rows=[{"date": "2026-10-02", "type": "income", "amount": "1",
                                              "currency": "USD"}], totals={"income_usd": "1", "expense_usd": "0"}),
                     replace=True)
    c = db.connect(path)
    assert c.execute("SELECT closed_by FROM transactions WHERE id=?", (hand,)).fetchone()[0] == "2026-10"
    c.close()


def test_totals_mismatch_refuses_and_writes_nothing(path):
    bad = close_file(totals={"income_usd": "3000", "expense_usd": "99"})
    with pytest.raises(month_close.CloseError, match="rows_do_not_match_file_totals"):
        month_close.load(path, bad)
    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM closed_months").fetchone()[0] == 0
    c.close()


def test_category_mismatch_refuses(path):
    bad = close_file(totals={"income_usd": "3000", "expense_usd": "25", "by_category": {"кафе": "25"}})
    with pytest.raises(month_close.CloseError, match="rows_do_not_match_file_categories"):
        month_close.load(path, bad)


@pytest.mark.parametrize("row,code", [
    ({"date": "2026-11-01", "type": "income", "amount": "1", "currency": "USD"}, "row_outside_month"),
    ({"date": "2026-10-01", "type": "gift", "amount": "1", "currency": "USD"}, "bad_type"),
    ({"date": "2026-10-01", "type": "income", "amount": "-1", "currency": "USD"}, "bad_amount"),
    ({"date": "2026-10-01", "type": "income", "amount": "5", "currency": "TRY"}, "no_usd_rate"),
    ({"date": "2026-10-32", "type": "income", "amount": "5", "currency": "USD"}, "bad_date"),
    ({"date": "2026-10-01", "type": "income", "amount": "5", "currency": "u$d"}, "bad_currency"),
])
def test_bad_rows_are_refused(path, row, code):
    raw = json.dumps({"month": "2026-10", "rows": [row]}).encode()
    with pytest.raises(month_close.CloseError, match=code):
        month_close.load(path, raw)


def test_file_rates_and_explicit_usd(path):
    raw = json.dumps({"month": "2026-10", "rates": {"try": 0.025}, "rates_source": "test", "rows": [
        {"date": "2026-10-01", "type": "expense", "amount": "200", "currency": "TRY"},
        {"date": "2026-10-02", "type": "expense", "amount": "100", "currency": "EUR", "usd": "108.5"}]}).encode()
    month_close.load(path, raw)
    c = db.connect(path)
    assert [r[0] for r in c.execute("SELECT usd_cents FROM transactions ORDER BY id")] == [500, 10850]
    rep = reports.report(c, "month", "2026-10", TODAY)
    assert rep["closed"]["rates"] == {"TRY": 0.025} and rep["closed"]["rates_source"] == "test"
    c.close()


def test_without_totals_they_come_from_rows(path):
    raw = json.dumps({"month": "2026-10", "rows": [
        {"date": "2026-10-01", "type": "income", "amount": "7", "currency": "USDT"}]}).encode()
    month_close.load(path, raw)
    v = month_close.verify(path, "2026-10", today=TODAY)
    assert v["ok"] and v["totals_from_file"] is False


def test_closed_month_rejects_new_records(path):
    month_close.load(path, close_file())
    c = db.connect(path)
    with pytest.raises(RecordError) as e:
        records.create_record(c, 1001, {"direction": "out", "amount": "5", "currency": "USD", "date": "2026-10-20",
                                        "new_method": "Cash", "client_id": uuid.uuid4().hex}, now=NOW, fx_rates={})
    assert e.value.code == "month_closed"
    c.close()


def test_not_json_and_empty_file(path):
    with pytest.raises(month_close.CloseError, match="not_json"):
        month_close.load(path, b"hello")
    with pytest.raises(month_close.CloseError, match="no_rows"):
        month_close.load(path, json.dumps({"month": "2026-10", "rows": []}).encode())


def test_cli_load_verify_show(path, tmp_path):
    f = tmp_path / "close.json"
    f.write_bytes(close_file())
    app = str(Path(__file__).resolve().parent.parent / "app")
    env = {"PYTHONPATH": app, "KASSA_DB_PATH": path, "KASSA_TZ": "UTC", "PATH": "/usr/bin:/bin"}

    def run(*a):
        return subprocess.run([sys.executable, "-m", "kassa_api.month_close", *a], capture_output=True, text=True,
                              env=env)
    r = run("load", str(f))
    assert r.returncode == 0 and "loaded" in r.stdout
    assert run("load", str(f)).stdout.count("already_loaded") == 1
    r = run("verify", "2026-10", "--file", str(f))
    assert r.returncode == 0 and "совпало" in r.stdout
    r = run("show", "2026-10")
    assert r.returncode == 0 and '"closed_matches": true' in r.stdout
    assert "salary" not in run("load", str(f)).stdout
    assert run("verify", "2026-09").returncode == 1


def test_export_lists_hand_entered_records_as_close_rows(path):
    add_hand(path, "2026-10-10", 5000)
    add_hand(path, "2026-09-10", 7000)             # другой месяц не попадает
    rows = month_close.export_hand(path, "2026-10")
    assert rows == [{"date": "2026-10-10", "type": "expense", "amount": "50.00", "currency": "USD",
                     "method": "Cash", "category": "кафе", "note": "", "usd": "50.00"}]
    month_close.load(path, close_file())
    assert month_close.export_hand(path, "2026-10") == []      # после закрытия записи перекрыты
