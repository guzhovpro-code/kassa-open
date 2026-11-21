"""Этап 3 и валюты по HTTP: отчёты, последние, удаление/возврат/правка, справочник валют в /api/meta."""
import http.client
import json
import threading
import uuid
from datetime import date, timedelta

import pytest

from helpers import BOT_ID, OWNER, STRANGER, make_init_data, new_key
from kassa_api import currencies, db
from kassa_api.server import Config, make_servers

PRIV, PUB = new_key()
TOKEN = "S" * 8 + "abcdefghijklmnopqrstuvwxyz012345"
TODAY = db.today_local()


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


def call(port, method, rest, body=None, user=OWNER, raw=None, headers=None, auth=True):
    h = dict(headers or {})
    if auth:
        h["Authorization"] = "tma " + make_init_data(PRIV, user_id=user)
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    if data is not None:
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


def rec(**kw):
    p = {"client_id": str(uuid.uuid4()), "direction": "out", "amount": "500", "currency": "TRY", "category": "кафе",
         "date": TODAY, "method": "cash"}
    p.update(kw)
    return p


def add(port, **kw):
    s, r = call(port, "POST", "api/records", rec(**kw))
    assert s == 200, r
    return r["id"]


def status_of(path, rid):
    c = db.connect(path, readonly=True)
    return c.execute("SELECT status FROM transactions WHERE id=?", (rid,)).fetchone()[0]


# --- авторизация -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("method,rest,body", [
    ("GET", "api/report", None), ("GET", "api/report?period=month", None), ("GET", "api/recent", None),
    ("POST", "api/records/1/cancel", {}), ("POST", "api/records/1/restore", {}),
    ("POST", "api/records/1/update", {"category": "бар"}),
])
def test_new_endpoints_need_valid_signature_and_allowed_user(env, method, rest, body):
    port, path = env
    rid = add(port)
    assert call(port, method, rest.replace("/1/", "/%d/" % rid), body, auth=False)[0] == 401
    assert call(port, method, rest.replace("/1/", "/%d/" % rid), body, headers={"Authorization": "tma junk"},
                auth=False)[0] == 401
    assert call(port, method, rest.replace("/1/", "/%d/" % rid), body, user=STRANGER)[0] == 403
    assert status_of(path, rid) == "active"


def test_actions_not_reachable_without_secret_prefix(env):
    port, path = env
    rid = add(port)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/records/%d/cancel" % rid, body=b"{}", headers={
        "Authorization": "tma " + make_init_data(PRIV), "Content-Type": "application/json", "Content-Length": "2"})
    assert c.getresponse().status == 404
    assert status_of(path, rid) == "active"


# --- отчёты ----------------------------------------------------------------------------------------------------
def test_report_day_and_month_over_http(env):
    port, _ = env
    add(port, amount="1234,50", note="для теста")
    add(port, amount="10", currency="USD", direction="in", category="доход", method="okx")
    s, day = call(port, "GET", "api/report")
    assert s == 200 and day["period"] == "day" and day["from"] == TODAY and day["count"] == 2
    cur = {c["currency"]: c for c in day["currencies"]}
    assert (cur["TRY"]["out_cents"], cur["USD"]["in_cents"]) == (123450, 1000)
    assert {r["amount_cents"] for r in day["records"]} == {123450, 1000}
    s, month = call(port, "GET", "api/report?period=month")
    assert s == 200 and month["period"] == "month" and month["from"] == TODAY[:7] + "-01"
    assert [g["name"] for g in month["by_method"]] == ["Биржа OKX"]
    s, month2 = call(port, "GET", "api/report?period=month&month=" + TODAY[:7])
    assert month2 == month


@pytest.mark.parametrize("query", [
    "period=year", "period=day&date=2999-01-01", "period=month&month=2999-01", "period=day&date=x",
    "period=month&month=2026-13", "period=day&date=%0a", "period=",
])
def test_report_bad_period_is_422(env, query):
    port, _ = env
    s, out = call(port, "GET", "api/report?" + query)
    assert (s, out["error"]) == (422, "bad_period")


def test_report_ignores_unknown_and_repeated_params(env):
    port, _ = env
    assert call(port, "GET", "api/report?foo=bar&period=day&period=month")[0] == 200
    assert call(port, "GET", "api/report?a=1&b=2&c=3&d=4&e=5&f=6&period=day")[0] in (200, 400, 422)


