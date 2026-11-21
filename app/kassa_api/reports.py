"""Отчёты «Сегодня», «Месяц» и «Период» (произвольные даты) и список «Последние». Только чтение; все цифры считает этот код, не модель.

Правила (спецификация, разд. 5 и Т3.1):
- в отчётах только активные записи (status='active'), дата - occurred_on; записи, перекрытые закрытием месяца
  (closed_by не пусто), в отчётах не участвуют: у закрытого месяца источник правды - строки закрытия
  (source='month-close', итог закрытия), отчёт месяца сверяется с итогами закрытия (closed.matches);
- по каждой валюте: расходы, поступления, итог (поступления минус расходы) в целых центах, без пересчёта между валютами;
- «≈ USD» - сумма usd_cents (снимок курса на момент записи); у записей без курса (usd_cents NULL) её нет,
  их число возвращается отдельно, чтобы страница не выдавала неполную сумму за полную;
- обмены валют (exchanges.py) в итоги не входят: у периода есть только число действующих обменов («exchanges»); комиссия
  обмена - обычный расход и считается как все;
- месяц дополнительно: расходы по категориям, поступления по способам и по сегментам (B2B / B2C / без пометки; у закрытых
  месяцев сегментов нет, это строки закрытия).
"""
import calendar
import collections
import json
import re
import sqlite3
from datetime import date as date_cls, timedelta

from .records import RecordError

RECENT_LIMIT = 20
MONTH_RE = re.compile(r"[0-9]{4}-[0-9]{2}")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
NO_METHOD_TITLE = "без способа"
SEGMENT_TITLES = {"b2b": "B2B", "b2c": "B2C"}
NO_SEGMENT_TITLE = "Без пометки"
MIN_YEAR = 2000

ITEM_COLUMNS = ("t.id, t.direction, t.amount_cents, t.currency, t.usd_cents, t.category, t.note, t.method, "
                "m.title AS method_title, t.occurred_on, t.created_at, t.status, t.via, t.closed_by, t.source, t.payer, t.segment, t.exchange_id")
ITEM_FROM = "FROM transactions t LEFT JOIN methods m ON m.code = t.method"


def _item(r) -> dict:
    return {"id": r["id"], "direction": r["direction"], "amount_cents": r["amount_cents"], "currency": r["currency"],
            "usd_cents": r["usd_cents"], "category": r["category"], "note": r["note"], "method": r["method"],
            "method_title": r["method_title"], "occurred_on": r["occurred_on"], "created_at": r["created_at"],
            "status": r["status"], "via": r["via"], "closed_by": r["closed_by"], "payer": r["payer"],
            "segment": r["segment"], "exchange_id": r["exchange_id"]}


MAX_RANGE_DAYS = 400


def _past_day(key: str | None, today_d: date_cls) -> date_cls:
    if not key or not DATE_RE.fullmatch(key):
        raise RecordError(422, "bad_period")
    try:
        d = date_cls.fromisoformat(key)
    except ValueError:
        raise RecordError(422, "bad_period") from None
    if d > today_d or d.year < MIN_YEAR:
        raise RecordError(422, "bad_period")
    return d


def period_bounds(period: str, key: str | None, today: str, to: str | None = None) -> tuple[str, str]:
    """('2026-10-01', '2026-10-31') для месяца, (d, d) для дня, (from, to) включительно для period='range'
    (key = from, to = to; from <= to <= сегодня, не больше MAX_RANGE_DAYS дней: иначе 422 bad_period / period_too_long).
    Будущее и мусор -> 422 bad_period. «Сегодня» передаёт вызывающий (db.today_local(): день в поясе KASSA_TZ)."""
    today_d = date_cls.fromisoformat(today)
    if period == "range":
        first, last = _past_day(key, today_d), _past_day(to, today_d)
        if first > last:
            raise RecordError(422, "bad_period")
        if (last - first).days + 1 > MAX_RANGE_DAYS:
            raise RecordError(422, "period_too_long")
        return first.isoformat(), last.isoformat()
    if period == "day":
        key = key or today
        if not DATE_RE.fullmatch(key):
            raise RecordError(422, "bad_period")
        try:
            d = date_cls.fromisoformat(key)
        except ValueError:
            raise RecordError(422, "bad_period") from None
        if d > today_d or d.year < MIN_YEAR:
            raise RecordError(422, "bad_period")
        return key, key
    if period == "month":
        key = key or today[:7]
        if not MONTH_RE.fullmatch(key):
            raise RecordError(422, "bad_period")
        year, month = int(key[:4]), int(key[5:7])
        if not (1 <= month <= 12) or year < MIN_YEAR or (year, month) > (today_d.year, today_d.month):
            raise RecordError(422, "bad_period")
        return "%04d-%02d-01" % (year, month), "%04d-%02d-%02d" % (year, month, calendar.monthrange(year, month)[1])
    raise RecordError(422, "bad_period")


