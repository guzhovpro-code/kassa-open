"""Этап 4 (правки по отзыву владельца): категория по тексту «на что», правка суммы и валюты с аудитом, /api/categorize."""
import http.client
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from helpers import BOT_ID, OWNER, STRANGER, freeze_local, make_init_data, new_key
from kassa_api import categorize, db, records, reports
from kassa_api.records import RecordError
from kassa_api.server import Config, make_servers

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


def body(**kw):
    base = {"client_id": str(uuid.uuid4()), "direction": "out", "amount": "500", "currency": "TRY", "date": TODAY, "method": "cash"}
    base.update(kw)
    return base


def create(conn, **kw):
    return records.create_record(conn, ACTOR, body(**kw), now=NOW)


def row(conn, rid):
    return dict(conn.execute("SELECT * FROM transactions WHERE id=?", (rid,)).fetchone())


def audits(conn, action=None):
    q = "SELECT * FROM audit" + (" WHERE action=?" if action else "") + " ORDER BY id"
    return [dict(r) for r in conn.execute(q, (action,) if action else ())]


def update(conn, rid, payload, **kw):
    return records.update_record(conn, ACTOR, rid, payload, now=kw.pop("now", NOW), **kw)


# --- словарь категорий -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("продукты", "продукты"), ("Продукты", "продукты"), ("еда в кафе", "кафе"), ("оплата ЖКХ", "жильё"),
    ("жкх за октябрь", "жильё"), ("подарки", "подарки"), ("Подарок маме", "подарки"), ("такси", "транспорт"),
    ("Такси домой", "транспорт"), ("Uber", "транспорт"), ("метро", "транспорт"),
    ("кофе", "кафе"), ("Пятёрочка", "продукты"), ("пятерочка", "продукты"), ("Мигрос", "продукты"),
    ("бензин", "топливо"), ("заправка", "топливо"), ("аптека", "быт"), ("аренда квартиры", "жильё"),
    ("в баре", "бар"), ("бар", "бар"), ("пиво", "бар"), ("кино", "развлечения"), ("курсор подписка", "AI-подписка"),
    ("Claude", "AI-подписка"), ("курсы английского", "обучение"), ("перевод другу", "перевод"),
    ("игрушки для сына", "семья"), ("что-то странное", "прочее"), ("", "прочее"), ("   ", "прочее"), (None, "прочее"),
    ("123", "прочее"),
    # граничные случаи слов-подстрок
    ("5 километров пешком", "прочее"), ("барбершоп", "прочее"), ("киноа", "прочее"), ("кинолог", "прочее"),
])
def test_expense_dictionary(text, expected):
    assert categorize.categorize(text, "out") == expected


@pytest.mark.parametrize("text,expected", [
    ("возврат долга", "возврат"), ("Вернули деньги", "возврат"), ("кэшбэк", "возврат"), ("клиент оплатил", "доход"),
    ("", "доход"), (None, "доход"), ("продукты", "доход"),
])
def test_income_dictionary(text, expected):
    assert categorize.categorize(text, "in") == expected


def test_every_dictionary_category_is_in_the_dropdown_list():
    names = {name for name, _ in categorize._RULES}
    assert names <= set(categorize.OUT_CATEGORIES)
    assert categorize.OUT_CATEGORIES[-1] == "прочее" and len(set(categorize.OUT_CATEGORIES)) == len(categorize.OUT_CATEGORIES)
    assert categorize.categorize("zzz", "out") in categorize.OUT_CATEGORIES


def test_dictionary_keeps_the_finbot_categories():
    """Все 12 категорий парсера импорта остались в словаре, их ключевые слова дают прежний результат."""
    for text, cat in [("claude", "AI-подписка"), ("магазин", "продукты"), ("ужин", "кафе"), ("коктейль", "бар"),
                      ("боулинг", "развлечения"), ("дизель", "топливо"), ("лекарства", "быт"), ("ипотека", "жильё"),
                      ("детский сад", "семья"), ("школа", "обучение"), ("перевод", "перевод")]:
        assert categorize.categorize(text, "out") == cat


# --- создание: категория сама -----------------------------------------------------------------------------------
@pytest.mark.parametrize("note,cat", [("продукты", "продукты"), ("еда в кафе", "кафе"), ("оплата ЖКХ", "жильё"),
                                      ("подарки", "подарки"), ("такси", "транспорт"), ("что-то", "прочее")])
