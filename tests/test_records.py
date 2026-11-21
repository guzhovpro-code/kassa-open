import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from kassa_api import db, records
from kassa_api.records import RecordError

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)      # локально 15:00 14.10 (UTC+3)
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


# --- Т2.4 ------------------------------------------------------------------------------------------------
def test_expense_from_form(conn):
    cid = str(uuid.uuid4())
    res = records.create_record(conn, ACTOR, body(client_id=cid, amount="500"), now=NOW)
    assert res["ok"] and res["duplicate_request"] is False
    r = conn.execute("SELECT * FROM transactions WHERE id=?", (res["id"],)).fetchone()
    assert (r["via"], r["direction"], r["amount_cents"], r["currency"]) == ("webapp", "out", 50000, "TRY")
    assert r["actor_id"] == str(ACTOR) and r["idem_key"] == "webapp:" + cid
    assert r["status"] == "active" and r["source"] == "kassa" and r["usd_cents"] is None
    assert r["method"] == "cash"


def test_first_native_id_is_above_floor(conn):
    res = records.create_record(conn, ACTOR, body(), now=NOW)
    assert res["id"] == db.TEST_ID_FLOOR + 1


def test_expense_with_method_and_note(conn):
    res = records.create_record(conn, ACTOR, body(method="monzo", note="  кофе   у дома "), now=NOW)
    r = conn.execute("SELECT method, note FROM transactions WHERE id=?", (res["id"],)).fetchone()
    assert (r["method"], r["note"]) == ("monzo", "кофе у дома")


@pytest.mark.parametrize("method", ["cash", "monzo", "revolut", "n26", "okx", "wise"])
def test_expense_accepts_every_expense_method(conn, method):
    assert records.create_record(conn, ACTOR, body(method=method, client_id=str(uuid.uuid4())), now=NOW)["ok"]


@pytest.mark.parametrize("patch", [{"method": None}, {"method": ""}, {"method": "   "}])
def test_expense_without_method_is_422(conn, patch):
    p = body(**patch)
    if patch["method"] is None:
        del p["method"]
    e = err(conn, p)
    assert (e.status, e.code) == (422, "method_required")
    assert count(conn) == 0


def test_expense_cannot_use_income_only_method(conn):
    assert err(conn, body(method="ledger")).code == "unknown_method"
    assert err(conn, body(method="card")).code == "unknown_method"          # «Картой» снят: способы конкретные
    assert count(conn) == 0


