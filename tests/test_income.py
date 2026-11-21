"""Поступления только на чтение: reports.income и GET /internal/income (токен бота). Данные и имена выдуманные."""
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helpers import BOT_ID, OWNER, freeze_local  # noqa: E402
from kassa_api import db, records, reports  # noqa: E402
from kassa_api.records import RecordError  # noqa: E402
from kassa_api.server import Config, make_servers  # noqa: E402
from test_api import INTERNAL, PUB, TOKEN, req  # noqa: E402

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)      # когда внесены записи (даты вперёд внести нельзя)
TODAY = "2026-10-21"          # «сегодня» для отчёта: среда, неделя 19-25.10
_n = [0]


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


def add(conn, day=TODAY, direction="in", amount="100", currency="USDT", method="bybit", **kw):
    _n[0] += 1
    body = {"direction": direction, "amount": amount, "currency": currency, "date": day, "method": method,
            "client_id": "inc-%016d" % _n[0], "confirm": ["possible_duplicate", "outlier"]}
    if direction == "out":
        body.update(method="cash", category="прочее", note="секретная заметка о расходе")
    body.update(kw)
    return records.create_record(conn, OWNER, body, now=NOW, fx_rates={})["id"]


def fill(conn):
    """Неделя 19-25.10: B2B 100 + 50 (два плательщика), B2C 30, без пометки 20, без курса (TRY) 1 запись; вокруг недели
    и расходы, которых в ответе быть не должно."""
    add(conn, "2026-10-19", amount="100", payer="Клиент Один", segment="b2b")
    add(conn, "2026-10-21", amount="50", payer="Клиент Два", segment="b2b")
    add(conn, "2026-10-25", amount="30", payer="Клиент Один", segment="b2c")
    add(conn, "2026-10-22", amount="20")
    add(conn, "2026-10-23", amount="500", currency="TRY", method="tinkoff", note="кто-то из TRY")
    add(conn, "2026-10-18", amount="7777")                       # воскресенье прошлой недели
    add(conn, "2026-10-26", amount="8888")                       # понедельник следующей недели
    add(conn, "2026-10-20", direction="out", amount="999")
    add(conn, "2026-10-21", direction="out", amount="11", currency="USD")


def snap(path):
    c = db.connect(path)
    try:
        return {t: [tuple(r) for r in c.execute("SELECT * FROM %s ORDER BY 1" % t)] for t in ("transactions", "audit", "outbox")}
    finally:
        c.close()


# --- reports.income --------------------------------------------------------------------------------------------------
def test_week_is_monday_to_sunday_and_only_income(conn):
    fill(conn)
    r = reports.income(conn, "week", None, TODAY)
    assert (r["from"], r["to"], r["partial"]) == ("2026-10-19", "2026-10-25", True)
    inc = r["income"]
    assert inc["usd_cents"] == (100 + 50 + 30 + 20) * 100 and inc["count"] == 5 and inc["without_usd"] == 1
    seg = r["by_segment"]
    assert seg["b2b"]["usd_cents"] == 15000 and seg["b2b"]["count"] == 2
    assert seg["b2c"]["usd_cents"] == 3000 and seg["b2c"]["count"] == 1
    assert seg["unmarked"]["usd_cents"] == 2000 and seg["unmarked"]["count"] == 2 and seg["unmarked"]["without_usd"] == 1
    assert seg["b2b"]["usd_cents"] + seg["b2c"]["usd_cents"] + seg["unmarked"]["usd_cents"] == inc["usd_cents"]


def test_by_payer_sums_and_sorts(conn):
    fill(conn)
    r = reports.income(conn, "week", None, TODAY)
    assert r["by_payer"] == [{"payer": "Клиент Один", "usd_cents": 13000, "count": 2, "without_usd": 0},
                             {"payer": "Клиент Два", "usd_cents": 5000, "count": 1, "without_usd": 0}]
    assert r["payers_omitted"] == 0


def test_response_has_no_expenses_notes_or_records(conn):
    fill(conn)
    text = json.dumps(reports.income(conn, "week", None, TODAY), ensure_ascii=False)
    for forbidden in ("секретная заметка", "кто-то из TRY", "прочее", "records", "note", "category", "out_cents", "7777", "8888", "99900"):
        assert forbidden not in text
    assert set(reports.income(conn, "week", None, TODAY)) == {"ok", "period", "from", "to", "partial", "closed_month", "income",
                                                              "by_segment", "by_payer", "payers_omitted"}


def test_cancelled_records_are_not_counted(conn):
    rid = add(conn, "2026-10-21", amount="40")
    before = reports.income(conn, "week", None, TODAY)["income"]["usd_cents"]
    records.cancel_record(conn, OWNER, rid, now=NOW)
    assert reports.income(conn, "week", None, TODAY)["income"]["usd_cents"] == before - 4000


