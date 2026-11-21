"""Отчёт за произвольный день или период: api/report?period=range&from=YYYY-MM-DD&to=YYYY-MM-DD (включительно)."""
from datetime import date, timedelta
from urllib.parse import quote

import pytest

from helpers import freeze_local
from kassa_api import reports
from kassa_api.records import RecordError
from test_api3 import add as api_add, call, env, TODAY as API_TODAY  # noqa: F401
from test_reports import TODAY, add, by_cur, conn, path  # noqa: F401


def test_range_bounds_inclusive_both_ends(conn):
    add(conn, "out", 100, day="2026-10-04")
    add(conn, "out", 200, day="2026-10-05")
    add(conn, "out", 400, day="2026-10-10")
    add(conn, "out", 800, day="2026-10-11")
    rep = reports.report(conn, "range", "2026-10-05", TODAY, "2026-10-10")
    assert (rep["period"], rep["from"], rep["to"], rep["count"]) == ("range", "2026-10-05", "2026-10-10", 2)
    assert by_cur(rep)["TRY"]["out_cents"] == 600


def test_single_day_is_from_equals_to(conn):
    add(conn, "out", 300, day="2026-10-07")
    add(conn, "out", 50, day="2026-10-08")
    rep = reports.report(conn, "range", "2026-10-07", TODAY, "2026-10-07")
    assert rep["count"] == 1 and by_cur(rep)["TRY"]["out_cents"] == 300


def test_range_has_month_shape_and_same_numbers_as_month(conn):
    add(conn, "out", 10000, usd=300, category="кафе")
    add(conn, "in", 50000, "USDT", usd=50000, method="bybit", category="доход")
    add(conn, "in", 7000, "TRY", usd=None, method="cash", category="доход")           # без курса
    add(conn, "out", 900, status="cancelled")
    month = reports.report(conn, "month", "2026-10", TODAY)
    rng = reports.report(conn, "range", "2026-10-01", TODAY, TODAY)
    for k in ("count", "currencies", "usd", "by_category", "by_method", "by_segment", "exchanges"):
        assert rng[k] == month[k], k
    assert rng["usd"]["records_without_usd"] == 1
    assert "closed" not in rng and "records" not in rng


def test_empty_range(conn):
    add(conn, "out", 100, day="2026-09-01")
    rep = reports.report(conn, "range", "2026-10-01", TODAY, "2026-10-05")
    assert rep["count"] == 0 and rep["currencies"] == [] and rep["by_category"] == []
    assert rep["usd"] == {"out_cents": 0, "in_cents": 0, "net_cents": 0, "records_without_usd": 0}


def test_range_counts_exchanges(conn):
    assert reports.report(conn, "range", "2026-10-01", TODAY, "2026-10-05")["exchanges"] == 0


@pytest.mark.parametrize("first,last,code", [
    ("2026-10-10", "2026-10-05", "bad_period"),               # from > to
    ("2026-10-01", "2026-10-15", "bad_period"),               # to в будущем
    ("2026-10-15", "2026-10-15", "bad_period"),               # день в будущем
    ("2026-13-01", "2026-10-05", "bad_period"), ("2026-02-30", "2026-10-05", "bad_period"),
    ("x", "2026-10-05", "bad_period"), (None, "2026-10-05", "bad_period"), ("2026-10-01", None, "bad_period"),
    ("2026-10-01\n", "2026-10-05", "bad_period"), ("1999-12-31", "2026-10-05", "bad_period"),
    ("2025-09-09", "2026-10-14", "period_too_long"),          # 400 дней + 1
])
def test_range_bad_input(conn, first, last, code):
    with pytest.raises(RecordError) as exc:
        reports.report(conn, "range", first, TODAY, last)
    assert (exc.value.status, exc.value.code) == (422, code)


def test_range_limit_is_400_days_inclusive():
    last = date(2026, 10, 14)
    ok = (last - timedelta(days=399)).isoformat()
    assert reports.period_bounds("range", ok, TODAY, TODAY) == (ok, TODAY)
    with pytest.raises(RecordError):
        reports.period_bounds("range", (last - timedelta(days=400)).isoformat(), TODAY, TODAY)


# --- по HTTP ----------------------------------------------------------------------------------------------------
def test_http_range_matches_month_and_needs_auth(env):
    port, _ = env
    api_add(port, amount="1234,50", note="для теста")
    api_add(port, amount="10", currency="USD", direction="in", category="доход", method="bybit")
    first = API_TODAY[:7] + "-01"
    s, month = call(port, "GET", "api/report?period=month")
    s2, rng = call(port, "GET", "api/report?period=range&from=%s&to=%s" % (first, API_TODAY))
    assert (s, s2) == (200, 200) and rng["period"] == "range" and (rng["from"], rng["to"]) == (first, API_TODAY)
    for k in ("count", "currencies", "usd", "by_category", "by_method", "by_segment"):
        assert rng[k] == month[k]
    s, day = call(port, "GET", "api/report?period=range&from=%s&to=%s" % (API_TODAY, API_TODAY))
    assert s == 200 and day["count"] == 2
    assert call(port, "GET", "api/report?period=range&from=%s&to=%s" % (first, API_TODAY), auth=False)[0] == 401


@pytest.mark.parametrize("query,code", [
    ("period=range", "bad_period"), ("period=range&from=2026-10-01", "bad_period"),
    ("period=range&to=2026-10-01", "bad_period"), ("period=range&from=2026-10-09&to=2026-10-01", "bad_period"),
    ("period=range&from=x&to=y", "bad_period"), ("period=range&from=2999-01-01&to=2999-01-02", "bad_period"),
    ("period=range&from=2020-01-01&to=2026-01-01", "period_too_long"),
    ("period=range&from=%s&to=%s" % (quote("2026-10-01\n"), "2026-10-05"), "bad_period"),
])
def test_http_range_bad_input_is_422(env, query, code):
    port, _ = env
    s, out = call(port, "GET", "api/report?" + query)
    assert (s, out["error"]) == (422, code)


def test_http_range_uses_kassa_tz_day(env, monkeypatch):
    port, _ = env
    # 22:30 UTC 13.10 = 01:30 14.10 по UTC+3: «сегодня» уже 14.10, а 15.10 ещё будущее
    freeze_local(monkeypatch, "2026-10-14 01:30")
    assert call(port, "GET", "api/report?period=range&from=2026-10-13&to=2026-10-14")[0] == 200
    s, out = call(port, "GET", "api/report?period=range&from=2026-10-13&to=2026-10-15")
    assert (s, out["error"]) == (422, "bad_period")