# --- Т2.5 ------------------------------------------------------------------------------------------------
def test_double_press_and_repeat_give_one_row(conn):
    p = body()
    first = records.create_record(conn, ACTOR, p, now=NOW)
    second = records.create_record(conn, ACTOR, dict(p), now=NOW + timedelta(seconds=1))
    third = records.create_record(conn, ACTOR, dict(p), now=NOW + timedelta(minutes=30))
    assert first["id"] == second["id"] == third["id"]
    assert (first["duplicate_request"], second["duplicate_request"]) == (False, True)
    assert count(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action='create'").fetchone()[0] == 1


def test_same_client_id_other_payload_is_conflict(conn):
    p = body()
    records.create_record(conn, ACTOR, p, now=NOW)
    assert err(conn, dict(p, amount="501")).code == "idem_conflict"
    assert count(conn) == 1


def test_parallel_requests_one_row(path):
    p = body()
    results, errors = [], []

    def worker():
        c = db.connect(path)
        try:
            results.append(records.create_record(c, ACTOR, dict(p), now=NOW)["id"])
        except Exception as exc:       # noqa: BLE001
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(set(results)) == 1
    c = db.connect(path)
    assert count(c) == 1


# --- Т2.6 / Т2.7 -----------------------------------------------------------------------------------------
def test_income_without_method_is_422(conn):
    e = err(conn, body(direction="in", category="доход", method=None))
    assert (e.status, e.code) == (422, "method_required")
    assert count(conn, "via='webapp' AND direction='in' AND method IS NULL") == 0
    assert count(conn) == 0


def test_income_with_method(conn):
    res = records.create_record(conn, ACTOR, body(direction="in", category="доход", method="monzo", currency="USD"),
                                now=NOW)
    r = conn.execute("SELECT * FROM transactions WHERE id=?", (res["id"],)).fetchone()
    assert (r["direction"], r["method"], r["amount_cents"]) == ("in", "monzo", 50000)


def test_all_decided_methods_exist(conn):
    titles = {m["title"] for m in records.meta(conn, True, NOW)["methods"] if m["scope"] in ("in", "both")}
    assert {"Биржа OKX", "Карта Revolut", "Карта Wise", "Карта Monzo", "Карта N26", "Наличные", "Кошелёк Ledger"} <= titles


def test_new_method_other(conn):
    before = conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0]
    res = records.create_record(conn, ACTOR, body(direction="in", category="доход", method=None,
                                                  new_method="  Ваш  Банк "), now=NOW)
    r = conn.execute("SELECT method FROM transactions WHERE id=?", (res["id"],)).fetchone()
    m = conn.execute("SELECT * FROM methods WHERE code=?", (r["method"],)).fetchone()
    assert m["title"] == "Ваш Банк" and m["scope"] == "in" and m["active"] == 1
    assert conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0] == before + 1
    # тот же способ другим регистром не плодит строку, запись получает прежний код
    res2 = records.create_record(conn, ACTOR, body(direction="in", category="доход", method=None,
                                                   new_method="ваш банк", amount="501"), now=NOW)
    r2 = conn.execute("SELECT method FROM transactions WHERE id=?", (res2["id"],)).fetchone()
    assert r2["method"] == r["method"]
    assert conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0] == before + 1
    # и его видно в списке для поступлений, но не для расходов
    meta = records.meta(conn, True, NOW)
    assert any(x["code"] == r["method"] and x["scope"] == "in" for x in meta["methods"])
    # тем же названием у расхода способ становится общим (одна строка, scope both)
    res3 = records.create_record(conn, ACTOR, body(method=None, new_method="Ваш банк", amount="502"), now=NOW)
    assert conn.execute("SELECT method FROM transactions WHERE id=?", (res3["id"],)).fetchone()[0] == r["method"]
    assert conn.execute("SELECT scope FROM methods WHERE code=?", (r["method"],)).fetchone()[0] == "both"
    assert conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0] == before + 1


def test_new_method_for_expense_has_expense_scope(conn):
    res = records.create_record(conn, ACTOR, body(method=None, new_method="Карта друга"), now=NOW)
    code = conn.execute("SELECT method FROM transactions WHERE id=?", (res["id"],)).fetchone()[0]
    assert conn.execute("SELECT scope FROM methods WHERE code=?", (code,)).fetchone()[0] == "out"
    assert err(conn, body(direction="in", category="доход", method=code, client_id=str(uuid.uuid4()))).code ==         "unknown_method"


def test_new_method_matching_existing_title_reuses_it(conn):
    res = records.create_record(conn, ACTOR, body(direction="in", category="доход", method=None, new_method="КАРТА WISE"),
                                now=NOW)
    assert conn.execute("SELECT method FROM transactions WHERE id=?", (res["id"],)).fetchone()[0] == "wise"


@pytest.mark.parametrize("title", ["Другой", "другая", "  ДРУГОЕ "])
def test_new_method_reserved_word_rejected(conn, title):
    assert err(conn, body(direction="in", category="доход", method=None, new_method=title)).code == "bad_new_method"


def test_new_method_not_with_code_and_not_too_long(conn):
    assert err(conn, body(direction="in", category="доход", method="okx", new_method="X")).code == "method_conflict"
    assert err(conn, body(method="cash", new_method="X")).code == "method_conflict"
    assert err(conn, body(direction="in", category="доход", method=None, new_method="x" * 31)).code ==         "bad_new_method"
    assert count(conn) == 0


