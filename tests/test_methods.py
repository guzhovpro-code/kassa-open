"""Способы оплаты - данные базы: команда kassa_api.methods, миграция, разбор текста по списку, список в /api/meta."""
import json
from datetime import datetime, timezone

import pytest

from conftest import REAL_METHODS_SEED
from helpers import OWNER, make_init_data
from kassa_api import cards, db, methods, moneytext as mt, records
from kassa_api.records import RecordError
from test_api import PRIV, TOKEN, env, req  # noqa: F401  (фикстура env)

TODAY = "2026-10-20"
NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def bare(tmp_path, monkeypatch):
    """Новая база с настоящим начальным состоянием: один способ «Наличные» (conftest подкладывает выдуманный набор)."""
    monkeypatch.setattr(db, "METHODS_SEED", REAL_METHODS_SEED)
    path = str(tmp_path / "kassa-test.db")
    db.init_db(path)
    conn = db.connect(path)
    yield conn, path
    conn.close()


def titles(conn):
    return [m["title"] for m in methods.list_methods(conn)]


def cli(path, *argv):
    return methods.main(list(argv), {"KASSA_DB_PATH": path, "KASSA_ALLOW_PROD_DB": "1"})


def test_new_database_has_only_cash(bare):
    conn, _ = bare
    assert methods.list_methods(conn) == [{"code": "cash", "title": "Наличные", "scope": "both", "kind": "cash", "active": 1}]


def test_add_list_remove(bare):
    conn, _ = bare
    code, state = methods.add_method(conn, "Карта Revolut", "card")
    assert state == "added" and code == "x:карта revolut"
    methods.add_method(conn, "Биржа OKX", "exchange")
    assert titles(conn) == ["Наличные", "Карта Revolut", "Биржа OKX"]
    assert [m["kind"] for m in methods.list_methods(conn)] == ["cash", "card", "exchange"]
    assert methods.add_method(conn, "  карта   REVOLUT ", "card") == (code, "exists")       # без учёта регистра и пробелов
    methods.remove_method(conn, "карта revolut")
    assert titles(conn) == ["Наличные", "Биржа OKX"]
    assert methods.add_method(conn, "Карта Revolut", "card") == (code, "restored")
    assert titles(conn) == ["Наличные", "Карта Revolut", "Биржа OKX"]      # вернулся на прежнее место


@pytest.mark.parametrize("title, kind", [("", "other"), ("x" * 31, "other"), ("Другой", "other"), ("Карта", "bogus")])
def test_add_rejects_bad_input(bare, title, kind):
    conn, _ = bare
    with pytest.raises(methods.MethodError):
        methods.add_method(conn, title, kind)
    assert titles(conn) == ["Наличные"]


def test_remove_unknown(bare):
    conn, _ = bare
    with pytest.raises(methods.MethodError):
        methods.remove_method(conn, "Нет такого")


def test_cli(bare, capsys):
    _, path = bare
    assert cli(path, "add", "Карта Revolut", "--type", "card") == 0
    assert cli(path, "add", "Кошелёк Ledger", "--type", "wallet") == 0
    capsys.readouterr()
    assert cli(path, "list") == 0
    out = capsys.readouterr().out
    assert "Наличные\tcash" in out and "Карта Revolut\tcard" in out and "Кошелёк Ledger\twallet" in out
    assert cli(path, "remove", "Кошелёк Ledger") == 0
    assert cli(path, "remove", "Кошелёк Ledger") == 1                      # уже убран
    with pytest.raises(SystemExit):
        cli(path, "add", "X", "--type", "bogus")
    capsys.readouterr()
    assert cli(path, "list") == 0
    assert "Ledger" not in capsys.readouterr().out


def test_cli_needs_db_path(capsys):
    assert methods.main(["list"], {}) == 2