def test_week_by_date_and_past_week_not_partial(conn):
    fill(conn)
    r = reports.income(conn, "week", "2026-10-18", TODAY)             # воскресенье: неделя 12-18.10
    assert (r["from"], r["to"], r["partial"]) == ("2026-10-12", "2026-10-18", False)
    assert r["income"]["usd_cents"] == 777700 and r["income"]["count"] == 1


def test_future_and_garbage_are_422(conn):
    for period, key in (("week", "2026-10-26"), ("week", "2026-13-01"), ("week", "garbage"), ("month", "2026-11"),
                        ("month", "2026-11-01"), ("day", None), ("year", None), ("month", "2026-1"), ("week", "1999-01-04")):
        with pytest.raises(RecordError) as e:
            reports.income(conn, period, key, TODAY)
        assert (e.value.status, e.value.code) == (422, "bad_period")


def test_date_later_than_today_inside_current_period_is_allowed(conn):
    fill(conn)
    base = reports.income(conn, "week", None, TODAY)
    for key in ("2026-10-22", "2026-10-25"):                       # позже сегодня (среда), но в этой же неделе
        r = reports.income(conn, "week", key, TODAY)
        assert (r["from"], r["to"], r["partial"]) == ("2026-10-19", "2026-10-25", True)
        assert r["income"] == base["income"]
    m = reports.income(conn, "month", "2026-10-31", TODAY)
    assert (m["from"], m["to"]) == ("2026-10-01", "2026-10-31")
    assert m["income"] == reports.income(conn, "month", None, TODAY)["income"]
    with pytest.raises(RecordError):
        reports.income(conn, "week", "2026-10-26", TODAY)           # следующая неделя целиком в будущем


def test_closed_month_flag(conn):
    fill(conn)
    assert reports.income(conn, "week", None, TODAY)["closed_month"] is False
    assert reports.income(conn, "month", None, TODAY)["closed_month"] is False
    conn.execute("INSERT INTO closed_months(month, closed_at, source_name, source_sha, rates, rates_source, expected, rows_in, rows_out) "
                 "VALUES ('2026-09','2026-10-14T23:00:00Z','x','y','{}','test','{}',0,0)")
    assert reports.income(conn, "month", "2026-09", TODAY)["closed_month"] is True
    assert reports.income(conn, "week", "2026-09-30", TODAY)["closed_month"] is True       # неделя 28.09-04.10 задевает сентябрь
    assert reports.income(conn, "month", "2026-10", TODAY)["closed_month"] is False


def test_month_matches_report_month(conn):
    fill(conn)
    rep = reports.report(conn, "month", None, TODAY)
    r = reports.income(conn, "month", None, TODAY)
    assert (r["from"], r["to"]) == ("2026-10-01", "2026-10-31")
    assert r["income"]["usd_cents"] == rep["usd"]["in_cents"]
    by_seg = {g["name"]: g for g in rep["by_segment"]}
    assert r["by_segment"]["b2b"]["usd_cents"] == by_seg["B2B"]["usd_cents"]
    assert r["by_segment"]["b2c"]["usd_cents"] == by_seg["B2C"]["usd_cents"]
    assert r["by_segment"]["unmarked"]["usd_cents"] == by_seg["Без пометки"]["usd_cents"]
    assert r["income"]["count"] == sum(g["count"] for g in rep["by_segment"])
    assert reports.income(conn, "month", "2026-10-03", TODAY)["income"] == r["income"]       # день внутри месяца
    assert reports.income(conn, "month", "2026-10", TODAY)["income"] == r["income"]


def test_closed_month_uses_the_same_rows_as_report(conn):
    """Закрытый месяц: записи импорта перекрыты (closed_by), считаются строки закрытия - как в reports.report."""
    conn.execute("INSERT INTO transactions(idem_key, direction, amount_cents, currency, usd_cents, method, occurred_on, "
                 "created_at, source, via, actor_id) VALUES ('close:2026-09:1','in',90000,'USD',90000,'bybit','2026-09-30',"
                 "'2026-10-14T23:00:00Z','month-close','close-import','month-close')")
    conn.execute("INSERT INTO transactions(idem_key, direction, amount_cents, currency, usd_cents, method, occurred_on, "
                 "created_at, source, via, actor_id, closed_by) VALUES ('fb:1','in',99999,'USD',99999,'bybit','2026-09-10',"
                 "'2026-09-10T10:00:00Z','finbot','import','finbot','2026-09')")
    rep = reports.report(conn, "month", "2026-09", TODAY)
    r = reports.income(conn, "month", "2026-09", TODAY)
    assert rep["usd"]["in_cents"] == 90000 == r["income"]["usd_cents"] and r["income"]["count"] == 1
    assert r["by_segment"]["unmarked"]["usd_cents"] == 90000 and r["by_payer"] == [] and r["partial"] is False