def test_category_is_assigned_from_note_when_not_given(conn, note, cat):
    res = create(conn, note=note)
    assert res["category"] == cat
    r = row(conn, res["id"])
    assert (r["category"], r["note"]) == (cat, note)                  # текст «на что» сохранён в заметку
    assert json.loads(audits(conn, "create")[-1]["detail"])["category_auto"] is True


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_category_means_auto_and_does_not_block(conn, value):
    res = create(conn, note="такси", category=value)
    assert row(conn, res["id"])["category"] == "транспорт"


def test_no_text_and_no_category_is_saved_as_other(conn):
    res = create(conn)
    r = row(conn, res["id"])
    assert (r["category"], r["note"], r["status"]) == ("прочее", None, "active")


def test_unrecognized_text_is_saved_as_other_and_text_is_kept(conn):
    res = create(conn, note="непонятное слово")
    r = row(conn, res["id"])
    assert (r["category"], r["note"]) == ("прочее", "непонятное слово")


def test_manual_category_wins_over_text(conn):
    res = create(conn, note="такси", category="бар")
    r = row(conn, res["id"])
    assert (r["category"], r["note"]) == ("бар", "такси")
    assert json.loads(audits(conn, "create")[-1]["detail"])["category_auto"] is False


def test_custom_category_is_still_allowed(conn):
    assert row(conn, create(conn, note="x", category="Кипр платёж")["id"])["category"] == "Кипр платёж"


def test_income_category_auto_and_method_still_required(conn):
    res = create(conn, direction="in", note="возврат долга", method="cash")
    assert row(conn, res["id"])["category"] == "возврат"
    res = create(conn, direction="in", note="клиент", method="cash", amount="501")
    assert row(conn, res["id"])["category"] == "доход"
    with pytest.raises(RecordError) as e:
        create(conn, direction="in", note="клиент", method=None)
    assert e.value.code == "method_required"


def test_repeat_with_auto_category_is_the_same_record(conn):
    p = body(note="такси")
    first = records.create_record(conn, ACTOR, p, now=NOW)
    again = records.create_record(conn, ACTOR, dict(p), now=NOW + timedelta(minutes=20))
    assert again["id"] == first["id"] and again["duplicate_request"] is True and again["category"] == "транспорт"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    # то же, но с другим текстом - другая категория -> конфликт, а не «уже записано»
    with pytest.raises(RecordError) as e:
        records.create_record(conn, ACTOR, dict(p, note="кофе"), now=NOW)
    assert e.value.code == "idem_conflict"


def test_meta_has_category_lists_and_defaults(conn):
    m = records.meta(conn, True, NOW)
    assert m["categories"]["out"] == categorize.OUT_CATEGORIES and m["categories"]["in"] == categorize.IN_CATEGORIES
    assert m["defaults"] == {"out": "прочее", "in": "доход"}
    assert m["currencies"]["main"] == ["TRY", "USDT", "USD", "RUB"] and m["currencies"]["second"] == ["EUR"]


# --- правка суммы и валюты ---------------------------------------------------------------------------------------
def seed(conn, cents=10000, cur="TRY", usd=None, direction="out", orig=None, day=TODAY, source="kassa", **kw):
    cur_ = conn.execute(
        "INSERT INTO transactions(idem_key, direction, amount_cents, amount_orig, currency, usd_cents, category, "
        "note, method, occurred_on, created_at, status, source, via, actor_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("s-" + uuid.uuid4().hex, direction, cents, orig, cur, usd, kw.get("category", "кафе"), kw.get("note"),
         kw.get("method"), day, day + "T09:00:00Z", kw.get("status", "active"), source,
         "finbot-import" if source != "kassa" else "webapp", None if source != "kassa" else "1"))
    return cur_.lastrowid


def test_amount_can_be_changed_and_reports_follow(conn):
    rid = seed(conn, 10000, "TRY")
    before = reports.report(conn, "day", None, TODAY)
    assert [c["out_cents"] for c in before["currencies"]] == [10000]
    res = update(conn, rid, {"amount": "250,5"})
    assert res["changed"] == ["amount"]
    assert row(conn, rid)["amount_cents"] == 25050
    for rep in (reports.report(conn, "day", None, TODAY), reports.report(conn, "month", None, TODAY)):
        assert [(c["currency"], c["out_cents"], c["net_cents"]) for c in rep["currencies"]] == [("TRY", 25050, -25050)]
    assert reports.recent(conn)["records"][0]["amount_cents"] == 25050


