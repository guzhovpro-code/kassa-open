"""Обмен валют (схема 7, задание 10.11.2026). Денежная логика обмена только здесь, суммы - целые центы.

Правила:
- обмен - отдельный вид записи: отдано (сумма, валюта, способ), получено 1-3 валюты (сумма, валюта, способ, необязательно
  доля отданного). В доходы и расходы обмен не попадает (v_legacy и отчёты его не видят);
- курс в базе не хранится: он считается при чтении как «отдано за эту валюту / получено». Доля не указана - код делит
  остаток отданного между ногами пропорционально их стоимости в USD по кэшу курсов (rates.RateStore.usable()), нога
  помечается part_estimated=1. Нет курса для одной из валют - доли неуказанных ног NULL (курс «не посчитан»), запись
  всё равно сохраняется;
- комиссия - обычный расход (категория «Комиссия обмена», способ = способ отдачи, если не указан другой) со ссылкой
  transactions.exchange_id; создаётся в той же транзакции, отмена обмена отменяет комиссию, возврат возвращает. Комиссию,
  связанную с обменом, нельзя менять через /api/records (records._load_for_change): только вместе с обменом;
- запись только вызовом create_exchange (его делает POST /api/exchanges с подписью Telegram); ключ идемпотентности
  webapp:<client_id> (для черновика из чата card:<id>): повтор того же запроса возвращает тот же обмен;
- в закрытый месяц (closed_months) обмен не вносится, не правится, не отменяется и не возвращается (422 month_closed);
- суммы в audit не пишутся: только факты («legs», «fee», «estimated», список изменённых полей).
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from . import db, money
from . import rates as fx
from .records import CLIENT_ID_RE, RecordError, _clean_text, _parse_currency, _parse_date

FEE_CATEGORY = "Комиссия обмена"
MAX_LEGS = 3
EDITABLE_KEYS = {"date", "give", "get", "fee", "note"}
SYMBOLS = {"RUB": "₽", "TRY": "₺", "USD": "$", "EUR": "€", "GBP": "£"}


# --- разбор запроса ------------------------------------------------------------------------------------------------
def _amount(value, error: str = "") -> int:
    try:
        return money.parse_amount_to_cents(value)
    except money.MoneyError as exc:
        raise RecordError(422, error or exc.args[0]) from None


def _method(conn: sqlite3.Connection, value, direction: str) -> str | None:
    """Код способа или None (способ у обмена необязателен). Способ должен быть активным и подходить направлению:
    отдаём - расход, получаем - поступление."""
    code = _clean_text(value, "method", 40, False)
    if code is None:
        return None
    row = conn.execute("SELECT scope FROM methods WHERE code=? AND active=1", (code,)).fetchone()
    if row is None or row["scope"] not in (direction, "both"):
        raise RecordError(422, "unknown_method")
    return code


def _parse_give(conn, raw) -> dict:
    if not isinstance(raw, dict):
        raise RecordError(422, "bad_give")
    return {"cents": _amount(raw.get("amount")), "currency": _parse_currency(raw.get("currency")),
            "method": _method(conn, raw.get("method"), "out")}


def _parse_legs(conn, raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise RecordError(422, "bad_get")
    if len(raw) > MAX_LEGS:
        raise RecordError(422, "too_many_legs")
    legs, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise RecordError(422, "bad_get")
        cur = _parse_currency(item.get("currency"))
        if cur in seen:
            raise RecordError(422, "duplicate_get_currency")
        seen.add(cur)
        part = item.get("part")
        legs.append({"cents": _amount(item.get("amount")), "currency": cur,
                     "method": _method(conn, item.get("method"), "in"),
                     "user_part": None if part in (None, "") else _amount(part, "bad_part")})
    return legs


def _parse_fee(conn, raw, give: dict) -> dict | None:
    """Комиссия: пусто (None, {} или без суммы) - комиссии нет. Валюта по умолчанию - валюта отдачи, способ - способ
    отдачи; способа нет ни там ни там - 422 method_required (расход без способа не пишется)."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise RecordError(422, "bad_fee")
    if raw.get("amount") in (None, ""):
        return None
    cur = _parse_currency(raw["currency"]) if raw.get("currency") not in (None, "") else give["currency"]
    method = _method(conn, raw.get("method"), "out") or give["method"]
    if method is None:
        raise RecordError(422, "method_required")
    return {"cents": _amount(raw.get("amount")), "currency": cur, "method": method}