def test_failed_record_does_not_leave_new_method(conn):
    before = conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0]
    err(conn, body(direction="in", category="доход", method=None, new_method="Новый", amount="0"))
    assert conn.execute("SELECT COUNT(*) FROM methods").fetchone()[0] == before


# --- Т2.8 ------------------------------------------------------------------------------------------------
def test_backdated(conn):
    res = records.create_record(conn, ACTOR, body(date="2026-10-11"), now=NOW)
    r = conn.execute("SELECT occurred_on, created_at FROM transactions WHERE id=?", (res["id"],)).fetchone()
    assert r["occurred_on"] == "2026-10-11" and r["created_at"] == "2026-10-14T12:00:00Z"


def test_local_today_is_istanbul(conn):
    late = datetime(2026, 10, 14, 22, 30, tzinfo=timezone.utc)     # локально уже 15.10
    assert db.today_local(late) == "2026-10-15"
    assert records.create_record(conn, ACTOR, body(date="2026-10-15"), now=late)["ok"]
    assert err(conn, body(date="2026-10-16"), now=late).code == "date_in_future"


@pytest.mark.parametrize("d,code", [("2026-10-15", "date_in_future"), ("2025-10-12", "date_too_old"),
                                   ("2026-13-01", "bad_date"), ("14.10.2026", "bad_date"), (None, "bad_date"),
                                   ("2026-02-30", "bad_date")])
def test_bad_dates(conn, d, code):
    assert err(conn, body(date=d)).code == code


def test_oldest_allowed_date(conn):
    assert records.create_record(conn, ACTOR, body(date="2025-10-13"), now=NOW)["ok"]       # ровно 366 дней


# --- Т2.9 ------------------------------------------------------------------------------------------------
def seed_expenses(conn, amounts_cents, currency="TRY", day="2026-10-01", status="active", direction="out"):
    for a in amounts_cents:
        conn.execute(
            "INSERT INTO transactions(idem_key, direction, amount_cents, currency, category, occurred_on, created_at, "
            "status, source, via, actor_id) VALUES (?,?,?,?, 'x', ?, '2026-10-01T00:00:00Z', ?, 'kassa', 'webapp', '1')",
            (str(uuid.uuid4()), direction, a, currency, day, status))


def test_outlier_needs_explicit_confirmation(conn):
    seed_expenses(conn, [10000] * 10)                                  # медиана 100,00
    before = count(conn)
    e = err(conn, body(amount="1000.01"))
    assert (e.status, e.code, e.extra["warnings"]) == (409, "confirm_required", ["outlier"])
    assert count(conn) == before                                        # записи нет
    ok = records.create_record(conn, ACTOR, body(amount="1000.01", confirm=["outlier"]), now=NOW)
    assert ok["ok"] and count(conn) == before + 1


def test_outlier_boundary_is_strict(conn):
    seed_expenses(conn, [10000] * 10)
    assert records.create_record(conn, ACTOR, body(amount="1000"), now=NOW)["ok"]   # ровно 10 медиан - можно


def test_outlier_even_count_median(conn):
    seed_expenses(conn, [10000] * 5 + [30000] * 5)                      # медиана (100+300)/2 = 200,00
    assert err(conn, body(amount="2000.01")).code == "confirm_required"
    assert records.create_record(conn, ACTOR, body(amount="2000"), now=NOW)["ok"]


def test_outlier_needs_ten_records_same_currency_active_window_and_expenses(conn):
    seed_expenses(conn, [10000] * 9)
    assert records.create_record(conn, ACTOR, body(amount="99999"), now=NOW)["ok"]            # только 9 записей
    seed_expenses(conn, [10000] * 10, currency="USD")
    assert records.create_record(conn, ACTOR, body(amount="99999", currency="EUR"), now=NOW)["ok"]
    seed_expenses(conn, [10000] * 10, status="cancelled", currency="GBP")
    assert records.create_record(conn, ACTOR, body(amount="99999", currency="GBP"), now=NOW)["ok"]
    seed_expenses(conn, [10000] * 10, day="2026-07-01", currency="KZT")                       # старше 60 дней
    assert records.create_record(conn, ACTOR, body(amount="99999", currency="KZT"), now=NOW)["ok"]
    seed_expenses(conn, [10000] * 10, direction="in", currency="AED")                          # это поступления
    assert records.create_record(conn, ACTOR, body(amount="99999", currency="AED"), now=NOW)["ok"]