def test_removed_method_keeps_records_and_reports(bare):
    conn, path = bare
    methods.add_method(conn, "Карта Revolut", "card")
    code = "x:карта revolut"
    body = dict(client_id="c" * 16, direction="in", amount="100", currency="USD", category="доход", method=code,
                date=TODAY)
    conn.execute("INSERT INTO transactions(idem_key, direction, amount_cents, currency, usd_cents, category, method, "
                 "occurred_on, created_at, source, via, actor_id) VALUES ('k1','in',10000,'USD',10000,'доход',?,?,"
                 "'2026-10-20T00:00:00Z','kassa','webapp','1')", (code, TODAY))
    methods.remove_method(conn, "Карта Revolut")
    row = conn.execute("SELECT t.method, m.title, m.active FROM transactions t JOIN methods m ON m.code=t.method").fetchone()
    assert tuple(row) == (code, "Карта Revolut", 0)                         # запись цела, название видно в отчётах
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    from kassa_api import reports
    rep = reports.report(conn, "month", "2026-10", TODAY)
    assert [g["name"] for g in rep["by_method"]] == ["Карта Revolut"]
    with pytest.raises(RecordError) as e:                                   # новую запись с убранным способом не принять
        records.create_record(conn, str(OWNER), dict(body, client_id="d" * 16, date=TODAY), now=NOW)
    assert e.value.code == "unknown_method"


def test_other_in_form_restores_removed_method(bare):
    conn, _ = bare
    methods.add_method(conn, "Карта Revolut", "card")
    methods.remove_method(conn, "Карта Revolut")
    conn.execute("BEGIN IMMEDIATE")
    code = records._ensure_method(conn, "карта revolut", "out")
    conn.execute("COMMIT")
    assert code == "x:карта revolut" and "Карта Revolut" in titles(conn)


def test_migration_from_v7_keeps_existing_methods(tmp_path):
    """База схемы 7 (методы старого набора с выдуманными названиями) обновляется до 8: строки на месте, kind у cash."""
    path = str(tmp_path / "kassa-test.db")
    db.init_db(path)                                                        # conftest: выдуманный набор из 7 способов
    c = db.connect(path)
    c.execute("INSERT INTO transactions(idem_key, direction, amount_cents, currency, usd_cents, category, method, "
              "occurred_on, created_at, source, via, actor_id) VALUES ('k1','in',500,'USD',500,'доход','revolut',"
              "'2026-10-20','2026-10-20T00:00:00Z','kassa','webapp','1')")
    c.execute("ALTER TABLE methods DROP COLUMN kind")
    c.execute("PRAGMA user_version=7")
    before = [tuple(r) for r in c.execute("SELECT code, title, sort, active, scope FROM methods ORDER BY sort")]
    c.close()
    db.init_db(path)
    c = db.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 8
    assert [tuple(r) for r in c.execute("SELECT code, title, sort, active, scope FROM methods ORDER BY sort")] == before
    kinds = dict(c.execute("SELECT code, kind FROM methods").fetchall())
    assert kinds["cash"] == "cash" and kinds["revolut"] == "other"
    assert c.execute("SELECT method FROM transactions").fetchone()[0] == "revolut"
    c.close()
    db.init_db(path)                                                        # повторный запуск ничего не ломает


def test_current_schema_is_idempotent(bare):
    _, path = bare
    db.init_db(path)
    db.init_db(path)


# --- разбор текста по списку способов человека --------------------------------------------------------------------
LIST = [
    {"code": "cash", "title": "Наличные", "scope": "both"},
    {"code": "x:карта revolut", "title": "Карта Revolut", "scope": "both"},
    {"code": "x:биржа okx", "title": "Биржа OKX", "scope": "both"},
    {"code": "x:кошелек ledger", "title": "Кошелёк Ledger", "scope": "in"},
    {"code": "x:карта monzo", "title": "Карта Monzo", "scope": "both"},
]


def pm(text, methods_=LIST, **kw):
    return mt.parse(text, TODAY, methods=methods_, **kw)["method"]


def test_text_matches_title_or_key_word_case_insensitive():
    assert pm("потратил 20 евро на карту Revolut") == "x:карта revolut"       # слово «карту» не совпало, помог Revolut
    assert pm("оплатил 300 лир REVOLUT") == "x:карта revolut"
    assert pm("получил 100 usdt на Биржа okx", default_direction="in") == "x:биржа okx"
    assert pm("получил 100 usdt на бирже OKX", default_direction="in") == "x:биржа okx"
    assert pm("потратил 5 евро карта revolut") == "x:карта revolut"
    assert pm("потратил 20 лир наличными") == "cash"


