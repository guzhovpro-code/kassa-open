"""Подведение месяца: загрузка файла закрытия, сверка и показ.

Запуск внутри контейнера api (база берётся из KASSA_DB_PATH):
  docker compose exec -T api python -m kassa_api.month_close load - [--replace] < close-2026-10.json
  docker compose exec -T api python -m kassa_api.month_close verify 2026-10
  docker compose exec -T api python -m kassa_api.month_close show 2026-10
  docker compose exec -T api python -m kassa_api.month_close export 2026-10   # записи месяца, внесённые руками
Файл можно передать и путём (`load /data/close/2026-10.json`): внутри контейнера виден только каталог ./data.

Формат файла закрытия (JSON):
{
  "month": "2026-10",
  "rows": [
    {"date": "2026-10-03", "type": "income", "amount": "1500.00", "currency": "USD", "method": "Биржа OKX",
     "category": "доход", "note": "текст", "usd": "1500.00", "rate": 0.029},
    {"date": "2026-10-04", "type": "expense", "amount": "120", "currency": "TRY", "method": "Наличные", ...},
    {"date": "2026-10-05", "type": "transfer", "amount": "300", "currency": "USD", "method": "Биржа OKX",
     "to_method": "Наличные"}
  ],
  "rates": {"TRY": 0.029},                 // необязательно: курс (долларов за 1 единицу валюты) для строк без usd
  "rates_source": "откуда курсы",           // необязательно
  "totals": {"income_usd": "1500.00", "expense_usd": "4.00",     // необязательно: итоги для сверки,
             "by_category": {"кафе": "4.00"}}                    // посчитанные отдельно от строк
}
- type: income | expense | transfer; сумма положительная; сумма в USD у строки: поле usd, иначе сама сумма для
  USD/USDT/USDC, иначе rate строки, иначе rates файла; нет курса - месяц не загружается.
- Переводы между своими счетами не доход и не расход: считаются в итоге закрытия отдельно (число), записями не
  становятся и в отчёты не попадают.

Что делает load, одной транзакцией:
- income и expense записываются как source='month-close', via='month-close', idem_key='mclose:<месяц>:<n>';
- всё, что за этот месяц внесено руками (бот, форма) и не является закрытием, получает closed_by=<месяц>: запись
  остаётся в базе и видна с пометкой, но в отчётах не участвует (источник правды - строки закрытия);
- в closed_months пишутся итоги закрытия, курсы и sha256 файла; в закрытый месяц новые записи не вносятся;
- итоги, посчитанные из строк, сверяются с totals файла (если они есть); не сошлось - месяц не загружается.
Повтор с тем же файлом ничего не меняет. Файл изменился - отказ, пока не указан --replace (строки закрытия месяца
заменяются целиком). Способ, которого нет в списке, создаётся (названия новых способов печатаются).
Печатаются счётчики и «совпало / не совпало»; заметки и имена не выводятся.
"""
import argparse
import collections
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date as date_cls
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import categorize, db, records, reports

SOURCE = "month-close"
VIA = "month-close"
ACTOR = "month-close"

MONTH_RE = re.compile(r"[0-9]{4}-[0-9]{2}")
CURRENCY_RE = re.compile(r"[A-Z][A-Z0-9]{1,9}")
TYPES = {"income": "in", "expense": "out"}
DOLLAR_LIKE = {"USD", "USDT", "USDC"}
MAX_UNITS = 1_000_000_000


class CloseError(Exception):
    """Месяц не загружен: причина без сумм и текстов, в args[0]."""


@dataclass
class Row:
    n: int
    direction: str
    cents: int
    currency: str
    usd_cents: int
    category: str
    note: str
    occurred_on: str
    method_title: str | None
    orig: str | None = None


@dataclass
class Parsed:
    month: str
    sha: str
    rows: list = field(default_factory=list)
    transfers: int = 0
    rates: dict = field(default_factory=dict)
    rates_source: str | None = None
    expected: dict = field(default_factory=dict)


