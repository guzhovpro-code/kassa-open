"""Запись и справочные данные кассы. Денежная логика только здесь, суммы - целые центы.

Правила:
- запись создаётся только вызовом create_record (его делает POST /api/records после явного «Записать»);
- ключ идемпотентности webapp:<client_id>: повтор того же запроса возвращает ту же запись, второй строки нет;
- запись без способа невозможна (422) ни для расхода, ни для поступления; пусто (NULL) допустимо только у
  импортированных записей (закрытие месяца);
- в закрытый месяц (closed_months) запись не вносится (422 month_closed): итог месяца - закрытие;
- категория не обязательна: нет её в запросе - она определяется по тексту «на что» (заметке) кодом по словарю
  (categorize.py), не распознали - «прочее»; запись из-за категории не блокируется;
- у поступления есть необязательные payer (кто платит, текст до 30 знаков) и segment ('b2b' | 'b2c'); у расхода их нет
  (422 bad_payer / bad_segment); сегмент не задан, а в «на что» стоит отдельное слово B2B / B2C - ставится он;
  изменение после записи идёт через update_record и попадает в audit;
- сумма больше 10 медиан расходов этой валюты за 60 дней (при 10+ записях) и возможный дубль за 10 минут
  требуют подтверждения (409 confirm_required), без подтверждения записи нет.
"""
import json
import re
import sqlite3
import unicodedata
from datetime import date as date_cls, datetime, timedelta, timezone

from . import categorize, currencies, db, money
from . import rates as fx

EXPENSE_CATEGORIES = categorize.OUT_CATEGORIES
INCOME_CATEGORIES = categorize.IN_CATEGORIES
CURRENCIES_MAIN = currencies.MAIN       # основные чипсы - по частоте в истории владельца: TRY, USDT, USD, RUB
CURRENCIES_SECOND = currencies.SECOND   # EUR: под «Ещё», первой в списке
RECENT_CURRENCY_DAYS = 60

# fullmatch, не match с «$»: «$» пропускает завершающий перевод строки. Цифры только ASCII ([0-9], не \d).
CLIENT_ID_RE = re.compile(r"[A-Za-z0-9-]{16,64}")
CURRENCY_RE = re.compile(r"[A-Z]{3,5}")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MAX_BACKDATE_DAYS = 366
OUTLIER_FACTOR = 10
OUTLIER_WINDOW_DAYS = 60
OUTLIER_MIN_RECORDS = 10
DUP_WINDOW_MINUTES = 10
RESERVED_METHOD_TITLES = {"другой", "другая", "другое"}
SEGMENTS = ("b2b", "b2c")
PAYER_MAX_LEN = 30