def _sorted_currencies(totals: dict) -> list[dict]:
    """По убыванию числа записей, затем по коду: самая используемая валюта сверху."""
    out = [{"currency": cur, **v} for cur, v in totals.items()]
    out.sort(key=lambda x: (-x["count"], x["currency"]))
    return out


def _period_rows(conn: sqlite3.Connection, first: str, last: str):
    """Записи отчётного периода: активные, не перекрытые закрытием месяца (общий отбор для report и income)."""
    return conn.execute(
        "SELECT " + ITEM_COLUMNS + " " + ITEM_FROM + " "
        "WHERE t.status='active' AND t.closed_by IS NULL AND t.occurred_on>=? AND t.occurred_on<=? "
        "ORDER BY t.occurred_on DESC, t.created_at DESC, t.id DESC", (first, last)).fetchall()


def report(conn: sqlite3.Connection, period: str, key: str | None, today: str, to: str | None = None) -> dict:
    """period: day | month | range. Для range key = дата начала, to = дата конца (включительно); ответ того же вида,
    что у month (итоги по валютам, ≈ USD, by_category, by_method, by_segment, count), без пометки о закрытии месяца."""
    first, last = period_bounds(period, key, today, to)
    rows = _period_rows(conn, first, last)

    per_cur = collections.defaultdict(lambda: {"out_cents": 0, "in_cents": 0, "count": 0})
    usd = {"out_cents": 0, "in_cents": 0, "records_without_usd": 0}
    for r in rows:
        side = r["direction"] + "_cents"
        per_cur[r["currency"]][side] += r["amount_cents"]
        per_cur[r["currency"]]["count"] += 1
        if r["usd_cents"] is None:
            usd["records_without_usd"] += 1
        else:
            usd[side] += r["usd_cents"]
    for v in per_cur.values():
        v["net_cents"] = v["in_cents"] - v["out_cents"]
    usd["net_cents"] = usd["in_cents"] - usd["out_cents"]

    result = {"ok": True, "period": period, "from": first, "to": last, "count": len(rows),
              "currencies": _sorted_currencies(per_cur), "usd": usd,
              # обмены в итоги не входят (отдельные таблицы); только число за период, без сумм
              "exchanges": conn.execute("SELECT COUNT(*) FROM exchanges WHERE status='active' AND occurred_on>=? "
                                        "AND occurred_on<=?", (first, last)).fetchone()[0]}
    if period == "day":
        result["records"] = [_item(r) for r in rows]
        result["by_category"] = _group(rows, "out", lambda r: r["category"] or "без категории")
    else:
        result["by_category"] = _group(rows, "out", lambda r: r["category"] or "без категории")
        result["by_method"] = _group(rows, "in", lambda r: r["method_title"] or NO_METHOD_TITLE)
        result["by_segment"] = _group(rows, "in", lambda r: SEGMENT_TITLES.get(r["segment"], NO_SEGMENT_TITLE))
        closed = _closed_info(conn, first[:7], rows) if period == "month" else None
        if closed is not None:
            result["closed"] = closed
    return result


TREND_FROM = "2026-05"          # динамика по месяцам начинается с мая 2026 (первый месяц данных Кассы)


