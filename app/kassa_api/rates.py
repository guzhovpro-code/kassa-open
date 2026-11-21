"""Курсы валют и снимок usd_cents. Пересчёт в USD по кэшу курсов.

Правила:
- USD, USDT, USDC считаются 1:1 без запроса ;
- путь записи и правки в сеть НЕ ходит: берёт кэш (RateStore.usable()); нет курса - usd_cents = NULL, запись
  всё равно сохраняется;
- сеть - только в фоновом RateWorker (раз в несколько минут проверяет TTL кэша, потом дописывает usd_cents
  записям, оставшимся без курса);
- источники: frankfurter (ECB, база USD) и ЦБ РФ (RUB; у ECB рубля с 2022 нет). ЦБ РФ даёт ещё и кросс-курсы
  (через рубль) для валют, которых нет у ECB (KZT, AED, GEL...); у ECB приоритет, где он есть;
- курс - снимок на момент записи, а не на дату операции (не на дату операции): запись задним числом получает
  текущий курс;
- кэш старше MAX_AGE_SECONDS не используется (лучше «нет курса», чем недельный курс под видом свежего);
- правдоподобие (ревью 15.10.2026): эталон - ПОСЛЕДНИЙ ПРИНЯТЫЙ курс кода (поле ref в записи источника; он не
  стирается, когда новый курс отклонён). Курс, отличающийся от эталона больше чем на 30% (MAX_JUMP), не применяется
  (в кэш rates не попадает), в журнал строка «rates ALARM», владельцу тревога на каждом обновлении (раз в сутки по
  ключу); принять его можно только если второй источник (ECB и ЦБ РФ) в этом же обновлении назвал то же значение
  (в пределах 30%) - настоящий сдвиг подтверждается так, а повторение ошибочного значения - нет. Нет эталона (первый
  запуск, эталон старше двух недель), но второй источник называет другое значение - код отклоняется у обоих;
- где есть оба источника (ECB и ЦБ РФ), побеждает более свежий;
- в журнал идут только исходы и количества: суммы, заметки и сами курсы не пишутся.
"""
import json
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

PARITY = frozenset({"USD", "USDT", "USDC"})
ECB_URL = "https://api.frankfurter.dev/v1/latest?base=USD"
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
TTL_SECONDS = 12 * 3600               # время жизни кэша
MAX_AGE_SECONDS = 3 * 24 * 3600       # дольше выходных ECB: старше не используем
HTTP_TIMEOUT = 15
TICK_SECONDS = 300
CACHE_NAME = "rates-cache.json"
MAX_JUMP = 0.30                       # допустимое отклонение нового курса от прошлого
PREV_MAX_AGE = 14 * 24 * 3600         # старше двух недель прошлый курс для сравнения не годится
BACKFILL_AUDIT_IDS = 200              # сколько номеров записей перечислить в одной строке audit
ISO = "%Y-%m-%dT%H:%M:%SZ"


