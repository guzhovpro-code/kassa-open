"""Bybit v5, только чтение: депозиты и выводы за месяц -> CSV в import/.

    python3 scripts/exchange_bybit_readonly.py 2026-10 [--out-dir import]

Ключи берутся из окружения (в файле .env рядом с проектом): KASSA_EXCHANGE_BYBIT_KEY и KASSA_EXCHANGE_BYBIT_SECRET.
Ключ создаётся на Bybit с правом «Read-Only» (без торговли и без вывода средств), см. MONTHLY-CLOSE-PROMPT.md.
Нет ключа - скрипт ничего не делает и пишет «биржа Bybit пропущена: нет ключа» (код выхода 0).
Скрипт только читает два метода API (депозиты и выводы); адреса получателей и txID в файл не попадают.
Файл: import/bybit-<месяц>.csv, колонки date, kind (deposit | withdrawal), coin, chain, amount, fee, status.
Даты - по UTC. Ничего, кроме стандартной библиотеки, не нужно.
"""
import argparse
import csv
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from calendar import monthrange
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://api.bybit.com"
RECV_WINDOW = "5000"
WINDOW_DAYS = 7              # запрос по окнам в 7 дней: у Bybit окно не больше 30
PAGE_LIMIT = 50
ENDPOINTS = (("deposit", "/v5/asset/deposit/query-record", "successAt", "depositFee"),
             ("withdrawal", "/v5/asset/withdraw/query-record", "updateTime", "withdrawFee"))
OK_STATUS = {"deposit": {"3"}, "withdrawal": {"success"}}      # только завершённые операции
CSV_COLUMNS = ["date", "kind", "coin", "chain", "amount", "fee", "status"]


class BybitError(Exception):
    pass


def http_get(url: str, headers: dict) -> dict:
    """Единственное место, где скрипт ходит в сеть (в тесте подменяется)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise BybitError("http_%d" % exc.code) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise BybitError("network") from None


def sign(secret: str, ts: str, key: str, query: str) -> str:
    return hmac.new(secret.encode(), (ts + key + RECV_WINDOW + query).encode(), hashlib.sha256).hexdigest()


def month_windows(month: str) -> list:
    y, m = int(month[:4]), int(month[5:7])
    start = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)
    last = monthrange(y, m)[1]
    end = int(datetime(y, m, last, 23, 59, 59, 999000, tzinfo=timezone.utc).timestamp() * 1000)
    step = WINDOW_DAYS * 86400 * 1000
    out, cur = [], start
    while cur <= end:
        out.append((cur, min(cur + step - 1, end)))
        cur += step
    return out


def fetch_rows(kind, path, key, secret, window, base_url=BASE_URL, get=http_get, now_ms=None) -> list:
    rows, cursor = [], None
    while True:
        params = {"startTime": window[0], "endTime": window[1], "limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        query = urllib.parse.urlencode(params)
        ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
        headers = {"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": RECV_WINDOW,
                   "X-BAPI-SIGN": sign(secret, ts, key, query)}
        data = get(base_url + path + "?" + query, headers)
        if data.get("retCode") != 0:
            raise BybitError("api_code_%s" % data.get("retCode"))      # текст ответа не печатаем
        result = data.get("result") or {}
        rows += result.get("rows") or []
        cursor = result.get("nextPageCursor")
        if not cursor or not result.get("rows"):
            return rows


def collect(month, key, secret, base_url=BASE_URL, get=http_get) -> list:
    out, seen = [], set()
    for kind, path, time_field, fee_field in ENDPOINTS:
        for window in month_windows(month):
            for r in fetch_rows(kind, path, key, secret, window, base_url, get):
                if str(r.get("status")) not in OK_STATUS[kind]:
                    continue
                ts = int(r.get(time_field) or r.get("createdTime") or r.get("createTime") or 0)
                day = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
                if not day.startswith(month):
                    continue
                uid = (kind, r.get("txID") or r.get("id") or "", ts, r.get("amount"))
                if uid in seen:
                    continue
                seen.add(uid)
                out.append({"date": day, "kind": kind, "coin": r.get("coin", ""), "chain": r.get("chain", ""),
                            "amount": r.get("amount", ""), "fee": r.get(fee_field) or "0",
                            "status": r.get("status", "")})
    out.sort(key=lambda r: (r["date"], r["kind"], r["coin"]))
    return out


def write_csv(rows, out_dir, month) -> Path:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = Path(out_dir) / ("bybit-%s.csv" % month)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return path


def main(argv=None, env=os.environ, get=http_get) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("month", help="YYYY-MM")
    ap.add_argument("--out-dir", default="import")
    ap.add_argument("--base-url", default=BASE_URL)
    args = ap.parse_args(argv)
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", args.month):
        print("ошибка: месяц в виде ГГГГ-ММ")
        return 2
    key, secret = env.get("KASSA_EXCHANGE_BYBIT_KEY", ""), env.get("KASSA_EXCHANGE_BYBIT_SECRET", "")
    if not key or not secret:
        print("биржа Bybit пропущена: нет ключа")
        return 0
    try:
        rows = collect(args.month, key, secret, args.base_url, get)
    except BybitError as exc:
        print("биржа Bybit пропущена: не удалось прочитать (%s)" % exc.args[0])
        return 0
    path = write_csv(rows, args.out_dir, args.month)
    print("Bybit: %d операций за %s записано в %s" % (len(rows), args.month, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