def test_payers_list_is_capped(conn):
    for i in range(23):
        add(conn, "2026-10-20", amount=str(10 + i), payer="Плательщик %02d" % i)
    r = reports.income(conn, "week", None, TODAY)
    assert len(r["by_payer"]) == 20 and r["payers_omitted"] == 3 and r["income"]["count"] == 23
    assert r["by_payer"][0]["usd_cents"] >= r["by_payer"][-1]["usd_cents"]


# --- GET /internal/income -------------------------------------------------------------------------------------------------
@pytest.fixture()
def srv(tmp_path, monkeypatch):
    freeze_local(monkeypatch, "2026-10-21 12:00")
    dbp = str(tmp_path / "kassa-test.db")
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), INTERNAL, PUB, dbp)
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0))
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield pub.server_address[1], internal.server_address[1], dbp
    for s in (pub, internal):
        s.shutdown()
        s.server_close()


def iget(srv, path, token=INTERNAL):
    return req(srv[1], "GET", path, headers={"Authorization": "Bearer " + token} if token else {})


def test_endpoint_requires_the_internal_token(srv):
    assert iget(srv, "/internal/income?period=week", token=None)[0] == 401
    assert iget(srv, "/internal/income?period=week", token="X" * 40)[0] == 401
    assert iget(srv, "/internal/income?period=week")[0] == 200


def test_endpoint_week_by_istanbul_calendar(srv, monkeypatch):
    # 26.10 01:00 в местном часовом поясе (UTC+3) = 25.10 22:00 UTC: по UTC ещё прошлая неделя, по местному времени (UTC+3) уже новая (26.10-01.11)
    freeze_local(monkeypatch, "2026-10-26 01:00")
    status, body = iget(srv, "/internal/income?period=week")
    assert status == 200 and (body["from"], body["to"]) == ("2026-10-26", "2026-11-01")
    freeze_local(monkeypatch, "2026-10-25 23:30")
    status, body = iget(srv, "/internal/income?period=week")
    assert (body["from"], body["to"]) == ("2026-10-19", "2026-10-25")


def test_endpoint_has_no_expenses_notes_or_records(srv):
    c = db.connect(srv[2])
    fill(c)
    c.close()
    status, body = iget(srv, "/internal/income?period=week&date=2026-10-18")
    assert status == 200
    status, body = iget(srv, "/internal/income?period=month&date=2026-10-15")
    assert status == 200 and body["income"]["count"] >= 5
    text = json.dumps(body, ensure_ascii=False)
    for forbidden in ("секретная заметка", "кто-то из TRY", "records", "note", "category", "out_cents"):
        assert forbidden not in text


def test_endpoint_is_read_only_and_token_cannot_write(srv):
    c = db.connect(srv[2])
    fill(c)
    c.close()
    before = snap(srv[2])
    for q in ("/internal/income?period=week&date=2026-10-20", "/internal/income?period=month", "/internal/income"):
        assert iget(srv, q)[0] == 200
    assert snap(srv[2]) == before
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert req(srv[1], method, "/internal/income?period=week", body={"a": 1},
                   headers={"Authorization": "Bearer " + INTERNAL})[0] in (404, 405)
    # прочие права токена не расширены: запись и отчёты с расходами через внутренний порт недоступны
    for path in ("/internal/report?period=day", "/internal/recent", "/internal/records", "/internal/inbox"):
        assert iget(srv, path)[0] == 404
    assert snap(srv[2]) == before


def test_endpoint_bad_period_and_date(srv):
    for q in ("period=day", "period=year", "period=week&date=2099-01-01", "period=week&date=oops", "period=month&date=2026-1"):
        status, body = iget(srv, "/internal/income?" + q)
        assert (status, body["error"]) == (422, "bad_period"), q
    assert iget(srv, "/internal/income?period=week&a=1&b=2&c=3&d=4&e=5&f=6")[0] == 400


def test_endpoint_not_on_public_port(srv):
    assert req(srv[0], "GET", "/%s/internal/income?period=week" % TOKEN, headers={"Authorization": "Bearer " + INTERNAL})[0] == 404
    assert req(srv[0], "GET", "/internal/income?period=week", headers={"Authorization": "Bearer " + INTERNAL})[0] == 404


def test_endpoint_log_has_no_amounts(srv, capsys):
    c = db.connect(srv[2])
    fill(c)
    c.close()
    iget(srv, "/internal/income?period=week")
    err = capsys.readouterr().err
    assert "income ok" in err and "Клиент" not in err and "10000" not in err


