def test_outlier_does_not_apply_to_income(conn):
    seed_expenses(conn, [10000] * 10)
    assert records.create_record(conn, ACTOR, body(direction="in", category="доход", method="okx",
                                                   amount="999999"), now=NOW)["ok"]


def test_possible_duplicate_warns_not_forbids(conn):
    records.create_record(conn, ACTOR, body(amount="250"), now=NOW)
    e = err(conn, body(amount="250"), now=NOW + timedelta(minutes=5))
    assert (e.status, e.extra["warnings"]) == (409, ["possible_duplicate"])
    assert count(conn) == 1
    res = records.create_record(conn, ACTOR, body(amount="250", confirm=["possible_duplicate"]),
                                now=NOW + timedelta(minutes=5))
    assert res["ok"] and count(conn) == 2
    # через 11 минут предупреждения нет (последняя похожая запись уже старше окна)
    assert records.create_record(conn, ACTOR, body(amount="250"), now=NOW + timedelta(minutes=17))["ok"]
    # другая сумма, валюта или направление - не дубль
    assert records.create_record(conn, ACTOR, body(amount="251"), now=NOW + timedelta(minutes=17))["ok"]
    assert records.create_record(conn, ACTOR, body(amount="250", currency="USD"), now=NOW + timedelta(minutes=17))["ok"]


def test_confirm_for_other_warning_is_not_enough(conn):
    seed_expenses(conn, [10000] * 10)
    records.create_record(conn, ACTOR, body(amount="2000", confirm=["outlier"]), now=NOW)
    e = err(conn, body(amount="2000", confirm=["outlier"]), now=NOW + timedelta(minutes=1))
    assert e.extra["warnings"] == ["possible_duplicate"]


# --- проверки полей --------------------------------------------------------------------------------------
@pytest.mark.parametrize("patch,code", [
    ({"amount": "0"}, "amount_not_positive"), ({"amount": "-1"}, "bad_amount"), ({"amount": 5}, "bad_amount"),
    ({"amount": "1.001"}, "too_many_decimals"), ({"direction": "x"}, "bad_direction"),
    ({"currency": "try1"}, "bad_currency"), ({"currency": "ЛИР"}, "bad_currency"), ({"currency": None}, "bad_currency"),
    ({"category": "a" * 41}, "bad_category"), ({"category": "x\u0000y"}, "bad_category"),
    ({"note": "n" * 201}, "bad_note"), ({"client_id": "short"}, "bad_client_id"), ({"client_id": None}, "bad_client_id"),
    ({"client_id": "x" * 20 + "<script>"}, "bad_client_id"), ({"confirm": "outlier"}, "bad_confirm"),
])
def test_validation(conn, patch, code):
    e = err(conn, body(**patch))
    assert (e.status, e.code) == (422, code)
    assert count(conn) == 0


def test_payload_not_dict(conn):
    assert err(conn, ["x"]).code == "bad_payload"


def test_currency_is_upper_cased_and_scr_allowed(conn):
    res = records.create_record(conn, ACTOR, body(currency=" scr "), now=NOW)
    assert conn.execute("SELECT currency FROM transactions WHERE id=?", (res["id"],)).fetchone()[0] == "SCR"


def test_sql_injection_text_is_just_text(conn):
    res = records.create_record(conn, ACTOR, body(note="x'); DROP TABLE transactions;--", category="а'b"), now=NOW)
    assert count(conn) == 1
    assert conn.execute("SELECT note FROM transactions WHERE id=?", (res["id"],)).fetchone()[0].startswith("x')")