def http_get_json(url: str, timeout: float = HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "kassa-api (stdlib)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _valid_rate(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _clean_code(code) -> bool:
    return isinstance(code, str) and len(code) == 3 and code.isascii() and code.isalpha() and code.isupper()


def parse_ecb(data) -> dict:
    """Ответ frankfurter -> {код: единиц валюты за 1 USD}. Мусорные значения отбрасываются."""
    rates = data.get("rates") if isinstance(data, dict) else None
    if not isinstance(rates, dict):
        return {}
    return {c: float(v) for c, v in rates.items() if _clean_code(c) and _valid_rate(v) and c != "USD"}


def parse_cbr(data) -> dict:
    """Ответ ЦБ РФ (Valute[код] = {Nominal, Value} - рублей за Nominal единиц) -> {код: единиц за 1 USD}:
    RUB = рублей за доллар, остальные - кросс через рубль."""
    valute = data.get("Valute") if isinstance(data, dict) else None
    if not isinstance(valute, dict):
        return {}

    def rub_per_unit(code):
        item = valute.get(code)
        if not isinstance(item, dict):
            return None
        value, nominal = item.get("Value"), item.get("Nominal")
        if not (_valid_rate(value) and _valid_rate(nominal)):
            return None
        return float(value) / float(nominal)

    usd_rub = rub_per_unit("USD")
    if usd_rub is None:
        return {}
    out = {"RUB": usd_rub}
    for code in valute:
        if code in ("USD", "RUB") or not _clean_code(code):
            continue
        per_unit = rub_per_unit(code)
        if per_unit:
            out[code] = usd_rub / per_unit
    return out


def convert(currency: str, cents: int, rates) -> int | None:
    """Сумма в центах валюты -> центы USD (половина вверх по модулю) или None, если курса нет."""
    if currency in PARITY:
        return cents
    per_usd = (rates or {}).get(currency)
    if not _valid_rate(per_usd):
        return None
    q = (Decimal(abs(cents)) / Decimal(repr(float(per_usd)))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return -int(q) if cents < 0 else int(q)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age(entry, now: datetime) -> float:
    """Возраст источника в секундах; нет данных или мусор -> бесконечность."""
    try:
        at = datetime.strptime(entry["at"], ISO).replace(tzinfo=timezone.utc)
    except (TypeError, KeyError, ValueError):
        return math.inf
    return (now - at).total_seconds()


class RateStore:
    """Кэш курсов в файле рядом с базой (rates-cache.json, права 600) и в памяти.
    Формат: {"ecb": {"at": ISO, "rates": {...}}, "cbr": {"at": ISO, "rates": {...}}}."""

    def __init__(self, path, fetch=None, clock=None, log=None, alarm=None):
        self.path = Path(path)
        self._alarm = alarm or (lambda kind, text: None)
        self._fetch = fetch or http_get_json
        self._clock = clock or _now
        self._log = log or (lambda msg: None)
        self._lock = threading.Lock()
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = raw if isinstance(raw, dict) else {}
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def _save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def usable(self) -> dict:
        """Курсы для расчёта: источник не старше MAX_AGE_SECONDS; где есть оба, побеждает более свежий (при равенстве
        ECB)."""
        now = self._clock()
        with self._lock:
            data = self._load()
            out = {}
            for src in sorted(("cbr", "ecb"), key=lambda k: -_age(data.get(k), now)):     # свежий - последним
                entry = data.get(src)
                if isinstance(entry, dict) and _age(entry, now) <= MAX_AGE_SECONDS \
                        and isinstance(entry.get("rates"), dict):
                    out.update({c: v for c, v in entry["rates"].items() if _clean_code(c) and _valid_rate(v)})
            return out

    def needs_refresh(self) -> bool:
        now = self._clock()
        with self._lock:
            data = self._load()
            return any(_age(data.get(src), now) > TTL_SECONDS for src in ("ecb", "cbr"))

    @staticmethod
    def _ref(entry, code, now: datetime):
        """Эталон кода: последний принятый курс из этого источника (не старше PREV_MAX_AGE) или None."""
        if not isinstance(entry, dict):
            return None
        ref = entry.get("ref")
        if isinstance(ref, dict) and isinstance(ref.get(code), list) and len(ref[code]) == 2:
            value, at = ref[code]
            age = _age({"at": at}, now)
        else:                                   # запись старого формата (без ref): эталон - её курсы
            rates = entry.get("rates")
            value, age = (rates.get(code) if isinstance(rates, dict) else None), _age(entry, now)
        return value if _valid_rate(value) and age <= PREV_MAX_AGE else None

    def _plausible(self, src: str, new: dict, now: datetime, other_new: dict | None = None):
        """-> (курсы без неправдоподобных, список отклонённых кодов). other_new - свежий ответ второго источника."""
        with self._lock:
            old = self._load().get(src)
        other_new = other_new or {}
        kept, rejected = {}, []
        for code, value in new.items():
            prev = self._ref(old, code, now)
            other = other_new.get(code)
            if prev is not None:
                if abs(value / prev - 1.0) <= MAX_JUMP:
                    kept[code] = value
                elif _valid_rate(other) and abs(value / other - 1.0) <= MAX_JUMP:
                    kept[code] = value              # сдвиг подтвердил второй источник
                else:
                    rejected.append(code)
            elif _valid_rate(other) and abs(value / other - 1.0) > MAX_JUMP:
                rejected.append(code)               # эталона нет, источники расходятся: не верим ни одному
            else:
                kept[code] = value
        return kept, sorted(rejected)

    def refresh_if_stale(self) -> dict:
        """Обновляет просроченные источники. Возвращает {источник: True/False/None}: True - обновлён,
        False - не получилось (сеть, мусор), None - не требовался. Исключений наружу не бросает.
        Сеть - вне замка: чтение курсов на пути записи не ждёт ответа внешних серверов."""
        result = {}
        now = self._clock()
        with self._lock:
            data = self._load()
            stale = [(src, url, parse) for src, url, parse in
                     (("ecb", ECB_URL, parse_ecb), ("cbr", CBR_URL, parse_cbr))
                     if _age(data.get(src), now) > TTL_SECONDS]
        fresh, got = {}, {}
        for src, url, parse in stale:
            try:
                rates = parse(self._fetch(url))
            except Exception as exc:        # noqa: BLE001 - сеть, JSON, формат: запись это не должно ломать
                self._log("rates %s error: %s" % (src, type(exc).__name__))
                result[src] = False
                continue
            if not rates:
                self._log("rates %s error: empty" % src)
                result[src] = False
                continue
            got[src] = rates
        for src, rates in got.items():
            other_src = "cbr" if src == "ecb" else "ecb"
            rates, rejected = self._plausible(src, rates, now, got.get(other_src))
            if rejected:
                self._log("rates ALARM: %s %s отклонился больше чем на %d%% от прошлого, не применён"
                          % (src, ",".join(rejected), int(MAX_JUMP * 100)))
                for code in rejected:
                    self._alarm("rate:%s:%s" % (src, code),
                                "Курс %s (источник %s) отклонился больше чем на %d%% от прошлого и не применён. "
                                "Новые записи в этой валюте пока без пересчёта в USD; проверьте курс."
                                % (code, src.upper(), int(MAX_JUMP * 100)))
            with self._lock:
                old = self._load().get(src)
            ref = {}                            # эталоны: прежние остаются у отклонённых кодов, у принятых - новые
            if isinstance(old, dict) and isinstance(old.get("ref"), dict):
                ref.update({c: v for c, v in old["ref"].items() if isinstance(v, list) and len(v) == 2})
            if isinstance(old, dict) and isinstance(old.get("rates"), dict):
                for c, v in old["rates"].items():
                    ref.setdefault(c, [v, old.get("at")])
            ref.update({c: [v, now.strftime(ISO)] for c, v in rates.items()})
            fresh[src] = {"at": now.strftime(ISO), "rates": rates, "ref": ref}
            result[src] = True
        for src in ("ecb", "cbr"):
            result.setdefault(src, None)
        if fresh:
            with self._lock:
                self._load().update(fresh)
                try:
                    self._save()
                except OSError as exc:
                    self._log("rates cache write error: %s" % type(exc).__name__)
            self._log("rates refreshed: %s" % ",".join(sorted(fresh)))
        return result


def backfill(conn: sqlite3.Connection, rates, now: datetime | None = None, via: str = "rates-worker") -> int:
    """Дописывает usd_cents записям Кассы, оставшимся без курса. Возвращает, у скольких дописан.

    Трогает только записи source='kassa' и импортированные записи, у которых курс сбросила правка (валюту
    сменили: след в audit). Импортированные записи с NULL остаются как есть. Одна транзакция; чтение кандидатов внутри неё, чтобы не затереть параллельную правку суммы.
    В audit - одна строка со счётчиком и номерами записей (без сумм)."""
    ts = (now or _now()).strftime(ISO)
    where = ("usd_cents IS NULL AND (source='kassa' OR EXISTS (SELECT 1 FROM audit a WHERE a.action='update' "
             "AND a.entity_id=CAST(transactions.id AS TEXT) AND a.detail LIKE '%\"usd_cents\": [%'))")
    if conn.execute("SELECT 1 FROM transactions WHERE " + where + " LIMIT 1").fetchone() is None:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        done = []
        for row in conn.execute("SELECT id, amount_cents, currency FROM transactions WHERE " + where).fetchall():
            usd = convert(row["currency"], row["amount_cents"], rates)
            if usd is None:
                continue
            conn.execute("UPDATE transactions SET usd_cents=? WHERE id=? AND usd_cents IS NULL", (usd, row["id"]))
            done.append(row["id"])
        if done:
            conn.execute("INSERT INTO audit(ts, actor_id, action, entity, entity_id, via, detail) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (ts, None, "usd_backfill", "transaction", None, via,
                          json.dumps({"records": len(done), "ids": done[:BACKFILL_AUDIT_IDS]})))
        conn.execute("COMMIT")
        return len(done)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


class RateWorker(threading.Thread):
    """Фон: раз в TICK_SECONDS обновляет просроченный кэш курсов и дописывает usd_cents. Любая ошибка -
    строка в журнале (только тип) и новая попытка на следующем такте; поток не умирает."""

    def __init__(self, store: RateStore, connect, interval: float = TICK_SECONDS, log=None):
        super().__init__(daemon=True, name="rate-worker")
        self.store = store
        self._connect = connect
        self.interval = interval
        self._stop_event = threading.Event()
        self._log = log or (lambda msg: sys.stderr.write("%s %s\n" % (time.strftime("%d/%b/%Y %H:%M:%S"), msg)))

    def tick(self) -> int:
        """Один такт; возвращает, у скольких записей дописан курс."""
        try:
            if self.store.needs_refresh():
                self.store.refresh_if_stale()
        except Exception as exc:        # noqa: BLE001
            self._log("rates tick error: %s" % type(exc).__name__)
        try:
            rates = self.store.usable()
            if not rates:
                return 0
            conn = self._connect()
            try:
                n = backfill(conn, rates)
            finally:
                conn.close()
            if n:
                self._log("usd backfill: %d" % n)
            return n
        except Exception as exc:        # noqa: BLE001
            self._log("usd backfill error: %s" % type(exc).__name__)
            return 0

    def run(self):
        while not self._stop_event.is_set():
            self.tick()
            self._stop_event.wait(self.interval)

    def stop(self):
        self._stop_event.set()