def _dec(value, what: str, positive=True) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise CloseError("bad_amount:" + what)
    try:
        text = value.strip().replace(" ", "").replace(" ", "").replace(",", ".") if isinstance(value, str) \
            else repr(value)
        dec = Decimal(text)
    except (InvalidOperation, ValueError):
        raise CloseError("bad_amount:" + what) from None
    if not dec.is_finite() or (positive and dec <= 0) or abs(dec) > MAX_UNITS:
        raise CloseError("bad_amount:" + what)
    return dec


def _cents(dec: Decimal) -> int:
    return int((dec * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _text(value, limit: int, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CloseError("bad_text:" + what)
    text = " ".join(value.split())
    if len(text) > limit:
        raise CloseError("text_too_long:" + what)
    return text


def parse_close(raw_bytes: bytes) -> Parsed:
    try:
        d = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CloseError("not_json") from None
    if not isinstance(d, dict) or not isinstance(d.get("rows"), list):
        raise CloseError("bad_file_shape")
    month = d.get("month")
    if not isinstance(month, str) or not MONTH_RE.fullmatch(month):
        raise CloseError("bad_month")
    try:
        date_cls.fromisoformat(month + "-01")
    except ValueError:
        raise CloseError("bad_month") from None
    file_rates = d.get("rates") or {}
    if not isinstance(file_rates, dict):
        raise CloseError("bad_rates")
    rates = {}
    for key, val in file_rates.items():
        rates[str(key).upper()] = _dec(val, "rate:" + str(key))
    src = d.get("rates_source")
    p = Parsed(month=month, sha=hashlib.sha256(raw_bytes).hexdigest(),
               rates={k: float(v) for k, v in rates.items()},
               rates_source=src if isinstance(src, str) else None)
    n = 0
    for i, r in enumerate(d["rows"], start=1):
        if not isinstance(r, dict):
            raise CloseError("bad_row:%d" % i)
        kind = r.get("type")
        if kind not in ("income", "expense", "transfer"):
            raise CloseError("bad_type:row %d" % i)
        day = r.get("date")
        if not isinstance(day, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
            raise CloseError("bad_date:row %d" % i)
        try:
            date_cls.fromisoformat(day)
        except ValueError:
            raise CloseError("bad_date:row %d" % i) from None
        if not day.startswith(month):
            raise CloseError("row_outside_month:row %d" % i)
        cur = r.get("currency")
        if not isinstance(cur, str) or not CURRENCY_RE.fullmatch(cur.strip().upper()) or not cur.strip().isascii():
            raise CloseError("bad_currency:row %d" % i)
        cur = cur.strip().upper()
        amount = _dec(r.get("amount"), "row %d" % i)
        if kind == "transfer":
            p.transfers += 1
            continue
        if r.get("usd") not in (None, ""):
            usd = _cents(_dec(r["usd"], "usd row %d" % i))
        else:
            if cur in DOLLAR_LIKE:
                rate = Decimal(1)
            elif r.get("rate") not in (None, ""):
                rate = _dec(r["rate"], "rate row %d" % i)
            elif cur in rates:
                rate = rates[cur]
            else:
                raise CloseError("no_usd_rate:row %d" % i)
            usd = _cents(amount * rate)
        cents = _cents(amount)
        if cents == 0 or usd <= 0:
            raise CloseError("amount_too_small:row %d" % i)
        orig = format(amount, "f") if amount != amount.quantize(Decimal("0.01")) else None
        direction = TYPES[kind]
        note = _text(r.get("note"), 200, "note row %d" % i)
        category = _text(r.get("category"), 40, "category row %d" % i) or categorize.categorize(note, direction)
        method = _text(r.get("method"), 60, "method row %d" % i) or None
        n += 1
        p.rows.append(Row(n, direction, cents, cur, usd, category, note, day, method, orig))
    if not p.rows and not p.transfers:
        raise CloseError("no_rows")
    got = row_totals(p.rows)
    t = d.get("totals")
    if t is not None:
        if not isinstance(t, dict):
            raise CloseError("bad_totals")
        want = {"income_usd_cents": _cents(_dec(t.get("income_usd"), "totals.income_usd", False)),
                "expense_usd_cents": _cents(_dec(t.get("expense_usd"), "totals.expense_usd", False))}
        want["net_usd_cents"] = want["income_usd_cents"] - want["expense_usd_cents"]
        cats = t.get("by_category")
        want["by_category"] = ({str(k): _cents(_dec(v, "totals.by_category", False)) for k, v in cats.items()}
                               if isinstance(cats, dict) else got["by_category"])
        for key in ("income_usd_cents", "expense_usd_cents"):
            if got[key] != want[key]:
                raise CloseError("rows_do_not_match_file_totals:" + key)
        if got["by_category"] != want["by_category"]:
            bad = sorted(k for k in set(got["by_category"]) | set(want["by_category"])
                         if got["by_category"].get(k) != want["by_category"].get(k))
            raise CloseError("rows_do_not_match_file_categories:" + ", ".join(bad[:5]))
        p.expected = {**want, "totals_from_file": True}
    else:
        p.expected = {**got, "totals_from_file": False}
    p.expected["transfers"] = p.transfers
    return p


def row_totals(rows) -> dict:
    cat = collections.defaultdict(int)
    t_in = t_out = 0
    for r in rows:
        if r.direction == "in":
            t_in += r.usd_cents
        else:
            t_out += r.usd_cents
            cat[r.category] += r.usd_cents
    return {"income_usd_cents": t_in, "expense_usd_cents": t_out, "net_usd_cents": t_in - t_out,
            "by_category": dict(cat)}


def load(db_path, raw_bytes: bytes, replace=False, name="stdin", env=None) -> dict:
    """Загружает один месяц; результат со статусом. Месяц с ошибкой не пишется вовсе."""
    p = parse_close(raw_bytes)
    db.init_db(db_path, os.environ if env is None else env)
    conn = db.connect(db_path)
    try:
        existing = conn.execute("SELECT source_sha FROM closed_months WHERE month=?", (p.month,)).fetchone()
        if existing is not None and not replace:
            if existing["source_sha"] == p.sha:
                return {"month": p.month, "status": "already_loaded", "inserted": 0}
            raise CloseError("file_changed_use_replace")
        ts = db.now_utc_iso()
        stats = collections.Counter()
        new_methods = []
        conn.execute("BEGIN IMMEDIATE")
        try:
            if existing is not None:                 # --replace: строки закрытия месяца заменяются целиком
                stats["replaced_rows"] = conn.execute(
                    "DELETE FROM transactions WHERE source=? AND idem_key LIKE ?",
                    (SOURCE, "mclose:%s:%%" % p.month)).rowcount
                conn.execute("UPDATE transactions SET closed_by=NULL WHERE closed_by=?", (p.month,))
                conn.execute("DELETE FROM closed_months WHERE month=?", (p.month,))
            known = {r["code"] for r in conn.execute("SELECT code FROM methods")}
            for r in p.rows:
                code = None
                if r.method_title:
                    code = records._ensure_method(conn, r.method_title, r.direction)
                    if code not in known:
                        known.add(code)
                        new_methods.append(r.method_title)
                cur = conn.execute(
                    "INSERT INTO transactions(idem_key, direction, amount_cents, amount_orig, currency, usd_cents, "
                    "category, note, method, occurred_on, created_at, status, source, via, actor_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)",
                    ("mclose:%s:%d" % (p.month, r.n), r.direction, r.cents, r.orig, r.currency, r.usd_cents,
                     r.category, r.note, code, r.occurred_on, ts, SOURCE, VIA, ACTOR))
                stats["inserted_" + r.direction] += cur.rowcount
            superseded = {row["source"]: row["n"] for row in conn.execute(
                "SELECT source, COUNT(*) n FROM transactions WHERE substr(occurred_on,1,7)=? AND source <> ? "
                "AND closed_by IS NULL GROUP BY source", (p.month, SOURCE))}
            conn.execute("UPDATE transactions SET closed_by=? WHERE substr(occurred_on,1,7)=? AND source <> ? "
                         "AND closed_by IS NULL", (p.month, p.month, SOURCE))
            conn.execute("INSERT INTO closed_months(month, closed_at, source_name, source_sha, rates, rates_source, "
                         "expected, rows_in, rows_out) VALUES (?,?,?,?,?,?,?,?,?)",
                         (p.month, ts, name, p.sha, json.dumps(p.rates), p.rates_source,
                          json.dumps(p.expected, ensure_ascii=False),
                          stats["inserted_in"], stats["inserted_out"]))
            conn.execute("INSERT INTO audit(ts, actor_id, action, entity, entity_id, via, detail) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (ts, None, "month_close_load", "month", p.month, VIA,
                          json.dumps({"inserted_in": stats["inserted_in"], "inserted_out": stats["inserted_out"],
                                      "transfers": p.transfers, "superseded": superseded,
                                      "replaced_rows": stats["replaced_rows"]}, ensure_ascii=False)))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {"month": p.month, "status": "loaded" if existing is None else "replaced",
            "inserted": stats["inserted_in"] + stats["inserted_out"], "inserted_in": stats["inserted_in"],
            "inserted_out": stats["inserted_out"], "transfers_not_counted": p.transfers,
            "superseded": superseded, "replaced_rows": stats["replaced_rows"], "new_methods": new_methods}


def _month_arg(month: str) -> str:
    if not MONTH_RE.fullmatch(month or ""):
        raise CloseError("bad_month")
    return month


def verify(db_path, month: str, file_bytes: bytes | None = None, today=None) -> dict:
    """Сверка загруженного месяца: строки против итогов закрытия, отчёт «Месяц» против них же."""
    month = _month_arg(month)
    conn = db.connect(db_path, readonly=True)
    today = today or db.today_local()
    res = {"month": month, "ok": False}
    try:
        cm = conn.execute("SELECT source_sha, expected, rows_in, rows_out FROM closed_months WHERE month=?",
                          (month,)).fetchone()
        if cm is None:
            res["error"] = "not_loaded"
            return res
        expected = json.loads(cm["expected"])
        rows = conn.execute("SELECT direction, usd_cents, category FROM transactions WHERE source=? AND "
                            "idem_key LIKE ? AND status='active'", (SOURCE, "mclose:%s:%%" % month)).fetchall()
        got = row_totals([Row(0, r["direction"], 0, "", r["usd_cents"] or 0, r["category"] or "", "", "", None)
                          for r in rows])
        in_db = sum(1 for r in rows if r["direction"] == "in")
        out_db = len(rows) - in_db
        rep = reports.report(conn, "month", month, today)
        closed = rep.get("closed") or {}
        live = {"income_usd_cents": rep["usd"]["in_cents"], "expense_usd_cents": rep["usd"]["out_cents"],
                "net_usd_cents": rep["usd"]["net_cents"],
                "by_category": {g["name"]: g["usd_cents"] for g in rep["by_category"]}}
        want = {k: expected[k] for k in ("income_usd_cents", "expense_usd_cents", "net_usd_cents", "by_category")}
        unflagged = conn.execute("SELECT COUNT(*) FROM transactions WHERE substr(occurred_on,1,7)=? AND source <> ? "
                                 "AND closed_by IS NULL", (month, SOURCE)).fetchone()[0]
        flagged = conn.execute("SELECT COUNT(*) FROM transactions WHERE closed_by=?", (month,)).fetchone()[0]
        res.update({
            "rows_in_out": {"db": [in_db, out_db], "expected": [cm["rows_in"], cm["rows_out"]]},
            "rows_equal_totals": got == want,
            "report_equals_totals": live == want,
            "report_marks_closed": bool(closed.get("matches")),
            "report_rows": rep["count"], "transfers": expected.get("transfers", 0),
            "hand_entered_covered": flagged, "not_covered": unflagged,
            "totals_from_file": expected.get("totals_from_file", False)})
        if file_bytes is not None:
            res["file_unchanged"] = hashlib.sha256(file_bytes).hexdigest() == cm["source_sha"]
        res["ok"] = (res["rows_in_out"]["db"] == res["rows_in_out"]["expected"] and res["rows_equal_totals"]
                     and res["report_equals_totals"] and res["report_marks_closed"] and unflagged == 0
                     and rep["count"] == len(rows) and res.get("file_unchanged", True))
    finally:
        conn.close()
    return res


def _money(cents: int) -> str:
    return "%d.%02d" % (cents // 100, cents % 100) if cents >= 0 else "-%d.%02d" % (-cents // 100, -cents % 100)


def show(db_path, month: str, today=None) -> dict:
    """Что показывает отчёт «Месяц»: закрыт ли месяц, итоги в USD, расходы по категориям. Заметок нет."""
    month = _month_arg(month)
    conn = db.connect(db_path, readonly=True)
    today = today or db.today_local()
    try:
        rep = reports.report(conn, "month", month, today)
        cm = conn.execute("SELECT closed_at, expected FROM closed_months WHERE month=?", (month,)).fetchone()
    finally:
        conn.close()
    out = {"month": month, "closed": cm is not None, "closed_matches": bool((rep.get("closed") or {}).get("matches")),
           "records_in_report": rep["count"], "usd": {k: _money(rep["usd"][k + "_cents"]) for k in ("in", "out", "net")},
           "by_category": {g["name"]: _money(g["usd_cents"]) for g in rep["by_category"]}}
    if cm is not None:
        out["closed_at"] = cm["closed_at"]
        out["transfers"] = json.loads(cm["expected"]).get("transfers", 0)
    return out


def export_hand(db_path, month: str) -> list:
    """Записи месяца, внесённые руками (бот, форма) и ещё не перекрытые закрытием, в виде строк файла закрытия:
    агент добавляет их в файл (после закрытия они в отчётах не участвуют, только строки закрытия)."""
    month = _month_arg(month)
    conn = db.connect(db_path, readonly=True)
    try:
        rows = conn.execute(
            "SELECT t.direction, t.amount_cents, t.amount_orig, t.currency, t.usd_cents, t.category, t.note, "
            "t.occurred_on, m.title AS method FROM transactions t LEFT JOIN methods m ON m.code=t.method "
            "WHERE t.status='active' AND t.closed_by IS NULL AND t.source <> ? AND substr(t.occurred_on,1,7)=? "
            "ORDER BY t.occurred_on, t.id", (SOURCE, month)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        row = {"date": r["occurred_on"], "type": "income" if r["direction"] == "in" else "expense",
               "amount": r["amount_orig"] or _money(r["amount_cents"]), "currency": r["currency"],
               "method": r["method"] or "", "category": r["category"] or "", "note": r["note"] or ""}
        if r["usd_cents"] is not None:
            row["usd"] = _money(r["usd_cents"])
        out.append(row)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["load", "verify", "show", "export"])
    ap.add_argument("target", help="load: файл закрытия или - (stdin); verify, show, export: месяц YYYY-MM")
    ap.add_argument("--db", default=os.environ.get("KASSA_DB_PATH", "/data/kassa.db"))
    ap.add_argument("--replace", action="store_true", help="load: заменить уже загруженное закрытие месяца")
    ap.add_argument("--file", help="verify: файл закрытия, чтобы проверить, что он не менялся")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "load":
            raw = sys.stdin.buffer.read() if args.target == "-" else open(args.target, "rb").read()
            res = load(args.db, raw, args.replace, name="stdin" if args.target == "-" else os.path.basename(args.target))
            print("load", json.dumps(res, ensure_ascii=False))
            return 0
        if args.cmd == "verify":
            fb = open(args.file, "rb").read() if args.file else None
            res = verify(args.db, args.target, fb)
            print("verify", args.target, "совпало" if res["ok"] else "не совпало", json.dumps(res, ensure_ascii=False))
            return 0 if res["ok"] else 1
        if args.cmd == "export":
            print(json.dumps(export_hand(args.db, args.target), ensure_ascii=False, indent=1))
            return 0
        res = show(args.db, args.target)
        print("show", json.dumps(res, ensure_ascii=False))
        return 0
    except CloseError as exc:
        print("error", exc.args[0])
        return 1
    except (OSError, sqlite3.Error) as exc:
        print("error", type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