def test_recent_and_filter_over_http(env):
    port, _ = env
    a = add(port)
    gone = add(port, amount="77")
    call(port, "POST", "api/records/%d/cancel" % gone, {})
    s, out = call(port, "GET", "api/recent")
    assert s == 200 and [(r["id"], r["status"]) for r in out["records"]] == [(gone, "cancelled"), (a, "active")]
    s, out = call(port, "GET", "api/recent?filter=no_method")
    assert s == 200 and out["records"] == [] and out["no_method_total"] == 0
    assert call(port, "GET", "api/recent?filter=evil")[1]["error"] == "bad_filter"


# --- удаление, возврат, правка ---------------------------------------------------------------------------------
def test_cancel_restore_over_http(env):
    port, path = env
    rid = add(port, amount="321")
    s, out = call(port, "POST", "api/records/%d/cancel" % rid, {})
    assert s == 200 and out["status"] == "cancelled" and out["changed"] is True and out["record"]["status"] == "cancelled"
    assert call(port, "GET", "api/report")[1]["count"] == 0
    c = db.connect(path, readonly=True)
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM v_legacy").fetchone()[0] == 0
    s, out = call(port, "POST", "api/records/%d/restore" % rid, {})
    assert s == 200 and out["status"] == "active" and out["record"]["status"] == "active"
    assert call(port, "GET", "api/report")[1]["count"] == 1
    assert c.execute("SELECT COUNT(*) FROM v_legacy").fetchone()[0] == 1


def test_update_over_http(env):
    port, path = env
    rid = add(port, direction="in", category="доход", method="monzo", currency="USD", amount="40")
    s, out = call(port, "POST", "api/records/%d/update" % rid, {"category": "возврат", "method": "wise"})
    assert s == 200 and out["changed"] == ["category", "method"]
    assert (out["record"]["category"], out["record"]["method"], out["record"]["amount_cents"],
            out["record"]["currency"]) == ("возврат", "wise", 4000, "USD")
    s, out = call(port, "POST", "api/records/%d/update" % rid, {"date": "2026-10-01"})
    assert (s, out["error"]) == (422, "field_not_editable")
    s, out = call(port, "POST", "api/records/%d/update" % rid, {"method": None})
    assert (s, out["error"]) == (422, "method_required")
    call(port, "POST", "api/records/%d/cancel" % rid, {})
    s, out = call(port, "POST", "api/records/%d/update" % rid, {"category": "доход"})
    assert (s, out["error"]) == (409, "record_cancelled")


@pytest.mark.parametrize("rest", ["api/records/999999/cancel", "api/records/0/restore", "api/records/999999/update"])
def test_unknown_record_is_404_json(env, rest):
    port, _ = env
    s, out = call(port, "POST", rest, {"category": "бар"})
    assert (s, out["error"]) == (404, "not_found")


@pytest.mark.parametrize("rest", [
    "api/records/abc/cancel", "api/records/-1/cancel", "api/records/1.0/cancel", "api/records/1/delete",
    "api/records/1234567890123/cancel", "api/records//cancel", "api/records/1/cancel/x", "api/records/%D9%A1/cancel",
    "api/records/1/", "api/records/1",
])
def test_malformed_action_paths_are_404(env, rest):
    port, path = env
    rid = add(port)
    assert call(port, "POST", rest, {})[0] == 404
    assert status_of(path, rid) == "active"


def test_get_on_action_paths_is_404_and_changes_nothing(env):
    port, path = env
    rid = add(port)
    assert call(port, "GET", "api/records/%d/cancel" % rid)[0] == 404
    assert status_of(path, rid) == "active"


def test_action_requires_json_content_type(env):
    port, path = env
    rid = add(port)
    s, out = call(port, "POST", "api/records/%d/cancel" % rid, raw=b"{}", headers={"Content-Type": "text/plain"})
    assert s == 415 and status_of(path, rid) == "active"
    s, out = call(port, "POST", "api/records/%d/cancel" % rid, raw=b"{bad", headers={"Content-Type": "application/json"})
    assert s == 400 and status_of(path, rid) == "active"


def test_server_log_for_actions_has_no_amounts_notes_or_ids(env, capfd):
    port, _ = env
    rid = add(port, amount="7654,32", note="СЕКРЕТНАЯ-ЗАМЕТКА")
    call(port, "GET", "api/report")
    call(port, "GET", "api/recent")
    call(port, "POST", "api/records/%d/update" % rid, {"category": "бар"})
    call(port, "POST", "api/records/%d/cancel" % rid, {})
    call(port, "POST", "api/records/%d/restore" % rid, {})
    err = capfd.readouterr().err
    assert "765432" not in err and "7654" not in err and "СЕКРЕТНАЯ" not in err and "бар" not in err
    assert "cancel ok" in err and "restore ok" in err and "update ok" in err
    assert TOKEN not in err