def test_text_without_match_is_not_selected():
    assert pm("потратил 20 лир картой") is None                               # «карта» - слишком общее слово
    assert pm("потратил 20 лир на банк") is None
    assert pm("потратил 20 лир") is None
    assert pm("потратил 20 евро revol") is None                              # часть слова не считается


def test_text_uses_only_the_list_given():
    assert pm("потратил 20 лир наличными", None) == "cash"                    # без списка работает только «наличные»
    assert pm("потратил 20 евро revolut", None) is None
    assert pm("потратил 20 евро revolut", [LIST[0]]) is None                  # способа нет в списке человека
    assert pm("потратил 20 лир наличными", [LIST[1]]) is None                 # «Наличные» убраны
    assert pm("оплатил 20 евро monzo", [LIST[0], LIST[4]]) == "x:карта monzo"


def test_text_scope_respected():
    assert pm("потратил 20 евро с кошелька ledger") is None                   # Ledger только для поступлений
    assert pm("получил 20 евро на ledger", default_direction="in") == "x:кошелек ledger"


def test_text_ambiguous_key_word_selects_nothing():
    two = LIST[:1] + [{"code": "a", "title": "Карта Revolut Plus", "scope": "both"},
                      {"code": "b", "title": "Revolut Business", "scope": "both"}]
    assert pm("потратил 20 евро revolut", two) is None
    assert pm("потратил 20 евро карта revolut plus", two) == "a"              # название целиком точнее слова


def test_card_from_phrase_uses_methods_from_database(bare):
    conn, _ = bare
    methods.add_method(conn, "Карта Revolut", "card")
    res = cards.create_from_text(conn, str(OWNER), str(OWNER), "phrase", "потратил 20 евро на карту Revolut", "7")
    assert res["ok"]
    draft = json.loads(conn.execute("SELECT draft FROM cards ORDER BY id DESC LIMIT 1").fetchone()["draft"])
    assert draft["method"] == "x:карта revolut"
    methods.remove_method(conn, "Карта Revolut")
    res = cards.create_from_text(conn, str(OWNER), str(OWNER), "phrase", "потратил 30 евро на карту Revolut", "8")
    draft = json.loads(conn.execute("SELECT draft FROM cards ORDER BY id DESC LIMIT 1").fetchone()["draft"])
    assert draft["method"] is None                                           # убранный способ не выбирается


# --- форма видит способы из конфига ------------------------------------------------------------------------------
def test_meta_lists_exactly_the_configured_methods(bare):
    conn, _ = bare
    assert [m["title"] for m in records.meta(conn, True)["methods"]] == ["Наличные"]
    methods.add_method(conn, "Карта Revolut", "card")
    methods.add_method(conn, "Биржа OKX", "exchange")
    meta = records.meta(conn, True)
    assert [m["title"] for m in meta["methods"]] == ["Наличные", "Карта Revolut", "Биржа OKX"]
    assert meta["method_order"]["out"] == meta["method_order"]["in"] == ["cash", "x:карта revolut", "x:биржа okx"]
    methods.remove_method(conn, "Биржа OKX")
    assert [m["title"] for m in records.meta(conn, True)["methods"]] == ["Наличные", "Карта Revolut"]


def test_method_order_prefers_frequent(bare):
    conn, _ = bare
    methods.add_method(conn, "Карта Revolut", "card")
    methods.add_method(conn, "Биржа OKX", "exchange")
    for i in range(2):
        conn.execute("INSERT INTO transactions(idem_key, direction, amount_cents, currency, usd_cents, category, method, "
                     "occurred_on, created_at, source, via, actor_id) VALUES (?,'out',100,'USD',100,'прочее',?,?,"
                     "'2026-10-20T00:00:00Z','kassa','webapp','1')", ("k%d" % i, "x:биржа okx", TODAY))
    assert records.method_order(conn, TODAY)["out"] == ["x:биржа okx", "cash", "x:карта revolut"]


def test_api_meta_sees_methods_added_by_command(env, monkeypatch):
    port, path = env
    c = db.connect(path)
    methods.add_method(c, "Карта Revolut", "card")
    c.close()
    status, body = req(port, "GET", f"/{TOKEN}/api/meta", init=make_init_data(PRIV))
    assert status == 200
    assert "Карта Revolut" in [m["title"] for m in body["methods"]]