def _check_currencies(give: dict, legs: list[dict]) -> None:
    if any(leg["currency"] == give["currency"] for leg in legs):
        raise RecordError(422, "same_currency")


def allocate(give_cents: int, legs: list[dict], fx_rates: dict | None) -> None:
    """Заполняет у каждой ноги part (доля отданного, центы или None) и estimated (0/1).
    Одна нога: доля = всё отданное. Указанные доли: сумма не больше отданного, указаны все - ровно отданное. Остаток
    делится между неуказанными по стоимости полученного в USD (наибольший остаток, сумма долей = остатку точно)."""
    if len(legs) == 1:
        if legs[0]["user_part"] not in (None, give_cents):
            raise RecordError(422, "parts_mismatch")
        legs[0]["part"], legs[0]["estimated"] = give_cents, 0
        return
    specified = sum(leg["user_part"] for leg in legs if leg["user_part"] is not None)
    if specified > give_cents:
        raise RecordError(422, "part_too_large")
    free = [leg for leg in legs if leg["user_part"] is None]
    for leg in legs:
        leg["part"], leg["estimated"] = leg["user_part"], 0 if leg["user_part"] is not None else 1
    if not free:
        if specified != give_cents:
            raise RecordError(422, "parts_mismatch")
        return
    rest = give_cents - specified
    if rest <= 0:
        raise RecordError(422, "parts_mismatch")
    weights = [fx.convert(leg["currency"], leg["cents"], fx_rates) for leg in free]
    if any(w is None or w <= 0 for w in weights):
        return                                          # нет курса: доли остаются NULL, курс не посчитан
    total = sum(weights)
    if rest < len(free):
        return                                          # на каждую неуказанную ногу не набирается и по центу
    shares = [rest * w // total for w in weights]
    order = sorted(range(len(free)), key=lambda i: (-(rest * weights[i] % total), i))
    for i in order[:rest - sum(shares)]:
        shares[i] += 1
    for i in [i for i, share in enumerate(shares) if share <= 0]:
        # доля округлилась в ноль центов (крошечная нога): по центу ей, сумма долей остаётся равной остатку
        shares[max(range(len(shares)), key=lambda j: shares[j])] -= 1
        shares[i] = 1
    for leg, share in zip(free, shares):
        leg["part"] = share


def parse_spec(conn, payload: dict, fx_rates: dict | None, today: str) -> dict:
    """Запрос (создание) -> {date, note, give, legs, fee}; проверки формы, без обращения к закрытым месяцам."""
    note = _clean_text(payload.get("note"), "note", 200, False)
    give = _parse_give(conn, payload.get("give"))
    legs = _parse_legs(conn, payload.get("get"))
    _check_currencies(give, legs)
    fee = _parse_fee(conn, payload.get("fee"), give)
    allocate(give["cents"], legs, fx_rates)
    return {"date": _parse_date(payload.get("date"), today), "note": note, "give": give, "legs": legs, "fee": fee}


def preview(payload: dict, fx_rates: dict | None) -> list[dict] | None:
    """Ноги черновика с долями и оценкой для текста карточки (без базы и проверки способов); None - не разобралось."""
    try:
        give_cents = _amount(payload["give"]["amount"])
        legs = []
        for item in payload["get"]:
            part = item.get("part")
            legs.append({"cents": _amount(item["amount"]), "currency": _parse_currency(item["currency"]),
                         "user_part": None if part in (None, "") else _amount(part, "bad_part")})
        allocate(give_cents, legs, fx_rates)
        return legs
    except (RecordError, KeyError, TypeError):
        return None


# --- курс и показ --------------------------------------------------------------------------------------------------
def rate_of(part_cents, get_cents) -> Decimal | None:
    """Сколько отданной валюты за единицу полученной (центы к центам: отношение то же). None - доли нет."""
    if part_cents is None or not get_cents:
        return None
    return Decimal(part_cents) / Decimal(get_cents)


def _round(value: Decimal | None) -> float | None:
    return None if value is None else float(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def fmt_cents(cents: int) -> str:
    whole, frac = divmod(cents, 100)
    text = "{:,}".format(whole).replace(",", " ")
    return text if frac == 0 else "%s,%02d" % (text, frac)


def money_text(cents: int, currency: str) -> str:
    return "%s %s" % (fmt_cents(cents), SYMBOLS.get(currency, currency))


def fmt_rate(rate: Decimal) -> str:
    places = 2 if rate >= 100 else 4
    text = format(rate.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), "f")
    return text.rstrip("0").rstrip(".").replace(".", ",") if "." in text else text


def rate_line(give_currency: str, leg: dict) -> str:
    """«1 ₺ = 2,68 ₽» (с «≈» у оценочной доли); доли нет - «курс не посчитан»."""
    rate = rate_of(leg.get("part"), leg["cents"])
    cur = SYMBOLS.get(leg["currency"], leg["currency"])
    if rate is None:
        return "1 %s: курс не посчитан" % cur
    return "1 %s %s %s %s" % (cur, "≈" if leg.get("estimated") else "=", fmt_rate(rate),
                              SYMBOLS.get(give_currency, give_currency))


# --- чтение --------------------------------------------------------------------------------------------------------
def _method_title(conn, code):
    if not code:
        return None
    row = conn.execute("SELECT title FROM methods WHERE code=?", (code,)).fetchone()
    return row["title"] if row else code


def _item(conn: sqlite3.Connection, row, closed: set | None = None) -> dict:
    closed = db.closed_months(conn) if closed is None else closed
    legs = []
    for leg in conn.execute("SELECT * FROM exchange_legs WHERE exchange_id=? ORDER BY id", (row["id"],)):
        rate = rate_of(leg["give_part_cents"], leg["get_cents"])
        legs.append({"get_cents": leg["get_cents"], "get_currency": leg["get_currency"],
                     "get_method": leg["get_method"], "get_method_title": _method_title(conn, leg["get_method"]),
                     "give_part_cents": leg["give_part_cents"], "estimated": bool(leg["part_estimated"]),
                     "rate": _round(rate), "rate_inv": _round(None if rate is None else 1 / rate)})
    fee = conn.execute("SELECT id, amount_cents, currency, status, method FROM transactions WHERE exchange_id=? "
                       "AND exchange_removed IS NULL ORDER BY id LIMIT 1", (row["id"],)).fetchone()
    return {"id": row["id"], "occurred_on": row["occurred_on"], "created_at": row["created_at"],
            "status": row["status"], "via": row["via"], "note": row["note"],
            "give": {"cents": row["give_cents"], "currency": row["give_currency"], "method": row["give_method"],
                     "method_title": _method_title(conn, row["give_method"])},
            "legs": legs,
            "fee": None if fee is None else {"tx_id": fee["id"], "cents": fee["amount_cents"],
                                             "currency": fee["currency"], "status": fee["status"],
                                             "method": fee["method"]},
            "closed": row["occurred_on"][:7] in closed}


def get_item(conn: sqlite3.Connection, ex_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM exchanges WHERE id=?", (ex_id,)).fetchone()
    return _item(conn, row) if row else None


def list_month(conn: sqlite3.Connection, month: str | None, today: str) -> dict:
    """Обмены месяца (и действующие, и отмененные), новые сверху. Месяц - как у отчёта (будущее и мусор: 422)."""
    from .reports import period_bounds        # reports импортирует records, а не наоборот: цикла нет
    first, last = period_bounds("month", month, today)
    closed = db.closed_months(conn)
    rows = conn.execute("SELECT * FROM exchanges WHERE occurred_on>=? AND occurred_on<=? "
                        "ORDER BY occurred_on DESC, created_at DESC, id DESC", (first, last)).fetchall()
    return {"ok": True, "month": first[:7], "count": sum(1 for r in rows if r["status"] == "active"),
            "items": [_item(conn, r, closed) for r in rows]}


def count_active(conn: sqlite3.Connection, first: str, last: str) -> int:
    """Число действующих обменов за период (строка «Обменов: N» в отчётах, без сумм)."""
    return conn.execute("SELECT COUNT(*) FROM exchanges WHERE status='active' AND occurred_on>=? AND occurred_on<=?",
                        (first, last)).fetchone()[0]


# --- запись --------------------------------------------------------------------------------------------------------
def _audit(conn, ts, actor_id, action, ex_id, via, detail):
    conn.execute("INSERT INTO audit(ts, actor_id, action, entity, entity_id, via, detail) VALUES (?,?,?,?,?,?,?)",
                 (ts, str(actor_id), action, "exchange", str(ex_id), via, json.dumps(detail, ensure_ascii=False)))


def _insert_legs(conn, ex_id, legs):
    for leg in legs:
        conn.execute("INSERT INTO exchange_legs(exchange_id, get_cents, get_currency, get_method, give_part_cents, "
                     "part_estimated) VALUES (?,?,?,?,?,?)",
                     (ex_id, leg["cents"], leg["currency"], leg["method"], leg["part"], leg["estimated"]))


def _insert_fee(conn, ex_id, idem_key, fee, occurred_on, created_at, via, actor_id, fx_rates):
    cur = conn.execute(
        "INSERT INTO transactions(idem_key, direction, amount_cents, currency, usd_cents, category, note, method, "
        "occurred_on, created_at, status, source, via, actor_id, exchange_id) "
        "VALUES (?,'out',?,?,?,?,?,?,?,?,'active','kassa',?,?,?)",
        ("exchange-fee:" + idem_key, fee["cents"], fee["currency"], fx.convert(fee["currency"], fee["cents"], fx_rates),
         FEE_CATEGORY, "обмен №%d" % ex_id, fee["method"], occurred_on, created_at, via, str(actor_id), ex_id))
    return cur.lastrowid


def _same_request(conn, row, spec) -> bool:
    """Повтор того же client_id - тот же запрос, только если совпало всё, что вводит пользователь."""
    multi = len(spec["legs"]) > 1
    want = [(leg["cents"], leg["currency"], leg["method"], leg["user_part"] if multi else None)
            for leg in spec["legs"]]
    have = [(leg["get_cents"], leg["get_currency"], leg["get_method"],
             leg["give_part_cents"] if multi and not leg["part_estimated"] else None)
            for leg in conn.execute("SELECT * FROM exchange_legs WHERE exchange_id=? ORDER BY id", (row["id"],))]
    fee = conn.execute("SELECT amount_cents, currency, method FROM transactions WHERE exchange_id=? "
                       "AND exchange_removed IS NULL", (row["id"],)).fetchone()
    want_fee = None if spec["fee"] is None else (spec["fee"]["cents"], spec["fee"]["currency"], spec["fee"]["method"])
    have_fee = None if fee is None else (fee["amount_cents"], fee["currency"], fee["method"])
    return ((row["occurred_on"], row["give_cents"], row["give_currency"], row["give_method"], row["note"], have,
             have_fee) == (spec["date"], spec["give"]["cents"], spec["give"]["currency"], spec["give"]["method"],
                           spec["note"], want, want_fee))


def _check_existing_fee(conn, fee_id, fee: dict) -> None:
    row = conn.execute("SELECT direction, status, amount_cents, currency, exchange_id, closed_by, source "
                       "FROM transactions WHERE id=?", (fee_id,)).fetchone()
    if row is None:
        raise RecordError(404, "fee_row_not_found")
    if (row["direction"] != "out" or row["status"] != "active" or row["exchange_id"] is not None
            or row["closed_by"] is not None or row["source"] == "month-close"):
        raise RecordError(409, "fee_row_not_linkable")
    if row["amount_cents"] != fee["cents"] or row["currency"] != fee["currency"]:
        raise RecordError(422, "fee_row_mismatch")


def create_exchange(conn: sqlite3.Connection, actor_id, payload: dict, via: str = "webapp",
                    now: datetime | None = None, fx_rates: dict | None = None, idem_key: str | None = None,
                    guard=None, on_created=None, existing_fee_id: int | None = None) -> dict:
    """payload: {client_id, date, give: {amount, currency, method?}, get: [{amount, currency, method?, part?}, ...],
    fee?: {amount, currency?, method?}, note?}. Курсы fx_rates - кэш {валюта: единиц за 1 USD}, в сеть путь записи не
    ходит. idem_key задан только кодом бэкенда (черновик из чата 'card:<id>', перенос обмена через via='splinter');
    из запроса формы он не берётся. guard(conn) и on_created(conn, ex_id) выполняются внутри той же транзакции.
    existing_fee_id - перенос уже записанного обмена: комиссия лежит в transactions отдельной записью, новая не
    создаётся, а у этой в той же транзакции ставится exchange_id (запись должна быть действующим расходом без обмена,
    не из закрытого месяца, сумма и валюта как у fee в payload; иначе 409 fee_row_not_linkable / 422 fee_row_mismatch).
    -> {"ok", "id", "duplicate_request", "estimated", "fee_id"}."""
    now = now or datetime.now(timezone.utc)
    today = db.today_local(now)
    if not isinstance(payload, dict):
        raise RecordError(422, "bad_payload")
    client_id = payload.get("client_id")
    if idem_key is None and (not isinstance(client_id, str) or not CLIENT_ID_RE.fullmatch(client_id)):
        raise RecordError(422, "bad_client_id")
    spec = parse_spec(conn, payload, fx_rates, today)
    if existing_fee_id is not None and spec["fee"] is None:
        raise RecordError(422, "fee_row_mismatch")      # привязывать нечего: в payload комиссии нет
    if idem_key is None:
        idem_key = "webapp:" + client_id
    created_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM exchanges WHERE idem_key=?", (idem_key,)).fetchone()
        if row is not None:
            if not _same_request(conn, row, spec):
                raise RecordError(409, "idem_conflict")
            fee = conn.execute("SELECT id FROM transactions WHERE exchange_id=? AND exchange_removed IS NULL",
                               (row["id"],)).fetchone()
            conn.execute("COMMIT")
            return {"ok": True, "id": row["id"], "duplicate_request": True,
                    "estimated": any(leg["estimated"] for leg in spec["legs"]), "fee_id": fee["id"] if fee else None}
        if guard is not None:
            guard(conn)
        if spec["date"][:7] in db.closed_months(conn):
            raise RecordError(422, "month_closed")
        if existing_fee_id is not None:
            _check_existing_fee(conn, existing_fee_id, spec["fee"])
        cur = conn.execute(
            "INSERT INTO exchanges(idem_key, occurred_on, created_at, give_cents, give_currency, give_method, note, "
            "status, via, actor_id) VALUES (?,?,?,?,?,?,?,'active',?,?)",
            (idem_key, spec["date"], created_at, spec["give"]["cents"], spec["give"]["currency"],
             spec["give"]["method"], spec["note"], via, str(actor_id)))
        ex_id = cur.lastrowid
        _insert_legs(conn, ex_id, spec["legs"])
        fee_id = None
        if existing_fee_id is not None:
            conn.execute("UPDATE transactions SET exchange_id=? WHERE id=? AND exchange_id IS NULL",
                         (ex_id, existing_fee_id))
            fee_id = existing_fee_id
        elif spec["fee"] is not None:
            fee_id = _insert_fee(conn, ex_id, idem_key, spec["fee"], spec["date"], created_at, via, actor_id, fx_rates)
        estimated = any(leg["estimated"] for leg in spec["legs"])
        _audit(conn, created_at, actor_id, "create", ex_id, via,
               dict({"legs": len(spec["legs"]), "fee": fee_id is not None, "estimated": estimated,
                     "backdated": spec["date"] != today}, **({"linked_fee": True} if existing_fee_id else {})))
        if on_created is not None:
            on_created(conn, ex_id)
        conn.execute("COMMIT")
        return {"ok": True, "id": ex_id, "duplicate_request": False, "estimated": estimated, "fee_id": fee_id}
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _load(conn, ex_id) -> sqlite3.Row:
    if not isinstance(ex_id, int) or isinstance(ex_id, bool) or ex_id <= 0:
        raise RecordError(404, "not_found")
    row = conn.execute("SELECT * FROM exchanges WHERE id=?", (ex_id,)).fetchone()
    if row is None:
        raise RecordError(404, "not_found")
    if row["occurred_on"][:7] in db.closed_months(conn):
        raise RecordError(422, "month_closed")
    return row


def _set_status(conn, actor_id, ex_id, new_status: str, via: str, now: datetime | None) -> dict:
    """Мягкое удаление и возврат вместе с комиссией. Повтор того же действия ничего не меняет и не пишет в audit."""
    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _load(conn, ex_id)
        changed = row["status"] != new_status
        if changed:
            cancelled_at = ts if new_status == "cancelled" else None
            conn.execute("UPDATE exchanges SET status=?, cancelled_at=? WHERE id=?", (new_status, cancelled_at, ex_id))
            conn.execute("UPDATE transactions SET status=?, cancelled_at=? WHERE exchange_id=? "
                         "AND exchange_removed IS NULL", (new_status, cancelled_at, ex_id))   # убранные комиссии не трогаем
            _audit(conn, ts, actor_id, "cancel" if new_status == "cancelled" else "restore", ex_id, via,
                   {"from": row["status"], "to": new_status})
        conn.execute("COMMIT")
        return {"ok": True, "id": ex_id, "status": new_status, "changed": changed}
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def cancel_exchange(conn, actor_id, ex_id, via: str = "webapp", now: datetime | None = None) -> dict:
    return _set_status(conn, actor_id, ex_id, "cancelled", via, now)


def restore_exchange(conn, actor_id, ex_id, via: str = "webapp", now: datetime | None = None) -> dict:
    return _set_status(conn, actor_id, ex_id, "active", via, now)


def _stored_spec(conn, row) -> dict:
    """Текущее состояние обмена в виде spec (то, что вводил владелец: доли указаны только у не оценочных ног)."""
    multi = conn.execute("SELECT COUNT(*) FROM exchange_legs WHERE exchange_id=?", (row["id"],)).fetchone()[0] > 1
    legs = [{"cents": r["get_cents"], "currency": r["get_currency"], "method": r["get_method"],
             "user_part": r["give_part_cents"] if multi and not r["part_estimated"] else None}
            for r in conn.execute("SELECT * FROM exchange_legs WHERE exchange_id=? ORDER BY id", (row["id"],))]
    fee = conn.execute("SELECT id, amount_cents, currency, method FROM transactions WHERE exchange_id=? "
                       "AND exchange_removed IS NULL ORDER BY id LIMIT 1", (row["id"],)).fetchone()
    return {"date": row["occurred_on"], "note": row["note"],
            "give": {"cents": row["give_cents"], "currency": row["give_currency"], "method": row["give_method"]},
            "legs": legs, "fee": None if fee is None else {"cents": fee["amount_cents"], "currency": fee["currency"],
                                                           "method": fee["method"], "id": fee["id"]}}


def update_exchange(conn: sqlite3.Connection, actor_id, ex_id, payload, via: str = "webapp",
                    now: datetime | None = None, fx_rates: dict | None = None) -> dict:
    """Правка обмена: дата, отдано, получено (ноги заменяются целиком), комиссия (пусто - убрать), заметка. Передаются
    только изменяемые ключи; значение, равное текущему, ничего не меняет. Доли неуказанных ног пересчитываются по
    курсам кэша. Комиссия правится вместе с обменом (сумма, валюта, способ, дата), убранная комиссия отменяется и
    помечается убранной (exchange_removed): с обменом не двигается, через /api/records остаётся заблокированной. В audit только список изменённых полей."""
    if not isinstance(payload, dict) or not payload:
        raise RecordError(422, "bad_payload")
    if not set(payload) <= EDITABLE_KEYS:
        raise RecordError(422, "field_not_editable")
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = db.today_local(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _load(conn, ex_id)
        if row["status"] != "active":
            raise RecordError(409, "exchange_cancelled")
        old = _stored_spec(conn, row)
        new = {k: (list(v) if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v)) for k, v in old.items()}
        if "date" in payload:
            new["date"] = _parse_date(payload["date"], today)
            if new["date"][:7] in db.closed_months(conn):
                raise RecordError(422, "month_closed")
        if "note" in payload:
            new["note"] = _clean_text(payload["note"], "note", 200, False)
        if "give" in payload:
            new["give"] = _parse_give(conn, payload["give"])
        if "get" in payload:
            new["legs"] = _parse_legs(conn, payload["get"])
        _check_currencies(new["give"], new["legs"])
        if "fee" in payload:
            new["fee"] = _parse_fee(conn, payload["fee"], new["give"])
        allocate(new["give"]["cents"], new["legs"], fx_rates)
        changed = []
        if new["date"] != old["date"]:
            changed.append("date")
        if new["note"] != old["note"]:
            changed.append("note")
        if new["give"] != old["give"]:
            changed.append("give")
        key = lambda legs: [(x["cents"], x["currency"], x["method"], x["user_part"]) for x in legs]   # noqa: E731
        if key(new["legs"]) != key(old["legs"]):
            changed.append("get")
        fee_key = lambda f: None if f is None else (f["cents"], f["currency"], f["method"])           # noqa: E731
        if fee_key(new["fee"]) != fee_key(old["fee"]) or (new["fee"] and new["date"] != old["date"]):
            changed.append("fee")
        if not changed:
            conn.execute("COMMIT")
            return {"ok": True, "id": ex_id, "changed": []}
        conn.execute("UPDATE exchanges SET occurred_on=?, give_cents=?, give_currency=?, give_method=?, note=? "
                     "WHERE id=?", (new["date"], new["give"]["cents"], new["give"]["currency"],
                                    new["give"]["method"], new["note"], ex_id))
        conn.execute("DELETE FROM exchange_legs WHERE exchange_id=?", (ex_id,))
        _insert_legs(conn, ex_id, new["legs"])
        if new["fee"] is None and old["fee"] is not None:
            conn.execute("UPDATE transactions SET status='cancelled', cancelled_at=?, exchange_removed=1 WHERE id=?",
                         (ts, old["fee"]["id"]))             # связь остаётся: через /api/records не вернуть и не править
        elif new["fee"] is not None and old["fee"] is not None:
            f = new["fee"]
            conn.execute("UPDATE transactions SET amount_cents=?, currency=?, usd_cents=?, method=?, occurred_on=?, "
                         "amount_orig=NULL WHERE id=?",
                         (f["cents"], f["currency"], fx.convert(f["currency"], f["cents"], fx_rates), f["method"],
                          new["date"], old["fee"]["id"]))
        elif new["fee"] is not None:
            _insert_fee(conn, ex_id, "%s:%s" % (row["idem_key"], uuid.uuid4().hex[:8]), new["fee"], new["date"], ts,
                        via, actor_id, fx_rates)
        _audit(conn, ts, actor_id, "update", ex_id, via, {"changed": changed})
        conn.execute("COMMIT")
        return {"ok": True, "id": ex_id, "changed": changed}
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# --- перенос уже записанного обмена одной командой ---------------------------------------------------------------------
def main(argv=None) -> int:
    """python -m kassa_api.exchanges link-existing-fee --db <база> --fee-id <id записи> --payload <файл.json>
    --idem-key <ключ> [--actor splinter] [--apply]. Создаёт обмен и в той же транзакции ставит exchange_id на уже
    записанную комиссию (новая не создаётся). Без --apply - сухой прогон на копии базы в памяти (боевая не меняется).
    Снимок базы перед --apply делает оператор (backup_db.py). Курсы берутся из кэша рядом с базой. В вывод идут только
    исходы и номера, без сумм."""
    import argparse
    import os
    from pathlib import Path
    ap = argparse.ArgumentParser(prog="python -m kassa_api.exchanges", description=(main.__doc__ or "").strip().split(chr(10))[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    lf = sub.add_parser("link-existing-fee")
    lf.add_argument("--db", default=os.environ.get("KASSA_DB_PATH", "/data/kassa-test.db"))
    lf.add_argument("--fee-id", type=int, required=True)
    lf.add_argument("--payload", required=True, help="JSON: date, give, get, fee (сумма и валюта как у записи), note")
    lf.add_argument("--idem-key", required=True)
    lf.add_argument("--actor", default="splinter")
    lf.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    db.check_path_allowed(args.db)
    with open(args.payload, encoding="utf-8") as f:
        payload = json.load(f)
    src = db.connect(args.db)
    try:
        if src.execute("PRAGMA user_version").fetchone()[0] != db.SCHEMA_VERSION:
            print("база не схемы %d: сначала запустите kassa-api с этой версией" % db.SCHEMA_VERSION)
            return 2
        if args.apply:
            conn = src
        else:
            conn = sqlite3.connect(":memory:", isolation_level=None)
            src.backup(conn)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
        rates_path = Path(args.db).with_name(fx.CACHE_NAME)
        try:
            res = create_exchange(conn, args.actor, payload, via="splinter", fx_rates=fx.RateStore(rates_path).usable(),
                                  idem_key=args.idem_key, existing_fee_id=args.fee_id)
        except RecordError as exc:
            print("отказ: %s" % exc.code)
            return 1
        print("%s: обмен №%d, комиссия №%d привязана%s" % (
            "записано" if args.apply else "сухой прогон ок (база не менялась)", res["id"], res["fee_id"],
            ", повтор: обмен уже был" if res["duplicate_request"] else ""))
        return 0
    finally:
        src.close()


if __name__ == "__main__":
    import sys
    sys.exit(main())