def test_atomicity_audit_failure_rolls_back_record(conn):
    conn.execute("CREATE TRIGGER boom BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(Exception):
        records.create_record(conn, ACTOR, body(), now=NOW)
    assert count(conn) == 0 and not conn.in_transaction


def test_db_constraints_block_nonsense(conn):
    import sqlite3
    base = ("INSERT INTO transactions(idem_key, direction, amount_cents, currency, occurred_on, created_at, source, "
            "via, actor_id) VALUES (?,?,?,?, '2026-10-14', 'x', 'kassa', ?, ?)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(base, ("k1", "out", 0, "TRY", "webapp", "1"))             # нулевая сумма
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(base, ("k2", "sideways", 100, "TRY", "webapp", "1"))      # направление
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(base, ("k3", "out", 100, "TRY", "webapp", None))          # запись формы без автора
    conn.execute(base, ("k4", "out", 100, "TRY", "finbot-import", None))      # импорт без автора можно
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(base, ("k4", "out", 100, "TRY", "finbot-import", None))   # повтор ключа


# --- meta -----------------------------------------------------------------------------------------------
def test_meta_last_currency_and_flags(conn):
    m = records.meta(conn, True, NOW)
    assert m["test"] is True and m["today"] == "2026-10-14"
    assert m["last_currency"] == {"out": "TRY", "in": "TRY"}
    records.create_record(conn, ACTOR, body(currency="USD"), now=NOW)
    records.create_record(conn, ACTOR, body(direction="in", category="доход", method="okx", currency="USDT"),
                          now=NOW + timedelta(seconds=5))
    m = records.meta(conn, True, NOW)
    assert m["last_currency"] == {"out": "USD", "in": "USDT"}
    assert m["categories"]["out"][:3] == ["продукты", "кафе", "бар"] and "прочее" in m["categories"]["out"]
    assert m["categories"]["in"] == ["доход", "возврат", "другое"]
    assert len(m["categories"]["out"]) == 14
    assert m["defaults"] == {"out": "прочее", "in": "доход"}
    out_methods = {x["code"] for x in m["methods"] if x["scope"] in ("out", "both")}
    assert out_methods == {"cash", "monzo", "revolut", "n26", "okx", "wise"}
    assert "card" not in {x["code"] for x in m["methods"]}              # «Картой» снят


# --- база и боевая kassa.db -----------------------------------------------------------------------------
def test_prod_db_name_refused_and_not_created(tmp_path):
    p = tmp_path / "kassa.db"
    with pytest.raises(SystemExit):
        db.init_db(p, env={})
    assert not p.exists()
    with pytest.raises(SystemExit):
        db.init_db(p, env={"KASSA_ALLOW_PROD_DB": "0"})
    assert not p.exists()


def test_prod_db_allowed_only_with_explicit_flag_and_has_no_id_floor(tmp_path):
    p = tmp_path / "kassa.db"
    assert db.init_db(p, env={"KASSA_ALLOW_PROD_DB": "1"}) is False
    c = db.connect(p)
    res = records.create_record(c, ACTOR, body(), now=NOW)
    assert res["id"] == 1


def test_init_db_idempotent_and_wal(path):
    db.init_db(path)
    db.init_db(path)
    c = db.connect(path)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert c.execute("SELECT COUNT(*) FROM methods").fetchone()[0] == len(db.METHODS_SEED)
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_v_legacy_shape_and_filters_cancelled(conn):
    seed_expenses(conn, [12345])
    seed_expenses(conn, [999], status="cancelled")
    rows = conn.execute("SELECT * FROM v_legacy").fetchall()
    assert len(rows) == 1
    assert set(rows[0].keys()) == {"id", "date", "direction", "amount", "currency", "amount_usd", "category", "note",
                                   "source", "created_at", "method"}
    assert rows[0]["amount"] == 123.45 and rows[0]["source"] == "bot" and rows[0]["amount_usd"] is None