def trend(conn: sqlite3.Connection, today: str) -> dict:
    """Динамика по месяцам (май -> текущий): доход, расход и нетто в USD, закрыт ли месяц. Месяц считается тем же
    кодом, что и отчёт «Месяц» (report), поэтому цифры по месяцам не могут разойтись с его итогами."""
    y, m = int(TREND_FROM[:4]), int(TREND_FROM[5:7])
    ty, tm = int(today[:4]), int(today[5:7])
    months = []
    while (y, m) <= (ty, tm):
        key = "%04d-%02d" % (y, m)
        rep = report(conn, "month", key, today)
        u = rep["usd"]
        months.append({"month": key, "in_usd_cents": u["in_cents"], "out_usd_cents": u["out_cents"],
                       "net_usd_cents": u["net_cents"], "records_without_usd": u["records_without_usd"],
                       "count": rep["count"], "closed": "closed" in rep,
                       "closed_matches": bool(rep.get("closed", {}).get("matches"))})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return {"ok": True, "period": "trend", "from": TREND_FROM, "to": today[:7], "months": months}


INCOME_PAYERS_LIMIT = 20


def _income_day(key: str) -> date_cls:
    if not DATE_RE.fullmatch(key):
        raise RecordError(422, "bad_period")
    try:
        d = date_cls.fromisoformat(key)
    except ValueError:
        raise RecordError(422, "bad_period") from None
    if d.year < MIN_YEAR:
        raise RecordError(422, "bad_period")
    return d


def income_bounds(period: str, key: str | None, today: str) -> tuple[str, str]:
    """Границы периода для income: 'week' - пн-вс по дню key (по умолчанию сегодня), 'month' - месяц дня key
    ('YYYY-MM-DD' или 'YYYY-MM'). День внутри текущего периода допустим и тогда, когда он позже сегодня; период
    целиком в будущем и мусор -> 422 bad_period."""
    today_d = date_cls.fromisoformat(today)
    if period == "week":
        d = _income_day(key or today)
        monday = d - timedelta(days=d.weekday())
        if monday > today_d:
            raise RecordError(422, "bad_period")
        return monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    if period == "month":
        if key and DATE_RE.fullmatch(key):
            key = _income_day(key).isoformat()[:7]
        return period_bounds("month", key, today)
    raise RecordError(422, "bad_period")


def income(conn: sqlite3.Connection, period: str, key: str | None, today: str) -> dict:
    """Только поступления периода (итоги, не записи): итог ≈ USD, B2B / B2C / без пометки,
    по плательщикам, число записей и записей без курса. Расходов, заметок,
    списка записей и капитала в ответе нет. Отбор записей - тот же, что у report (закрытый месяц считается
    его строками закрытия: сегментов и плательщиков там нет)."""
    first, last = income_bounds(period, key, today)
    rows = [r for r in _period_rows(conn, first, last) if r["direction"] == "in"]
    total = {"usd_cents": 0, "count": 0, "without_usd": 0}
    seg = {name: {"usd_cents": 0, "count": 0, "without_usd": 0} for name in ("b2b", "b2c", "unmarked")}
    payers: dict = {}
    for r in rows:
        cents = r["usd_cents"] or 0
        part = seg[r["segment"] if r["segment"] in SEGMENT_TITLES else "unmarked"]
        for g in (total, part):
            g["count"] += 1
            g["usd_cents"] += cents
            g["without_usd"] += r["usd_cents"] is None
        if r["payer"]:
            p = payers.setdefault(r["payer"], {"payer": r["payer"], "usd_cents": 0, "count": 0, "without_usd": 0})
            p["count"] += 1
            p["usd_cents"] += cents
            p["without_usd"] += r["usd_cents"] is None
    by_payer = sorted(payers.values(), key=lambda p: (-p["usd_cents"], -p["count"], p["payer"]))
    closed = any(conn.execute("SELECT 1 FROM closed_months WHERE month=?", (m,)).fetchone() for m in
                 sorted({first[:7], last[:7]}))
    return {"ok": True, "period": period, "from": first, "to": last, "partial": last > today,
            "closed_month": closed, "income": total, "by_segment": seg, "by_payer": by_payer[:INCOME_PAYERS_LIMIT],
            "payers_omitted": max(0, len(by_payer) - INCOME_PAYERS_LIMIT)}