def test_currency_can_be_changed_and_reports_follow(conn):
    rid = seed(conn, 10000, "TRY")
    res = update(conn, rid, {"currency": "usd"})
    assert res["changed"] == ["currency"] and row(conn, rid)["currency"] == "USD"
    rep = reports.report(conn, "day", None, TODAY)
    assert [(c["currency"], c["out_cents"]) for c in rep["currencies"]] == [("USD", 10000)]


def test_amount_and_currency_together_and_everything_else(conn):
    rid = seed(conn, 10000, "TRY", note="старое", category="кафе")
    res = update(conn, rid, {"amount": "12", "currency": "EUR", "note": "новое", "category": "бар"})
    assert res["changed"] == ["amount", "category", "currency", "note"]
    r = row(conn, rid)
    assert (r["amount_cents"], r["currency"], r["note"], r["category"]) == (1200, "EUR", "новое", "бар")


def test_audit_has_was_and_became_who_and_when(conn):
    rid = seed(conn, 10000, "TRY", usd=300, note="н")
    update(conn, rid, {"amount": "200", "currency": "RUB", "category": "бар"}, now=NOW)
    (a,) = audits(conn, "update")
    assert (a["actor_id"], a["ts"], a["entity"], a["entity_id"], a["via"]) == \
        (str(ACTOR), "2026-10-14T12:00:00Z", "transaction", str(rid), "webapp")
    d = json.loads(a["detail"])["changed"]
    assert d["amount_cents"] == [10000, 20000] and d["currency"] == ["TRY", "RUB"]
    assert d["category"] == ["кафе", "бар"]
    assert d["usd_cents"] == [300, None]                              # валюта сменилась -> курса нет


def test_same_values_change_nothing_and_write_no_audit(conn):
    rid = seed(conn, 10000, "TRY", usd=300, note="н")
    snap = row(conn, rid)
    res = update(conn, rid, {"amount": "100", "currency": "TRY", "category": "кафе", "note": "н"})
    assert res["changed"] == [] and row(conn, rid) == snap and audits(conn, "update") == []


def test_repeating_the_same_change_is_idempotent(conn):
    rid = seed(conn, 10000, "TRY", usd=300)
    assert update(conn, rid, {"amount": "250", "currency": "USD"})["changed"] == ["amount", "currency"]
    snap = row(conn, rid)
    assert update(conn, rid, {"amount": "250", "currency": "USD"})["changed"] == []
    assert row(conn, rid) == snap and len(audits(conn, "update")) == 1


def test_usd_snapshot_is_rescaled_when_only_amount_changes(conn):
    rid = seed(conn, 10000, "TRY", usd=300)
    update(conn, rid, {"amount": "250"})                              # x2.5 по тому же курсу
    r = row(conn, rid)
    assert (r["amount_cents"], r["usd_cents"]) == (25000, 750)
    assert json.loads(audits(conn, "update")[0]["detail"])["changed"]["usd_cents"] == [300, 750]


@pytest.mark.parametrize("usd,old,new,expected", [
    (100, 300, 100, 33),      # 33,33 -> 33
    (100, 300, 200, 67),      # 66,67 -> 67
    (1, 2, 1, 1),             # 0,5 -> половина вверх
    (5, 10, 1, 1),            # 0,5 -> 1
    (0, 500, 900, 0),
    (-100, 300, 600, -200),
])
def test_rescale_usd_rounding(usd, old, new, expected):
    assert records._rescale_usd(usd, old, new) == expected


def test_usd_without_rate_stays_null(conn):
    rid = seed(conn, 10000, "TRY", usd=None)
    update(conn, rid, {"amount": "50"})
    assert row(conn, rid)["usd_cents"] is None
    assert "usd_cents" not in json.loads(audits(conn, "update")[0]["detail"])["changed"]


def test_new_usd_rate_unknown_for_other_currency_and_report_counts_it(conn):
    rid = seed(conn, 10000, "TRY", usd=300)
    update(conn, rid, {"currency": "KZT"})                           # курсов в кэше нет: снимок пропал
    rep = reports.report(conn, "day", None, TODAY)
    assert rep["usd"]["records_without_usd"] == 1 and rep["usd"]["out_cents"] == 0