class RecordError(Exception):
    def __init__(self, status: int, code: str, extra: dict | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra or {}

    def payload(self) -> dict:
        return {"ok": False, "error": self.code, **self.extra}


def _clean_text(value, field: str, max_len: int, required: bool) -> str | None:
    if value is None or value == "":
        if required:
            raise RecordError(422, "missing_" + field)
        return None
    if not isinstance(value, str):
        raise RecordError(422, "bad_" + field)
    text = " ".join(value.split())
    if not text:
        if required:
            raise RecordError(422, "missing_" + field)
        return None
    if len(text) > max_len or any(unicodedata.category(ch).startswith("C") for ch in text):
        raise RecordError(422, "bad_" + field)
    return text


def _parse_segment(value, direction: str) -> str | None:
    """Пусто -> None (без пометки). Только 'b2b' / 'b2c' и только у поступления: расходу сегмент не ставится."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value not in SEGMENTS or direction != "in":
        raise RecordError(422, "bad_segment")
    return value


_SEGMENT_WORD = re.compile(r"(?<!\w)b2([bc])(?!\w)")
_CYR_TO_LAT = str.maketrans({"в": "b", "с": "c"})


def segment_from_note(note) -> str | None:
    """Отдельное слово B2B / B2C в тексте «на что» (любой регистр, кириллические В и С тоже) -> 'b2b' / 'b2c'.
    Оба слова сразу или ни одного -> None."""
    if not note:
        return None
    found = set(_SEGMENT_WORD.findall(note.lower().translate(_CYR_TO_LAT)))
    return "b2" + found.pop() if len(found) == 1 else None


def _parse_payer(value, direction: str) -> str | None:
    payer = _clean_text(value, "payer", PAYER_MAX_LEN, False)
    if payer is not None and direction != "in":
        raise RecordError(422, "bad_payer")
    return payer


def _parse_date(value, today: str) -> str:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise RecordError(422, "bad_date")
    try:
        parsed = date_cls.fromisoformat(value)
    except ValueError:
        raise RecordError(422, "bad_date") from None
    today_d = date_cls.fromisoformat(today)
    if parsed > today_d:
        raise RecordError(422, "date_in_future")
    if (today_d - parsed).days > MAX_BACKDATE_DAYS:
        raise RecordError(422, "date_too_old")
    return value


def _parse_currency(value) -> str:
    """Только ASCII-буквы. Регистр меняем лишь после проверки на ASCII: у str.upper() есть трюки вроде
    «ſ» -> «S» и «ﬁ» -> «FI», из-за которых не-ASCII строка превращалась бы в «валидный» код."""
    if not isinstance(value, str):
        raise RecordError(422, "bad_currency")
    text = value.strip()
    if not text.isascii():
        raise RecordError(422, "bad_currency")
    text = text.upper()
    if not CURRENCY_RE.fullmatch(text):
        raise RecordError(422, "bad_currency")
    return text


def normalize_title(title: str) -> str:
    return " ".join(title.split()).casefold()


def _resolve_method(conn: sqlite3.Connection, direction: str, method, new_method) -> tuple[str | None, str | None]:
    """Возвращает (код способа или None, название нового способа или None). Новый создаётся позже, в транзакции."""
    method = _clean_text(method, "method", 40, False)
    new_title = _clean_text(new_method, "new_method", 30, False)
    if method and new_title:
        raise RecordError(422, "method_conflict")
    if not method and not new_title:
        raise RecordError(422, "method_required")
    if method:
        row = conn.execute("SELECT scope FROM methods WHERE code=? AND active=1", (method,)).fetchone()
        if row is None or row["scope"] not in (direction, "both"):
            raise RecordError(422, "unknown_method")
        return method, None
    if new_title:
        if normalize_title(new_title) in RESERVED_METHOD_TITLES:
            raise RecordError(422, "bad_new_method")
        return None, new_title
    return None, None


def _find_method(conn: sqlite3.Connection, title: str, direction: str) -> str | None:
    """Код способа этого направления с таким названием (без учёта регистра) или None; ничего не создаёт."""
    norm = normalize_title(title)
    for row in conn.execute("SELECT code, title, scope FROM methods WHERE active=1"):
        if row["scope"] in (direction, "both") and normalize_title(row["title"]) == norm:
            return row["code"]
    return None


def _ensure_method(conn: sqlite3.Connection, title: str, direction: str) -> str:
    """Находит способ по названию без учёта регистра или создаёт новую строку в methods (для этого направления;
    если такое название уже есть у другого направления - способ становится общим)."""
    found = _find_method(conn, title, direction)
    if found:
        return found
    norm = normalize_title(title)
    code = "x:" + norm
    nxt = conn.execute("SELECT COALESCE(MAX(sort),0)+10 FROM methods").fetchone()[0]
    conn.execute("INSERT OR IGNORE INTO methods(code, title, sort, active, scope) VALUES (?,?,?,1,?)",
                 (code, " ".join(title.split()), nxt, direction))
    conn.execute("UPDATE methods SET scope='both' WHERE code=? AND scope<>?", (code, direction))
    conn.execute("UPDATE methods SET active=1 WHERE code=?", (code,))        # название снова названо: способ возвращается
    return code


def _median2(values: list[int]) -> int:
    """Удвоенная медиана целым числом (чтобы не уходить в дроби)."""
    s = sorted(values)
    n = len(s)
    return 2 * s[n // 2] if n % 2 else s[n // 2 - 1] + s[n // 2]


def _outlier(conn: sqlite3.Connection, currency: str, cents: int, today: str, exclude_id: int | None = None) -> bool:
    since = (date_cls.fromisoformat(today) - timedelta(days=OUTLIER_WINDOW_DAYS)).isoformat()
    vals = [r[0] for r in conn.execute(
        "SELECT amount_cents FROM transactions WHERE direction='out' AND status='active' AND closed_by IS NULL "
        "AND currency=? AND amount_cents>0 AND occurred_on>=? AND occurred_on<=? AND id IS NOT ?",
        (currency, since, today, exclude_id))]
    if len(vals) < OUTLIER_MIN_RECORDS:
        return False
    return 2 * cents > OUTLIER_FACTOR * _median2(vals)


def _possible_duplicate(conn: sqlite3.Connection, direction: str, cents: int, currency: str, now: datetime) -> bool:
    since = (now - timedelta(minutes=DUP_WINDOW_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return conn.execute(
        "SELECT 1 FROM transactions WHERE direction=? AND amount_cents=? AND currency=? AND status='active' "
        "AND source <> 'month-close' AND created_at>=? LIMIT 1",
        (direction, cents, currency, since)).fetchone() is not None


def create_record(conn: sqlite3.Connection, actor_id: int | str, payload: dict, via: str = "webapp",
                  now: datetime | None = None, fx_rates: dict | None = None, idem_key: str | None = None,
                  guard=None, confirm_gate=None, on_created=None) -> dict:
    """fx_rates - кэш курсов {валюта: единиц за 1 USD} (rates.RateStore.usable()); путь записи в сеть не ходит.
    USD/USDT/USDC считаются 1:1; нет курса - usd_cents NULL, запись сохраняется, курс допишет фоновый дорасчёт.
    idem_key задан только кодом бэкенда (черновик из чата: 'card:<id>'); из запроса формы он не берётся.
    guard(conn), confirm_gate(conn, warnings, confirm) -> confirm и on_created(conn, rec_id, info) выполняются внутри
    той же транзакции (проверка черновика, учёт показанных предупреждений, закрытие черновика и правка карточки
    в чате: либо всё вместе, либо ничего)."""
    now = now or datetime.now(timezone.utc)
    today = db.today_local(now)
    if not isinstance(payload, dict):
        raise RecordError(422, "bad_payload")
    client_id = payload.get("client_id")
    if idem_key is None and (not isinstance(client_id, str) or not CLIENT_ID_RE.fullmatch(client_id)):
        raise RecordError(422, "bad_client_id")
    direction = payload.get("direction")
    if direction not in ("in", "out"):
        raise RecordError(422, "bad_direction")
    try:
        cents = money.parse_amount_to_cents(payload.get("amount"))
    except money.MoneyError as exc:
        raise RecordError(422, exc.args[0]) from None
    currency = _parse_currency(payload.get("currency"))
    note = _clean_text(payload.get("note"), "note", 200, False)
    category = _clean_text(payload.get("category"), "category", 40, False)
    category_auto = category is None
    if category_auto:
        category = categorize.categorize(note, direction)
    payer = _parse_payer(payload.get("payer"), direction)
    segment = _parse_segment(payload.get("segment"), direction)
    if segment is None and direction == "in":
        segment = segment_from_note(note)           # чипсы не выбраны: сегмент по слову B2B / B2C в тексте
    occurred_on = _parse_date(payload.get("date"), today)
    method_code, new_title = _resolve_method(conn, direction, payload.get("method"), payload.get("new_method"))
    confirm = payload.get("confirm") or []
    if not isinstance(confirm, list) or not all(isinstance(x, str) for x in confirm):
        raise RecordError(422, "bad_confirm")

    if idem_key is None:
        idem_key = "webapp:" + client_id
    created_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT id, direction, amount_cents, currency, occurred_on, category, note, method, payer, "
                           "segment FROM transactions WHERE idem_key=?", (idem_key,)).fetchone()
        if row is not None:
            # повтор того же client_id - тот же запрос, только если совпало всё, что вводит пользователь
            want_method = method_code if not new_title else _find_method(conn, new_title, direction)
            if (row["direction"], row["amount_cents"], row["currency"], row["occurred_on"], row["category"],
                    row["note"], row["method"], row["payer"], row["segment"]) != \
                    (direction, cents, currency, occurred_on, category, note, want_method, payer, segment):
                raise RecordError(409, "idem_conflict")
            conn.execute("COMMIT")
            return {"ok": True, "id": row["id"], "duplicate_request": True, "category": row["category"]}

        if guard is not None:
            guard(conn)
        if occurred_on[:7] in db.closed_months(conn):
            raise RecordError(422, "month_closed")
        warnings = []
        if direction == "out" and _outlier(conn, currency, cents, today):
            warnings.append("outlier")
        if _possible_duplicate(conn, direction, cents, currency, now):
            warnings.append("possible_duplicate")
        if confirm_gate is not None:
            confirm = confirm_gate(conn, warnings, confirm)
        missing = [w for w in warnings if w not in confirm]
        if missing:
            raise RecordError(409, "confirm_required", {"warnings": missing})

        if new_title:
            method_code = _ensure_method(conn, new_title, direction)
        cur = conn.execute(
            "INSERT INTO transactions(idem_key, direction, amount_cents, currency, usd_cents, category, note, method, "
            "occurred_on, created_at, status, source, via, actor_id, payer, segment) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'active','kassa',?,?,?,?)",
            (idem_key, direction, cents, currency, fx.convert(currency, cents, fx_rates), category, note,
             method_code, occurred_on, created_at, via, str(actor_id), payer, segment))
        rec_id = cur.lastrowid
        detail = {"new_method": bool(new_title), "backdated": occurred_on != today, "category_auto": category_auto}
        if payer is not None:
            detail["payer"] = True              # имя плательщика в audit не пишем, только факт
        if segment is not None:
            detail["segment"] = segment
        conn.execute("INSERT INTO audit(ts, actor_id, action, entity, entity_id, via, detail) VALUES (?,?,?,?,?,?,?)",
                     (created_at, str(actor_id), "create", "transaction", str(rec_id), via,
                      json.dumps(detail, ensure_ascii=False)))
        if on_created is not None:
            on_created(conn, rec_id, {"direction": direction, "cents": cents, "currency": currency, "note": note,
                                      "category": category, "date": occurred_on, "method": method_code})
        conn.execute("COMMIT")
        return {"ok": True, "id": rec_id, "duplicate_request": False, "category": category}
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _load_for_change(conn: sqlite3.Connection, rec_id) -> sqlite3.Row:
    if not isinstance(rec_id, int) or isinstance(rec_id, bool) or rec_id <= 0:
        raise RecordError(404, "not_found")
    row = conn.execute("SELECT id, direction, status, category, method, amount_cents, amount_orig, currency, "
                       "usd_cents, note, closed_by, source, payer, segment, exchange_id FROM transactions WHERE id=?",
                       (rec_id,)).fetchone()
    if row is None:
        raise RecordError(404, "not_found")
    if row["exchange_id"] is not None:
        raise RecordError(409, "linked_to_exchange")     # комиссия обмена: правится, отменяется и возвращается с обменом
    if row["closed_by"] is not None or row["source"] == "month-close":
        raise RecordError(409, "record_closed")        # закрытый месяц: запись менять нельзя
    return row


def _set_status(conn: sqlite3.Connection, actor_id, rec_id, new_status: str, via: str,
                now: datetime | None) -> dict:
    """Мягкое удаление и возврат: строка остаётся, меняются status и cancelled_at. Повтор того же действия
    ничего не меняет и не пишет в audit (идемпотентно)."""
    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _load_for_change(conn, rec_id)
        changed = row["status"] != new_status
        if changed:
            conn.execute("UPDATE transactions SET status=?, cancelled_at=? WHERE id=?",
                         (new_status, ts if new_status == "cancelled" else None, rec_id))
            conn.execute("INSERT INTO audit(ts, actor_id, action, entity, entity_id, via, detail) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (ts, str(actor_id), "cancel" if new_status == "cancelled" else "restore", "transaction",
                          str(rec_id), via, json.dumps({"from": row["status"], "to": new_status})))
        conn.execute("COMMIT")
        return {"ok": True, "id": rec_id, "status": new_status, "changed": changed}
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def cancel_record(conn, actor_id, rec_id, via: str = "webapp", now: datetime | None = None) -> dict:
    return _set_status(conn, actor_id, rec_id, "cancelled", via, now)


def restore_record(conn, actor_id, rec_id, via: str = "webapp", now: datetime | None = None) -> dict:
    return _set_status(conn, actor_id, rec_id, "active", via, now)


EDITABLE_FIELDS = {"amount", "currency", "category", "note", "method", "new_method", "payer", "segment"}
UPDATE_KEYS = EDITABLE_FIELDS | {"confirm"}


def _rescale_usd(old_usd: int | None, old_cents: int, new_cents: int) -> int | None:
    """Снимок курса (usd_cents) при смене суммы в той же валюте: курс записи тот же, сумма в долларах
    пересчитывается пропорционально (округление половина-вверх по модулю). Курса не было или старая
    сумма не положительная - пересчитать не из чего, остаётся NULL."""
    if old_usd is None or old_cents <= 0:
        return None
    q, r = divmod(abs(old_usd) * new_cents, old_cents)
    if 2 * r >= old_cents:
        q += 1
    return -q if old_usd < 0 else q


def update_record(conn: sqlite3.Connection, actor_id, rec_id, payload, via: str = "webapp",
                  now: datetime | None = None, fx_rates: dict | None = None) -> dict:
    """Правка записи: сумма, валюта, категория, «на что» (заметка), способ, у поступления ещё плательщик и сегмент
    (null / пусто снимает пометку). Дата не меняется.

    - Передаются только изменяемые поля; значение, равное текущему, ничего не меняет (повтор безвреден).
    - Каждое реальное изменение - одна строка audit: кто, когда, вход и для каждого поля [было, стало].
    - Курс в долларах (usd_cents - снимок на момент записи): USD/USDT/USDC - всегда 1:1; сумма поменялась,
      валюта та же - пересчитывается пропорционально (курс записи тот же), нет прежнего курса - берётся кэш
      fx_rates; валюта поменялась - по кэшу курсов (нет курса - NULL, дорасчёт позже);
      amount_orig (исходная строка суммы импорта) при смене суммы сбрасывается.
    - Отчёты считаются из transactions на лету, отдельного пересчёта нет.
    - Расход, который при новой сумме и валюте больше 10 медиан, требует подтверждения («confirm»), как при записи."""
    if not isinstance(payload, dict) or not payload:
        raise RecordError(422, "bad_payload")
    if not set(payload) <= UPDATE_KEYS:
        raise RecordError(422, "field_not_editable")
    confirm = payload.get("confirm") or []
    if not isinstance(confirm, list) or not all(isinstance(x, str) for x in confirm):
        raise RecordError(422, "bad_confirm")
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    new_category = _clean_text(payload["category"], "category", 40, True) if "category" in payload else None
    new_note = _clean_text(payload["note"], "note", 200, False) if "note" in payload else None
    try:
        new_cents = money.parse_amount_to_cents(payload["amount"]) if "amount" in payload else None
    except money.MoneyError as exc:
        raise RecordError(422, exc.args[0]) from None
    new_currency = _parse_currency(payload["currency"]) if "currency" in payload else None
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _load_for_change(conn, rec_id)
        if row["status"] != "active":
            raise RecordError(409, "record_cancelled")
        changes = {}
        extra = {}                      # производные поля: в audit есть, в ответе нет
        cents, currency = row["amount_cents"], row["currency"]
        if new_cents is not None and new_cents != cents:
            changes["amount_cents"] = [cents, new_cents]
            cents = new_cents
        if new_currency is not None and new_currency != currency:
            changes["currency"] = [currency, new_currency]
            currency = new_currency
        if "category" in payload and new_category != row["category"]:
            changes["category"] = [row["category"], new_category]
        if "note" in payload and new_note != row["note"]:
            changes["note"] = [row["note"], new_note]
        if "payer" in payload:
            new_payer = _parse_payer(payload["payer"], row["direction"])
            if new_payer != row["payer"]:
                changes["payer"] = [row["payer"], new_payer]
        if "segment" in payload:
            new_segment = _parse_segment(payload["segment"], row["direction"])
            if new_segment != row["segment"]:
                changes["segment"] = [row["segment"], new_segment]
        if "method" in payload or "new_method" in payload:
            method_code, new_title = _resolve_method(conn, row["direction"], payload.get("method"),
                                                     payload.get("new_method"))
            if new_title:                           # при откате ниже (confirm_required) создание откатится вместе
                method_code = _ensure_method(conn, new_title, row["direction"])
            if method_code != row["method"]:
                changes["method"] = [row["method"], method_code]
        if "amount_cents" in changes or "currency" in changes:
            if currency in fx.PARITY or "currency" in changes:
                new_usd = fx.convert(currency, cents, fx_rates)
            else:
                new_usd = _rescale_usd(row["usd_cents"], row["amount_cents"], cents)
                if new_usd is None:
                    new_usd = fx.convert(currency, cents, fx_rates)
            if new_usd != row["usd_cents"]:
                extra["usd_cents"] = [row["usd_cents"], new_usd]
            if "amount_cents" in changes and row["amount_orig"] is not None:
                extra["amount_orig"] = [row["amount_orig"], None]
            if (row["direction"] == "out" and "outlier" not in confirm
                    and _outlier(conn, currency, cents, db.today_local(now), exclude_id=rec_id)):
                raise RecordError(409, "confirm_required", {"warnings": ["outlier"]})
        if changes:
            every = {**changes, **extra}
            sets = ", ".join("%s=?" % field for field in every)
            conn.execute("UPDATE transactions SET " + sets + " WHERE id=?",
                         [new for _, new in every.values()] + [rec_id])
            conn.execute("INSERT INTO audit(ts, actor_id, action, entity, entity_id, via, detail) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (ts, str(actor_id), "update", "transaction", str(rec_id), via,
                          json.dumps({"changed": every}, ensure_ascii=False)))
        conn.execute("COMMIT")
        return {"ok": True, "id": rec_id, "changed": sorted(k.replace("_cents", "") for k in changes)}
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def recent_currencies(conn: sqlite3.Connection, today: str) -> list[str]:
    """Валюты активных записей за 60 дней, которых нет среди основных чипсов; чаще используемые первыми."""
    since = (date_cls.fromisoformat(today) - timedelta(days=RECENT_CURRENCY_DAYS)).isoformat()
    rows = conn.execute("SELECT currency, COUNT(*) n FROM transactions WHERE status='active' AND closed_by IS NULL "
                        "AND occurred_on>=? AND occurred_on<=? GROUP BY currency ORDER BY n DESC, currency",
                        (since, today))
    return [r["currency"] for r in rows if r["currency"] not in CURRENCIES_MAIN]


METHOD_TOP = 3                  # сколько способов показывать чипсами на главном экране
METHOD_USAGE_DAYS = 90


def method_order(conn: sqlite3.Connection, today: str) -> dict:
    """Порядок способов по направлению: чаще используемые за 90 дней (без записей, перекрытых закрытием),
    затем остальные в порядке списка (поле sort: порядок, в котором их добавляли)."""
    since = (date_cls.fromisoformat(today) - timedelta(days=METHOD_USAGE_DAYS)).isoformat()
    active = {r["code"]: (r["scope"], r["sort"]) for r in conn.execute(
        "SELECT code, scope, sort FROM methods WHERE active=1")}
    out = {}
    for direction in ("out", "in"):
        usage = {r["method"]: r["n"] for r in conn.execute(
            "SELECT method, COUNT(*) n FROM transactions WHERE direction=? AND status='active' AND closed_by IS NULL "
            "AND method IS NOT NULL AND occurred_on>=? AND occurred_on<=? GROUP BY method", (direction, since, today))}
        codes = [c for c, (scope, _) in active.items() if scope in (direction, "both")]
        codes.sort(key=lambda c: (-usage.get(c, 0), active[c][1], c))
        out[direction] = codes
    return out


def meta(conn: sqlite3.Connection, test: bool, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    methods = [{"code": r["code"], "title": r["title"], "scope": r["scope"]}
               for r in conn.execute("SELECT code, title, scope FROM methods WHERE active=1 ORDER BY sort, title")]
    last = {}
    last_method = {}
    for direction in ("out", "in"):
        r = conn.execute("SELECT currency FROM transactions WHERE direction=? AND status='active' "
                         "AND source <> 'month-close' AND closed_by IS NULL "
                         "ORDER BY created_at DESC, id DESC LIMIT 1", (direction,)).fetchone()
        last[direction] = r["currency"] if r else "TRY"
        # последний способ, выбранный в Кассе (не импорт): подставляется в форму при следующем открытии
        r = conn.execute("SELECT t.method FROM transactions t JOIN methods m ON m.code=t.method "
                         "WHERE t.direction=? AND t.status='active' AND t.source='kassa' AND t.method IS NOT NULL "
                         "AND m.active=1 AND m.scope IN (?, 'both') "
                         "ORDER BY t.created_at DESC, t.id DESC LIMIT 1", (direction, direction)).fetchone()
        last_method[direction] = r["method"] if r else None
    today = db.today_local(now)
    return {"test": test, "today": today, "methods": methods,
            "method_order": method_order(conn, today), "method_top": METHOD_TOP, "last_method": last_method,
            "closed_months": sorted(db.closed_months(conn)),
            "categories": {"out": EXPENSE_CATEGORIES, "in": INCOME_CATEGORIES},
            "currencies": {"main": CURRENCIES_MAIN, "second": CURRENCIES_SECOND,
                           "recent": recent_currencies(conn, today)},
            "defaults": {"out": categorize.DEFAULT_OUT, "in": categorize.DEFAULT_IN},
            "last_currency": last}