# --- валюты ----------------------------------------------------------------------------------------------------
def test_meta_currencies_main_order_and_recent_only(env):
    port, _ = env
    s, meta = call(port, "GET", "api/meta")
    assert meta["currencies"] == {"main": ["TRY", "USDT", "USD", "RUB"], "second": ["EUR"], "recent": []}
    assert len(json.dumps(meta)) < 6000                                  # справочник не едет с каждым /api/meta


def test_full_catalog_is_served_once_as_utf8_json(env):
    port, _ = env
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/%s/currencies.json" % TOKEN)
    r = c.getresponse()
    raw = r.read()
    assert r.status == 200 and r.getheader("Content-Type") == "application/json; charset=utf-8"
    assert len(raw) < 30000
    cat = json.loads(raw.decode("utf-8"))
    codes = [x["code"] for x in cat]
    assert len(codes) == len(set(codes)) >= 176
    assert {"TRY", "USDT", "USDC", "BTC", "ETH", "KZT", "AED", "GEL", "SCR", "JPY", "CHF", "XAU"} <= set(codes)
    assert "XXX" not in codes and "XTS" not in codes
    assert all(x["name"] and x["k"] == x["k"].lower() for x in cat)
    names = {x["code"]: x["name"] for x in cat}
    assert names["TRY"] == "Турецкая лира" and names["KZT"] == "Казахстанский тенге" and names["USD"] == "Доллар США"
    c.request("GET", "/currencies.json")                                 # без секретного префикса - 404
    assert c.getresponse().status == 404
    c.close()


def test_catalog_has_every_active_iso_code():
    """Контроль полноты: в справочнике все 176 кодов официального списка SIX 2026-09-17 без XXX и XTS."""
    iso = ("AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF "
           "CHE CHF CHW CLF CLP CNY COP COU CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP "
           "GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT "
           "LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR "
           "NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN "
           "SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAD "
           "XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU XUA YER ZAR ZMW ZWG").split()
    assert len(iso) == len(set(iso)) == 176
    assert set(iso) <= currencies.CODES and currencies.CODES - set(iso) == {"USDT", "USDC", "BTC", "ETH"}


def test_recent_currencies_by_frequency_excluding_main_and_old(env):
    port, path = env
    add(port, currency="KZT", amount="100")
    add(port, currency="KZT", amount="200")
    add(port, currency="AED", amount="300")
    add(port, currency="USD", amount="400")                                  # основная: не в «недавних»
    old = (date.fromisoformat(TODAY) - timedelta(days=61)).isoformat()
    add(port, currency="GEL", amount="500", date=old)                        # старше 60 дней
    gone = add(port, currency="CNY", amount="600")
    call(port, "POST", "api/records/%d/cancel" % gone, {})                   # удалена
    inside = (date.fromisoformat(TODAY) - timedelta(days=60)).isoformat()
    add(port, currency="THB", amount="700", date=inside)                     # ровно 60 дней: входит
    meta = call(port, "GET", "api/meta")[1]
    assert meta["currencies"]["recent"] == ["KZT", "AED", "THB"]


def test_search_choice_is_remembered_as_last_currency_per_direction(env):
    port, _ = env
    add(port, currency="KZT")
    add(port, direction="in", category="доход", method="cash", currency="GEL", amount="9")
    meta = call(port, "GET", "api/meta")[1]
    assert meta["last_currency"] == {"out": "KZT", "in": "GEL"}
    add(port, currency="JPY", amount="11")
    assert call(port, "GET", "api/meta")[1]["last_currency"]["out"] == "JPY"


# --- мусор в строке запроса и страховка от непредвиденных ошибок -----------------------------------------------
def test_too_many_query_fields_is_400_not_dropped_connection(env):
    port, _ = env
    s, out = call(port, "GET", "api/report?" + "&".join("p%d=1" % i for i in range(20)))
    assert (s, out["error"]) == (400, "bad_query")
    s, out = call(port, "GET", "api/recent?" + "&".join("p%d=1" % i for i in range(20)))
    assert (s, out["error"]) == (400, "bad_query")
    assert call(port, "GET", "api/report")[0] == 200


def test_unexpected_error_gives_500_json_and_server_lives(env, monkeypatch, capfd):
    port, _ = env
    from kassa_api import reports

    def boom(*a, **kw):
        raise ZeroDivisionError("secret 12345")
    monkeypatch.setattr(reports, "report", boom)
    s, out = call(port, "GET", "api/report")
    assert (s, out) == (500, {"ok": False, "error": "server_error"})
    assert "unhandled error: ZeroDivisionError" in capfd.readouterr().err
    monkeypatch.undo()
    assert call(port, "GET", "api/report")[0] == 200