def test_currency_changed_to_parity_currency_gets_one_to_one_usd(conn):
    rid = seed(conn, 10000, "TRY", usd=300)
    update(conn, rid, {"currency": "USDT"})                          # этап 5: USD/USDT/USDC всегда 1:1
    assert row(conn, rid)["usd_cents"] == 10000
    rep = reports.report(conn, "day", None, TODAY)
    assert rep["usd"]["records_without_usd"] == 0 and rep["usd"]["out_cents"] == 10000


def test_amount_orig_is_cleared_when_amount_changes(conn):
    rid = seed(conn, 12346, "TRY", orig="123.456", source="bot")
    update(conn, rid, {"currency": "USD"})
    assert row(conn, rid)["amount_orig"] == "123.456"                 # сумма не менялась - след исходной строки остаётся
    update(conn, rid, {"amount": "100"})
    assert row(conn, rid)["amount_orig"] is None
    assert json.loads(audits(conn, "update")[-1]["detail"])["changed"]["amount_orig"] == ["123.456", None]


def test_imported_records_can_be_edited_too(conn):
    rid = seed(conn, 5000, "RUB", source="bot", category="продукты")
    update(conn, rid, {"amount": "70"})
    assert row(conn, rid)["amount_cents"] == 7000 and row(conn, rid)["source"] == "bot"


def test_negative_imported_amount_can_be_fixed(conn):
    rid = seed(conn, -30000, "TRY", usd=-700, source="bot", category="возврат")
    update(conn, rid, {"amount": "300"})
    r = row(conn, rid)
    assert r["amount_cents"] == 30000 and r["usd_cents"] is None


@pytest.mark.parametrize("payload,code", [
    ({"amount": "0"}, "amount_not_positive"), ({"amount": "-5"}, "bad_amount"), ({"amount": "abc"}, "bad_amount"),
    ({"amount": 5}, "bad_amount"), ({"amount": None}, "bad_amount"), ({"amount": "1.001"}, "too_many_decimals"),
    ({"amount": "9" * 12}, "amount_too_large"), ({"amount": "٥٠٠"}, "bad_amount"),
    ({"currency": "try1"}, "bad_currency"), ({"currency": "ЛИР"}, "bad_currency"), ({"currency": ""}, "bad_currency"),
    ({"currency": None}, "bad_currency"), ({"currency": "ſcr"}, "bad_currency"),
    ({"note": "n" * 201}, "bad_note"), ({"note": 5}, "bad_note"),
    ({"category": ""}, "missing_category"),
    ({"confirm": "outlier"}, "bad_confirm"), ({"date": TODAY}, "field_not_editable"),
])
def test_bad_values_change_nothing(conn, payload, code):
    rid = seed(conn, 10000, "TRY", usd=300, note="н")
    snap = row(conn, rid)
    with pytest.raises(RecordError) as e:
        update(conn, rid, dict({"amount": "5"}, **payload) if code == "field_not_editable" else payload)
    assert e.value.code == code
    assert row(conn, rid) == snap and audits(conn, "update") == []


def test_one_bad_field_blocks_the_whole_edit(conn):
    rid = seed(conn, 10000, "TRY", category="кафе")
    snap = row(conn, rid)
    with pytest.raises(RecordError):
        update(conn, rid, {"category": "бар", "amount": "oops"})
    assert row(conn, rid) == snap


def test_note_can_be_cleared(conn):
    rid = seed(conn, 10000, "TRY", note="что-то")
    assert update(conn, rid, {"note": ""})["changed"] == ["note"]
    assert row(conn, rid)["note"] is None


def test_cancelled_record_cannot_be_edited(conn):
    rid = seed(conn, 10000, "TRY", status="cancelled")
    with pytest.raises(RecordError) as e:
        update(conn, rid, {"amount": "5"})
    assert (e.value.status, e.value.code) == (409, "record_cancelled")


def test_unknown_record_is_404(conn):
    with pytest.raises(RecordError) as e:
        update(conn, 999999, {"amount": "5"})
    assert e.value.status == 404


