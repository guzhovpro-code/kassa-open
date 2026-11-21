"""Этап 7 (без браузера): данные для диаграмм. Расходы по категориям (месяц и день) и динамика по месяцам считаются
тем же кодом, что и табличные итоги отчёта, поэтому не могут разойтись с ними."""
import pytest

from kassa_api import db, reports
from test_api3 import PUB, TOKEN, call, env  # noqa: F401
from test_reports import add, conn, path  # noqa: F401
TODAY = "2026-10-14"


def cat_sum(rep):
    return sum(g["usd_cents"] for g in rep["by_category"])




def test_day_report_has_expense_categories_with_sum_equal_to_total(conn):
    add(conn, "out", 1000, "USD", usd=1000, category="кафе")
    add(conn, "out", 4000, "TRY", usd=100, category="кафе")
    add(conn, "out", 5000, "TRY", usd=125, category="такси")
    add(conn, "out", 9000, "TRY", usd=None, category="бар")                    # без курса: в сумму не входит
    add(conn, "in", 70000, "USD", usd=70000, category="доход", method="bybit")  # поступления в расходы не попадают
    add(conn, "out", 2000, "USD", usd=2000, category="жильё", status="cancelled")      # удалённая не считается
    rep = reports.report(conn, "day", None, "2026-10-14")
    names = {g["name"]: g for g in rep["by_category"]}
    assert set(names) == {"кафе", "такси", "бар"}
    assert names["кафе"]["usd_cents"] == 1100 and names["кафе"]["count"] == 2
    assert names["бар"]["usd_cents"] == 0 and names["бар"]["without_usd"] == 1
    assert [g["name"] for g in rep["by_category"]] == ["кафе", "такси", "бар"]            # по убыванию, затем по числу
    assert cat_sum(rep) == rep["usd"]["out_cents"] == 1225




def test_trend_open_month_counts_only_active_records(conn):
    add(conn, "out", 1000, "USD", usd=1000, day="2026-10-02")
    add(conn, "in", 5000, "USD", usd=5000, day="2026-10-03", category="доход", method="bybit")
    add(conn, "out", 7000, "USD", usd=7000, day="2026-10-04", status="cancelled")
    add(conn, "out", 300, "BTC", usd=None, day="2026-10-05")
    tr = reports.trend(conn, "2026-10-14")
    last = tr["months"][-1]
    assert last["month"] == "2026-10" and not last["closed"]
    assert (last["in_usd_cents"], last["out_usd_cents"], last["net_usd_cents"]) == (5000, 1000, 4000)
    assert last["records_without_usd"] == 1 and last["count"] == 3
    assert all(m["count"] == 0 and m["net_usd_cents"] == 0 for m in tr["months"][:-1])          # пустые месяцы нули


def test_trend_crosses_the_year_boundary(conn):
    tr = reports.trend(conn, "2027-02-10")
    assert [m["month"] for m in tr["months"]] == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10",
                                                  "2026-11", "2026-12", "2027-01", "2027-02"]


# --- HTTP ------------------------------------------------------------------------------------------------------------
def test_trend_endpoint_needs_auth_and_returns_months(env):
    port, dbp = env
    assert call(port, "GET", "api/report?period=trend", auth=False)[0] == 401
    s, b = call(port, "GET", "api/report?period=trend")
    assert s == 200 and b["ok"] and b["period"] == "trend"
    assert b["months"][0]["month"] == "2026-05" and b["months"][-1]["month"] == db.today_local()[:7]
    s, b = call(port, "GET", "api/report?period=trend&month=2020-01")                 # лишний параметр не мешает
    assert s == 200
    assert call(port, "GET", "api/report?period=quarter")[0] == 422


def test_day_and_month_endpoints_return_category_groups(env):
    port, _ = env
    for rest in ("api/report?period=day", "api/report?period=month"):
        s, b = call(port, "GET", rest)
        assert s == 200 and b["by_category"] == []
