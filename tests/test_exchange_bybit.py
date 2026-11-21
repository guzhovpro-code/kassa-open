"""Bybit только чтение: подпись, окна, страницы, фильтры и пропуск без ключа (HTTP подменён, данные выдуманные)."""
import csv
import hashlib
import hmac
import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlparse

spec = importlib.util.spec_from_file_location(
    "exchange_bybit", Path(__file__).resolve().parent.parent / "scripts" / "exchange_bybit_readonly.py")
bb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bb)

MS = lambda y, m, d: int(bb.datetime(y, m, d, 12, tzinfo=bb.timezone.utc).timestamp() * 1000)   # noqa: E731


def fake_get(calls):
    def get(url, headers):
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        calls.append((u.path, q, headers))
        s, e = int(q["startTime"]), int(q["endTime"])
        if "deposit" in u.path:
            rows = [{"coin": "USDT", "chain": "TRX", "amount": "100.5", "status": 3, "txID": "t1",
                     "successAt": MS(2026, 10, 3), "toAddress": "SECRET-ADDR"},
                    {"coin": "USDT", "chain": "TRX", "amount": "9", "status": 2, "txID": "t2",
                     "successAt": MS(2026, 10, 3)}]
        else:
            rows = [{"coin": "USDT", "chain": "ETH", "amount": "40", "withdrawFee": "1", "status": "success",
                     "txID": "w1", "updateTime": MS(2026, 10, 20), "toAddress": "SECRET-ADDR"},
                    {"coin": "BTC", "chain": "BTC", "amount": "0.1", "status": "Cancel", "txID": "w2",
                     "updateTime": MS(2026, 10, 21)}]
        rows = [r for r in rows if s <= int(r.get("successAt") or r["updateTime"]) <= e]
        return {"retCode": 0, "result": {"rows": rows, "nextPageCursor": ""}}
    return get


def test_skips_without_key(tmp_path, capsys):
    assert bb.main(["2026-10", "--out-dir", str(tmp_path)], env={}, get=lambda *a: 1 / 0) == 0
    assert "биржа Bybit пропущена: нет ключа" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_collects_completed_only_signs_requests_and_hides_addresses(tmp_path, capsys):
    calls = []
    env = {"KASSA_EXCHANGE_BYBIT_KEY": "k123", "KASSA_EXCHANGE_BYBIT_SECRET": "s456"}
    assert bb.main(["2026-10", "--out-dir", str(tmp_path)], env=env, get=fake_get(calls)) == 0
    out = capsys.readouterr().out
    assert "s456" not in out and "k123" not in out
    rows = list(csv.DictReader(open(tmp_path / "bybit-2026-10.csv", encoding="utf-8")))
    assert [(r["date"], r["kind"], r["amount"], r["fee"]) for r in rows] == [
        ("2026-10-03", "deposit", "100.5", "0"), ("2026-10-20", "withdrawal", "40", "1")]
    assert "SECRET-ADDR" not in (tmp_path / "bybit-2026-10.csv").read_text()
    assert oct((tmp_path / "bybit-2026-10.csv").stat().st_mode & 0o777) == "0o600"
    path, q, headers = calls[0]
    expect = hmac.new(b"s456", (headers["X-BAPI-TIMESTAMP"] + "k123" + "5000" +
                                "&".join("%s=%s" % kv for kv in q.items())).encode(), hashlib.sha256).hexdigest()
    assert headers["X-BAPI-SIGN"] == expect
    assert all(p in ("/v5/asset/deposit/query-record", "/v5/asset/withdraw/query-record") for p, _, _ in calls)
    assert max(int(q["endTime"]) - int(q["startTime"]) for _, q, _ in calls) < 7 * 86400 * 1000


def test_only_read_endpoints_and_get(tmp_path):
    assert {e[1] for e in bb.ENDPOINTS} == {"/v5/asset/deposit/query-record", "/v5/asset/withdraw/query-record"}


def test_api_error_is_skipped_without_leaking(tmp_path, capsys):
    env = {"KASSA_EXCHANGE_BYBIT_KEY": "k", "KASSA_EXCHANGE_BYBIT_SECRET": "s"}
    assert bb.main(["2026-10", "--out-dir", str(tmp_path)], env=env,
                   get=lambda u, h: {"retCode": 10003, "retMsg": "Invalid api_key"}) == 0
    assert "пропущена" in capsys.readouterr().out and not list(tmp_path.iterdir())


def test_pagination_follows_cursor():
    pages = {"": {"retCode": 0, "result": {"rows": [{"coin": "A", "amount": "1", "status": 3, "txID": "1",
                                                      "successAt": MS(2026, 10, 2)}], "nextPageCursor": "c2"}},
             "c2": {"retCode": 0, "result": {"rows": [{"coin": "B", "amount": "2", "status": 3, "txID": "2",
                                                        "successAt": MS(2026, 10, 2)}], "nextPageCursor": ""}}}

    def get(url, headers):
        return pages[parse_qs(urlparse(url).query).get("cursor", [""])[0]]
    rows = bb.fetch_rows("deposit", "/p", "k", "s", (0, 1), get=get)
    assert [r["coin"] for r in rows] == ["A", "B"]


def test_bad_month(tmp_path):
    assert bb.main(["2026-13"], env={}) == 2