def test_outlier_on_edit_needs_confirm_and_nothing_changes_before(conn):
    for _ in range(10):
        seed(conn, 10000, "KZT")
    rid = seed(conn, 10000, "KZT")
    snap = row(conn, rid)
    with pytest.raises(RecordError) as e:
        update(conn, rid, {"amount": "5000"})                         # 500000 центов против медианы 10000
    assert (e.value.status, e.value.code, e.value.extra) == (409, "confirm_required", {"warnings": ["outlier"]})
    assert row(conn, rid) == snap and audits(conn, "update") == []
    update(conn, rid, {"amount": "5000", "confirm": ["outlier"]})
    assert row(conn, rid)["amount_cents"] == 500000


def test_outlier_on_edit_by_currency_change_and_not_for_income(conn):
    for _ in range(10):
        seed(conn, 10000, "KZT")
    big = seed(conn, 800000, "TRY")
    with pytest.raises(RecordError) as e:
        update(conn, big, {"currency": "KZT"})
    assert e.value.code == "confirm_required"
    inc = seed(conn, 800000, "TRY", direction="in", method="cash", category="доход")
    update(conn, inc, {"currency": "KZT"})                            # поступления медианой не проверяются
    assert row(conn, inc)["currency"] == "KZT"


def test_edit_just_below_the_outlier_limit_needs_no_confirm(conn):
    ids = [seed(conn, 10000, "KZT") for _ in range(10)]
    update(conn, ids[0], {"amount": "999"})                           # 99900 < 10 x 10000: не выброс
    assert row(conn, ids[0])["amount_cents"] == 99900


def test_edit_does_not_touch_other_rows(conn):
    a, b = seed(conn, 100, "TRY"), seed(conn, 200, "TRY")
    update(conn, a, {"amount": "5", "currency": "USD"})
    assert row(conn, b)["amount_cents"] == 200 and row(conn, b)["currency"] == "TRY"


def test_edit_is_atomic_when_audit_insert_fails(conn):
    rid = seed(conn, 10000, "TRY")
    conn.execute("CREATE TRIGGER no_audit BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT, 'x'); END")
    snap = row(conn, rid)
    with pytest.raises(Exception):
        update(conn, rid, {"amount": "5"})
    assert row(conn, rid) == snap
    assert not conn.in_transaction


# --- HTTP ------------------------------------------------------------------------------------------------------
PRIV, PUB = new_key()
TOKEN = "T" * 8 + "abcdefghijklmnopqrstuvwxyz012345"


@pytest.fixture()
def env(tmp_path):
    db_path = str(tmp_path / "kassa-test.db")
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), "K" * 40, PUB, db_path)
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0))
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield pub.server_address[1], db_path
    for s in (pub, internal):
        s.shutdown()
        s.server_close()


def call(port, method, rest, body=None, user=OWNER, auth=True):
    h = {}
    if auth:
        h["Authorization"] = "tma " + make_init_data(PRIV, user_id=user)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
        h["Content-Length"] = str(len(data))
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, "/%s/%s" % (TOKEN, rest), body=data, headers=h)
    r = c.getresponse()
    out = r.read()
    c.close()
    try:
        return r.status, json.loads(out)
    except ValueError:
        return r.status, out


def post_record(port, **kw):
    p = {"client_id": str(uuid.uuid4()), "direction": "out", "amount": "500", "currency": "TRY", "date": TODAY, "method": "cash"}
    p.update(kw)
    return call(port, "POST", "api/records", p)


@pytest.mark.parametrize("text,expected", [("продукты", "продукты"), ("еда в кафе", "кафе"),
                                           ("оплата ЖКХ", "жильё"), ("подарки", "подарки"), ("такси", "транспорт"),
                                           ("абракадабра", "прочее"), ("", "прочее")])
def test_categorize_endpoint(env, text, expected):
    port, _ = env
    assert call(port, "POST", "api/categorize", {"text": text, "direction": "out"}) == (
        200, {"ok": True, "category": expected})


def test_categorize_endpoint_income_and_missing_text(env):
    port, _ = env
    assert call(port, "POST", "api/categorize", {"direction": "in"})[1]["category"] == "доход"
    assert call(port, "POST", "api/categorize", {"text": "возврат", "direction": "in"})[1]["category"] == "возврат"


@pytest.mark.parametrize("payload", [{"text": "x"}, {"text": "x", "direction": "sideways"}, [], "x",
                                     {"text": 5, "direction": "out"}, {"text": "я" * 401, "direction": "out"}])
def test_categorize_endpoint_rejects_bad_input(env, payload):
    port, _ = env
    s, out = call(port, "POST", "api/categorize", payload)
    assert s == 422 and out["ok"] is False