def _closed_info(conn: sqlite3.Connection, month: str, rows) -> dict | None:
    """Пометка «месяц закрыт»: когда закрыт, курсы закрытия и сверка итогов отчёта с итогами закрытия
    (доход, расход, нетто в USD и расходы по категориям). Курс - из закрытия, не текущий."""
    cm = conn.execute("SELECT closed_at, rates, rates_source, expected FROM closed_months WHERE month=?",
                      (month,)).fetchone()
    if cm is None:
        return None
    expected = json.loads(cm["expected"])
    live_in = sum(r["usd_cents"] or 0 for r in rows if r["direction"] == "in")
    live_out = sum(r["usd_cents"] or 0 for r in rows if r["direction"] == "out")
    live_cat = collections.defaultdict(int)
    for r in rows:
        if r["direction"] == "out":
            live_cat[r["category"] or "без категории"] += r["usd_cents"] or 0
    matches = (live_in == expected["income_usd_cents"] and live_out == expected["expense_usd_cents"]
               and live_in - live_out == expected["net_usd_cents"] and dict(live_cat) == expected["by_category"])
    return {"month": month, "closed_at": cm["closed_at"], "matches": matches,
            "rates": json.loads(cm["rates"]) if cm["rates"] else {}, "rates_source": cm["rates_source"]}


def _group(rows, direction: str, name_of) -> list[dict]:
    groups = collections.OrderedDict()
    for r in rows:
        if r["direction"] != direction:
            continue
        g = groups.setdefault(name_of(r), {"count": 0, "usd_cents": 0, "without_usd": 0, "by_currency": {}})
        g["count"] += 1
        g["by_currency"][r["currency"]] = g["by_currency"].get(r["currency"], 0) + r["amount_cents"]
        if r["usd_cents"] is None:
            g["without_usd"] += 1
        else:
            g["usd_cents"] += r["usd_cents"]
    out = [{"name": name, "count": g["count"], "usd_cents": g["usd_cents"], "without_usd": g["without_usd"],
            "currencies": [{"currency": c, "cents": v} for c, v in sorted(g["by_currency"].items())]}
           for name, g in groups.items()]
    out.sort(key=lambda g: (-g["usd_cents"], -g["count"], g["name"]))
    return out


def recent(conn: sqlite3.Connection, no_method: bool = False, limit: int = RECENT_LIMIT,
           closed: bool = False) -> dict:
    """20 последних внесённых записей (и действующих, и удалённых; импортированные записи, перекрытые закрытием месяца,
    видны с пометкой closed_by; сами строки закрытия - не «внесённые», их здесь нет).
    no_method - поступления без способа (только действующие, не перекрытые), closed - перекрытые закрытием."""
    if closed:
        where = "WHERE t.closed_by IS NOT NULL"
    elif no_method:
        where = "WHERE t.direction='in' AND t.status='active' AND t.method IS NULL AND t.closed_by IS NULL " \
                "AND t.source <> 'month-close'"
    else:
        where = "WHERE t.source <> 'month-close'"
    rows = conn.execute("SELECT " + ITEM_COLUMNS + " " + ITEM_FROM + " " + where +
                        " ORDER BY t.created_at DESC, t.id DESC LIMIT ?", (limit,)).fetchall()
    total_no_method = conn.execute("SELECT COUNT(*) FROM transactions WHERE direction='in' AND status='active' "
                                   "AND method IS NULL AND closed_by IS NULL AND source <> 'month-close'"
                                   ).fetchone()[0]
    total_closed = conn.execute("SELECT COUNT(*) FROM transactions WHERE closed_by IS NOT NULL").fetchone()[0]
    return {"ok": True, "records": [_item(r) for r in rows], "no_method_total": total_no_method,
            "closed_total": total_closed}


def get_item(conn: sqlite3.Connection, rec_id: int) -> dict | None:
    r = conn.execute("SELECT " + ITEM_COLUMNS + " " + ITEM_FROM + " WHERE t.id=?", (rec_id,)).fetchone()
    return _item(r) if r else None