def test_categorize_needs_signature_and_allowed_user(env):
    port, _ = env
    body = {"text": "такси", "direction": "out"}
    assert call(port, "POST", "api/categorize", body, auth=False)[0] == 401
    assert call(port, "POST", "api/categorize", body, user=STRANGER)[0] == 403


def test_categorize_is_post_only_and_not_reachable_without_prefix(env):
    port, _ = env
    assert call(port, "GET", "api/categorize")[0] == 404
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/categorize", body=b"{}", headers={"Content-Type": "application/json",
                                                              "Content-Length": "2"})
    assert c.getresponse().status == 404


def test_text_never_reaches_the_server_log(env, capfd):
    port, _ = env
    call(port, "POST", "api/categorize", {"text": "СЕКРЕТНЫЙ ТЕКСТ 7654", "direction": "out"})
    post_record(port, note="СЕКРЕТНЫЙ ТЕКСТ 7654", amount="7654")
    err = capfd.readouterr().err
    assert "СЕКРЕТНЫЙ" not in err and "7654" not in err and TOKEN not in err


def test_create_over_http_without_category(env):
    port, path = env
    s, out = post_record(port, note="оплата ЖКХ")
    assert s == 200 and out["category"] == "жильё"
    c = db.connect(path, readonly=True)
    r = c.execute("SELECT category, note FROM transactions WHERE id=?", (out["id"],)).fetchone()
    assert (r["category"], r["note"]) == ("жильё", "оплата ЖКХ")


# Раньше сравнивал запись с датой «2026-10-14» против настоящих часов сервера: тест падал в любой другой день. Теперь часы
# заморожены на 14.10.2026 в начале, середине и конце суток (граница даты по местному времени (UTC+3): UTC+3).
@pytest.mark.parametrize("local_time", ["00:30", "12:00", "23:50"])
def test_edit_amount_and_currency_over_http(env, monkeypatch, local_time):
    freeze_local(monkeypatch, TODAY + " " + local_time)
    port, path = env
    rid = post_record(port, note="такси")[1]["id"]
    s, out = call(port, "POST", "api/records/%d/update" % rid, {"amount": "75,5", "currency": "USD"})
    assert s == 200 and out["changed"] == ["amount", "currency"]
    assert (out["record"]["amount_cents"], out["record"]["currency"]) == (7550, "USD")
    assert call(port, "POST", "api/records/%d/update" % rid, {"amount": "75,5", "currency": "USD"})[1]["changed"] == []
    rep = call(port, "GET", "api/report")[1]
    assert [(c["currency"], c["out_cents"]) for c in rep["currencies"]] == [("USD", 7550)]
    c = db.connect(path, readonly=True)
    assert c.execute("SELECT COUNT(*) FROM audit WHERE action='update'").fetchone()[0] == 1


def test_edit_errors_over_http(env):
    port, path = env
    rid = post_record(port)[1]["id"]
    assert call(port, "POST", "api/records/%d/update" % rid, {"amount": "0"}) == (
        422, {"ok": False, "error": "amount_not_positive"})
    assert call(port, "POST", "api/records/%d/update" % rid, {"date": TODAY})[1]["error"] == "field_not_editable"
    c = db.connect(path, readonly=True)
    assert c.execute("SELECT amount_cents FROM transactions WHERE id=?", (rid,)).fetchone()[0] == 50000


def test_edit_outlier_confirm_over_http(env):
    port, path = env
    c = db.connect(path)
    for i in range(10):
        c.execute("INSERT INTO transactions(idem_key, direction, amount_cents, currency, category, occurred_on, "
                  "created_at, source, via, actor_id) VALUES (?, 'out', 10000, 'KZT', 'x', ?, '2026-10-01T00:00:00Z', "
                  "'kassa', 'webapp', '1')", ("seed%d" % i, db.today_local()))
    c.close()
    rid = post_record(port, currency="KZT", amount="100")[1]["id"]
    s, out = call(port, "POST", "api/records/%d/update" % rid, {"amount": "99999"})
    assert (s, out["error"], out["warnings"]) == (409, "confirm_required", ["outlier"])
    s, out = call(port, "POST", "api/records/%d/update" % rid, {"amount": "99999", "confirm": ["outlier"]})
    assert s == 200 and out["record"]["amount_cents"] == 9999900
