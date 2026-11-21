"""Черновики-карточки для голоса и денежных фраз из чата (процесс бота bot.py).

Бот - только транспорт, поэтому его токен (KASSA_INTERNAL_TOKEN) умеет
ровно три вещи: создать ЧЕРНОВИК из текста или расшифровки (с лимитом и сроком жизни), отменить черновик
(«Отмена», «Это не деньги»; ответ без текста карточки) и забрать очередь исходящих. Записать деньги, отменить или
вернуть запись, прочитать карточку токеном нельзя.

Запись создаётся только в Mini App: «Верно» / «Записать» / «Исправить» в карточке - инлайн-кнопки web_app, они
открывают форму на экране этого черновика (сумма, валюта, «на что», способ предзаполнены), а «Записать» уходит с
подписанным initData (Ed25519, допуск по user id) на POST /api/records с draft_id. Ключ идемпотентности записи по
черновику - 'card:<id>': форма и карточка не могут записать одно и то же дважды. Проверка черновика, учёт
предупреждения о сумме, запись, закрытие черновика и правка сообщения в чате - одна транзакция.

Обмен валют (exchanges.py): фраза «поменял X на Y» (exchangetext.py) даёт черновик с draft.kind='exchange' и такую же
карточку; запись обмена - только POST /api/exchanges с подписанным initData и draft_id (confirm_exchange_draft), ключ
идемпотентности 'card:<id>'. Токен бота обмен не записывает: ему доступны те же «Отмена» и «Это не деньги».

Кнопки описываются словарями {"text", "cb"} (callback_data) или {"text", "web_app"} (адрес формы); бот собирает
из них инлайн-клавиатуру. callback_data: kassa:<действие>:<id карточки> (лимит Telegram - 64 байта); живых действий
два: no и notmoney. Третье - отметка дня «Только карта» / «Не тратил» под вечерним напоминанием: callback_data
kassa:day:<cardonly|nospend>:<YYYY-MM-DD> (день виден в самой кнопке), обрабатывает day_mark.
"""
import json
import sqlite3
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import Decimal

from . import categorize, db, exchanges, exchangetext, money, moneytext, records, rates as fx

ARM_MINUTES = 10
DRAFT_TTL_HOURS = 24            # неподтверждённый черновик живёт сутки, потом закрывается сам
MAX_PENDING = 20                # открытых черновиков одновременно
MAX_CREATED_PER_HOUR = 30       # новых черновиков в час (потолок для того, кто владеет токеном)
MIN_CONFIRM_SECONDS = 2         # предупреждение о сумме надо успеть прочитать: «ещё раз» раньше этого срока не считается
CB_PREFIX = "kassa"
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]
DIR_WORD = {"out": "Расход", "in": "Поступление"}
DIARY_HINT = "Если это запись в дневник, начните сообщение со слова «Дневник:» и отправьте ещё раз."
NOT_MONEY_TEXT = "Это не деньги, ничего не записано. " + DIARY_HINT
CANCELLED_TEXT = "✖ Отменено, ничего не записано."
EXPIRED_TEXT = "⌛ Черновик устарел, ничего не записано."
REJECT_TEXT = {
    "no_amount": "Не вижу суммы. Ничего не записано. Откройте Кассу и введите цифрами. " + DIARY_HINT,
    "unsure_amount": "Не уверен в сумме, ничего не записано. Откройте Кассу и введите цифрами. " + DIARY_HINT,
    "too_many": "Слишком много неподтверждённых записей. Откройте Кассу, вкладка «Входящие»: разберите их. "
                "Ничего не записано.",
    "rate_limit": "Слишком часто. Подождите немного или откройте Кассу. Ничего не записано.",
    "several": "В сообщении несколько сумм, ничего не записано. Отправьте каждую трату отдельным сообщением или "
               "откройте Кассу. " + DIARY_HINT,
    "unsure_exchange": "Похоже на обмен валюты, но я не уверен в суммах или валютах, ничего не записано. Откройте "
                       "Кассу, режим «Обмен», и введите цифрами. " + DIARY_HINT,
    "msg_conflict": "Это сообщение не принято: его номер уже занят другим текстом. Ничего не записано. "
                    "Отправьте ещё раз.",
}
ERROR_TEXT = {
    "month_closed": "Этот месяц закрыт: записи за него не вносятся.",
    "method_required": "Сначала выберите способ.",
    "unknown_method": "Такого способа нет. Выберите из списка.",
    "bad_amount": "Сумма не подошла. Откройте Кассу и введите цифрами.",
    "amount_too_large": "Сумма слишком большая. Откройте Кассу и проверьте нули.",
    "date_in_future": "Дата в будущем. Откройте Кассу.",
}
WARN_TEXT = {
    "outlier": "Сумма больше чем в 10 раз выше обычного расхода в этой валюте. Проверьте нули.",
    "possible_duplicate": "Такая же запись уже была за последние 10 минут.",
}
CALLBACK_ACTIONS = ("no", "notmoney")      # живые callback-действия карточки; всё прочее - «кнопка устарела»
STALE_BUTTON = "Эта кнопка устарела. Откройте карточку заново или Кассу."


class CardError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_amount(amount: str) -> str:
    """'1500' -> '1 500', '8.5' -> '8,50'. Только показ; в базу идёт исходная строка."""
    d = Decimal(amount)
    whole, _, frac = format(d.quantize(Decimal("0.01")), "f").partition(".")
    whole = "{:,}".format(int(whole)).replace(",", " ")
    return whole if frac == "00" else whole + "," + frac


def fmt_date(iso: str, today: str) -> str:
    d, t = date_cls.fromisoformat(iso), date_cls.fromisoformat(today)
    delta = (t - d).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "вчера"
    if delta == 2:
        return "позавчера"
    return "%d %s" % (d.day, MONTHS_GEN[d.month - 1])


# --- кнопка «Надиктовать» ------------------------------------------------------------------------------------------
def arm_voice(conn: sqlite3.Connection, actor_id, direction: str, now: datetime | None = None) -> str:
    if direction not in ("in", "out"):
        raise CardError(422, "bad_direction")
    now = now or datetime.now(timezone.utc)
    until = _iso(now + timedelta(minutes=ARM_MINUTES))
    conn.execute("INSERT INTO awaiting_voice(actor_id, direction, armed_until) VALUES (?,?,?) "
                 "ON CONFLICT(actor_id) DO UPDATE SET direction=excluded.direction, armed_until=excluded.armed_until",
                 (str(actor_id), direction, until))
    return until


def armed(conn: sqlite3.Connection, actor_id, now: datetime | None = None) -> str | None:
    """Направление, под которое взведён приём голоса, или None (не взведён / срок вышел)."""
    now = now or datetime.now(timezone.utc)
    row = conn.execute("SELECT direction, armed_until FROM awaiting_voice WHERE actor_id=?", (str(actor_id),)).fetchone()
    if row is None:
        return None
    if row["armed_until"] < _iso(now):
        conn.execute("DELETE FROM awaiting_voice WHERE actor_id=?", (str(actor_id),))
        return None
    return row["direction"]


def disarm(conn: sqlite3.Connection, actor_id) -> None:
    conn.execute("DELETE FROM awaiting_voice WHERE actor_id=?", (str(actor_id),))


# --- очередь исходящих ----------------------------------------------------------------------------------------------
def outbox_add(conn: sqlite3.Connection, kind: str, chat_id, payload: dict, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    cur = conn.execute("INSERT INTO outbox(created_at, kind, chat_id, payload) VALUES (?,?,?,?)",
                       (_iso(now), kind, str(chat_id), json.dumps(payload, ensure_ascii=False)))
    return cur.lastrowid


def outbox_claim(conn: sqlite3.Connection, now: datetime | None = None, limit: int = 10,
                 lease_seconds: int = 60, max_attempts: int = 5) -> list[dict]:
    """Берёт неотправленные сообщения и закрепляет их за ботом на lease_seconds (если не подтвердит - вернутся).
    Сообщение, которое уже выдавали max_attempts раз и ни разу не подтвердили, больше не выдаётся (failed)."""
    now = now or datetime.now(timezone.utc)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE outbox SET status='failed', leased_until=NULL WHERE status='pending' AND attempts >= ? "
                     "AND (leased_until IS NULL OR leased_until < ?)", (max_attempts, _iso(now)))
        rows = conn.execute("SELECT id, kind, chat_id, payload, attempts FROM outbox WHERE status='pending' "
                            "AND (leased_until IS NULL OR leased_until < ?) ORDER BY id LIMIT ?",
                            (_iso(now), limit)).fetchall()
        lease = _iso(now + timedelta(seconds=lease_seconds))
        out = []
        for r in rows:
            conn.execute("UPDATE outbox SET leased_until=?, attempts=attempts+1 WHERE id=?", (lease, r["id"]))
            out.append({"id": r["id"], "kind": r["kind"], "chat_id": r["chat_id"],
                        "payload": json.loads(r["payload"]), "attempts": r["attempts"] + 1})
        conn.execute("COMMIT")
        return out
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def outbox_ack(conn: sqlite3.Connection, item_id: int, ok: bool, now: datetime | None = None,
               max_attempts: int = 5, message_id=None) -> str:
    now = now or datetime.now(timezone.utc)
    row = conn.execute("SELECT attempts, status, leased_until FROM outbox WHERE id=?", (item_id,)).fetchone()
    if row is None:
        raise CardError(404, "not_found")
    if row["status"] != "pending":
        return row["status"]
    # подтвердить можно только то, что выдано и аренда ещё идёт: чужие id и «подтверждение вслепую» не принимаются
    if row["leased_until"] is None or row["leased_until"] < _iso(now):
        raise CardError(409, "lease_expired")
    if ok:
        conn.execute("UPDATE outbox SET status='sent', sent_at=?, leased_until=NULL, message_id=COALESCE(?, message_id) "
                     "WHERE id=?", (_iso(now), str(message_id) if message_id is not None else None, item_id))
        return "sent"
    if row["attempts"] >= max_attempts:
        conn.execute("UPDATE outbox SET status='failed', leased_until=NULL WHERE id=?", (item_id,))
        return "failed"
    conn.execute("UPDATE outbox SET leased_until=? WHERE id=?", (_iso(now + timedelta(seconds=30)), item_id))
    return "pending"


def alarm_once(conn: sqlite3.Connection, chat_id, kind: str, text: str, now: datetime | None = None) -> bool:
    """Тревога владельцу в чат не чаще раза в сутки по ключу kind (daily_marks). True, если поставлена."""
    now = now or datetime.now(timezone.utc)
    day = db.today_local(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("INSERT OR IGNORE INTO daily_marks(day, kind, created_at) VALUES (?,?,?)",
                           (day, "alarm:" + kind[:60], _iso(now)))
        if cur.rowcount:
            outbox_add(conn, "send", chat_id, {"text": text, "buttons": []}, now)
        conn.execute("COMMIT")
        return bool(cur.rowcount)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# --- карточки ------------------------------------------------------------------------------------------------------
def _load(conn, card_id) -> sqlite3.Row:
    if not isinstance(card_id, int) or isinstance(card_id, bool) or card_id <= 0:
        raise CardError(404, "not_found")
    row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if row is None:
        raise CardError(404, "not_found")
    return row


def _draft(row) -> dict:
    return json.loads(row["draft"])


def _method_title(conn, code):
    if not code:
        return None
    r = conn.execute("SELECT title FROM methods WHERE code=?", (code,)).fetchone()
    return r["title"] if r else code


def render(conn: sqlite3.Connection, row, webapp_url: str, test: bool, now: datetime | None = None,
           fx_rates: dict | None = None) -> dict:
    """Текст и кнопки карточки по её состоянию. -> {"text", "buttons"}.
    «Верно», «Записать», «Исправить», «Это поступление» - кнопки web_app (открывают форму на экране этого черновика);
    callback остался только у «Отмена» и «Это не деньги»."""
    now = now or datetime.now(timezone.utc)
    today = db.today_local(now)
    d = _draft(row)
    cid = row["id"]
    status = row["status"]
    if d.get("kind") == "exchange":
        return _render_exchange(conn, row, d, webapp_url, test, today, fx_rates)
    method_title = _method_title(conn, d.get("method"))
    summary = "%s: %s %s · %s · %s" % (DIR_WORD[d["direction"]], fmt_amount(d["amount"]), d["currency"],
                                       d["category"], fmt_date(d["date"], today))
    lines = []
    if row["source"] == "voice":
        lines.append("🎙 Услышал: «%s»" % (row["heard"] or ""))
    else:
        word = "трату" if d["direction"] == "out" else "поступление"
        lines.append("Похоже на %s: «%s»" % (word, row["heard"] or ""))
    lines.append(summary)
    if d.get("currency_guess"):
        lines.append("Валюта не названа, взята по последней записи: %s." % d["currency"])
    if status == "pending":
        lines.append("Способ: %s" % (method_title or "не выбран, выберите в Кассе"))
        for w in json.loads(row["warned"] or "[]"):
            lines.append("⚠ " + WARN_TEXT.get(w, w))
        lines.append("Проверьте и нажмите «Записать» в Кассе: без этого ничего не запишется.")
    elif status == "confirmed":
        lines.append("Способ: %s" % (method_title or "—"))
    if test:
        lines.append("🧪 ТЕСТ: запись пойдёт в тестовую базу")
    edit_url = "%s?draft=%d" % (webapp_url, cid)
    if status == "pending":
        if row["source"] == "voice":
            buttons = [[{"text": "✅ Верно", "web_app": edit_url + "&ok=1"},
                        {"text": "✏️ Исправить", "web_app": edit_url}],
                       [{"text": "✖ Отмена", "cb": "%s:no:%d" % (CB_PREFIX, cid)}]]
        else:
            other = "поступление" if d["direction"] == "out" else "расход"
            buttons = [[{"text": "✅ Записать", "web_app": edit_url + "&ok=1"},
                        {"text": "✏️ Исправить", "web_app": edit_url}],
                       [{"text": "🔁 Это " + other, "web_app": edit_url + "&flip=1"},
                        {"text": "✖ Это не деньги", "cb": "%s:notmoney:%d" % (CB_PREFIX, cid)}]]
        return {"text": "\n".join(lines), "buttons": buttons}
    if status == "confirmed":
        cancelled = conn.execute("SELECT status FROM transactions WHERE id=?", (row["tx_id"],)).fetchone()
        head = "↩ Запись №%d отменена" % row["tx_id"] if (cancelled is not None and cancelled["status"] == "cancelled") \
            else "✅ Записано №%d" % row["tx_id"]
        return {"text": head + "\n" + "\n".join(lines[1:]),
                "buttons": [[{"text": "Открыть кассу", "web_app": webapp_url + "?tab=recent"}]]}
    if status == "cancelled":
        return {"text": EXPIRED_TEXT if d.get("expired") else CANCELLED_TEXT, "buttons": []}
    return {"text": "✏️ Исправлено в форме, эта карточка закрыта.", "buttons": []}


def _exchange_texts(conn, give: dict, legs: list, fee: dict | None, give_method, today: str, date: str) -> list:
    """Строки карточки обмена. give: {cents, currency}; legs: [{cents, currency, part, estimated, method}]."""
    gm = _method_title(conn, give_method)
    got = ", ".join(exchanges.money_text(leg["cents"], leg["currency"]) +
                    (" (%s)" % _method_title(conn, leg.get("method")) if leg.get("method") else "") for leg in legs)
    lines = ["Обмен: отдал %s%s → получил %s · %s" % (exchanges.money_text(give["cents"], give["currency"]),
                                                    " (%s)" % gm if gm else "", got, fmt_date(date, today))]
    lines.append("Курс: " + "; ".join(exchanges.rate_line(give["currency"], leg) for leg in legs))
    if any(leg.get("estimated") for leg in legs):
        lines.append("≈ доли не указаны: курс оценочный, по рыночной стоимости полученного.")
    if fee is not None:
        lines.append("Комиссия: " + exchanges.money_text(fee["cents"], fee["currency"]))
    return lines


def _exchange_pending_view(conn, row, d: dict, webapp_url: str, test: bool, today: str, fx_rates) -> dict:
    give = {"cents": money.parse_amount_to_cents(d["give"]["amount"]), "currency": d["give"]["currency"]}
    parts = exchanges.preview(d, fx_rates) or [None] * len(d["get"])
    legs = [{"cents": money.parse_amount_to_cents(g["amount"]), "currency": g["currency"], "method": g.get("method"),
             "part": p["part"] if p else None, "estimated": p["estimated"] if p else 1}
            for g, p in zip(d["get"], parts)]
    fee = d.get("fee") and {"cents": money.parse_amount_to_cents(d["fee"]["amount"]), "currency": d["fee"]["currency"]}
    lines = [("🎙 Услышал: «%s»" if row["source"] == "voice" else "Похоже на обмен: «%s»") % (row["heard"] or "")]
    lines += _exchange_texts(conn, give, legs, fee, d["give"].get("method"), today, d["date"])
    if fee is not None and not d["give"].get("method"):
        lines.append("Способ отдачи не выбран: для комиссии он нужен, выберите в Кассе.")
    for w in json.loads(row["warned"] or "[]"):
        lines.append("⚠ " + WARN_TEXT.get(w, w))
    lines.append("Проверьте и нажмите «Записать» в Кассе: без этого ничего не запишется.")
    if test:
        lines.append("🧪 ТЕСТ: запись пойдёт в тестовую базу")
    edit_url = "%s?draft=%d" % (webapp_url, row["id"])
    if row["source"] == "voice":
        buttons = [[{"text": "✅ Верно", "web_app": edit_url + "&ok=1"}, {"text": "✏️ Исправить", "web_app": edit_url}],
                   [{"text": "✖ Отмена", "cb": "%s:no:%d" % (CB_PREFIX, row["id"])}]]
    else:
        buttons = [[{"text": "✅ Записать", "web_app": edit_url + "&ok=1"},
                    {"text": "✏️ Исправить", "web_app": edit_url}],
                   [{"text": "✖ Это не деньги", "cb": "%s:notmoney:%d" % (CB_PREFIX, row["id"])}]]
    return {"text": "\n".join(lines), "buttons": buttons}


def _render_exchange(conn, row, d: dict, webapp_url: str, test: bool, today: str, fx_rates) -> dict:
    """Карточка обмена: «Записать» и «Исправить» открывают форму Mini App на экране этого черновика (запись только с
    подписью), «Отмена» и «Это не деньги» - callback. Записанный обмен показывается из базы, а не из черновика."""
    status = row["status"]
    if status == "pending":
        return _exchange_pending_view(conn, row, d, webapp_url, test, today, fx_rates)
    if status == "confirmed":
        item = exchanges.get_item(conn, row["tx_id"])
        give = {"cents": item["give"]["cents"], "currency": item["give"]["currency"]}
        legs = [{"cents": x["get_cents"], "currency": x["get_currency"], "part": x["give_part_cents"],
                 "estimated": x["estimated"], "method": x["get_method"]} for x in item["legs"]]
        fee = item["fee"] and {"cents": item["fee"]["cents"], "currency": item["fee"]["currency"]}
        head = "↩ Обмен №%d отменён" % item["id"] if item["status"] == "cancelled" \
            else "✅ Записан обмен №%d" % item["id"]
        lines = _exchange_texts(conn, give, legs, fee, item["give"]["method"], today, item["occurred_on"])
        return {"text": head + "\n" + "\n".join(lines),
                "buttons": [[{"text": "Открыть кассу", "web_app": webapp_url + "?tab=exchanges"}]]}
    if status == "cancelled":
        return {"text": EXPIRED_TEXT if d.get("expired") else CANCELLED_TEXT, "buttons": []}
    return {"text": "✏️ Исправлено в форме, эта карточка закрыта.", "buttons": []}


def _exchange_draft(conn, ex: dict) -> dict:
    """Черновик обмена из разбора; способ, не подходящий направлению (отдаём - расход, получаем - поступление), снимается."""
    def ok(code, direction):
        if code is None:
            return None
        r = conn.execute("SELECT scope FROM methods WHERE code=? AND active=1", (code,)).fetchone()
        return code if r is not None and r["scope"] in (direction, "both") else None
    draft = dict(ex)
    draft["give"] = dict(ex["give"], method=ok(ex["give"]["method"], "out"))
    draft["get"] = [dict(g, method=ok(g["method"], "in")) for g in ex["get"]]
    return draft


def _new_draft(parsed: dict, direction: str) -> dict:
    return {"direction": parsed["direction"], "amount": parsed["amount"], "currency": parsed["currency"],
            "currency_guess": bool(parsed["currency_guess"]), "note": parsed["note"],
            "category": categorize.categorize(parsed["note"], parsed["direction"]), "date": parsed["date"],
            "method": parsed["method"]}


def _normalize_heard(text) -> str:
    text = " ".join(str(text or "").split())
    return text[:600]


def create_from_text(conn: sqlite3.Connection, actor_id, chat_id, source: str, text, src_message_id,
                     default_direction: str | None = None, now: datetime | None = None) -> dict:
    """Фраза или расшифровка -> черновик. Возвращает {"ok", "card_id", "duplicate", "resend"?, "reply"?, "reject"?}.
    Не уверен в сумме / суммы нет / слишком много открытых черновиков -> {"ok": False, "reject": код} без карточки.
    Проверка повтора, лимитов и вставка - одна транзакция (повторная доставка того же сообщения даёт ту же карточку)."""
    if source not in ("voice", "phrase"):
        raise CardError(422, "bad_source")
    now = now or datetime.now(timezone.utc)
    today = db.today_local(now)
    text = _normalize_heard(text)
    src = str(src_message_id) if src_message_id is not None else None
    expire_old(conn, now)
    last = {"out": _last_currency(conn, "out"), "in": _last_currency(conn, "in")}
    direction_default = default_direction if default_direction in ("in", "out") else "out"
    try:
        exchange = exchangetext.parse(text, today)      # сначала обмен; не обмен - обычный разбор денег без изменений
    except exchangetext.ExchangeReject as exc:
        return {"ok": False, "reject": exc.code, "reply": REJECT_TEXT[exc.code]}
    if exchange is not None:
        draft = _exchange_draft(conn, exchange)
    else:
        try:
            parsed = moneytext.parse(text, today, direction_default, last[direction_default])
        except moneytext.Reject as exc:
            return {"ok": False, "reject": exc.code, "reply": REJECT_TEXT["several" if exc.several else exc.code]}
        if parsed["currency"] is None or parsed["currency_guess"]:
            parsed["currency"] = last[parsed["direction"]]
            parsed["currency_guess"] = True
        if parsed["method"] is not None:
            scope = conn.execute("SELECT scope FROM methods WHERE code=? AND active=1", (parsed["method"],)).fetchone()
            if scope is None or scope["scope"] not in (parsed["direction"], "both"):
                parsed["method"] = None
        draft = _new_draft(parsed, direction_default)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if src is not None:
            row = conn.execute("SELECT * FROM cards WHERE chat_id=? AND src_message_id=?",
                               (str(chat_id), src)).fetchone()
            if row is not None:                   # то же сообщение доставлено повторно
                conn.execute("COMMIT")
                if (row["heard"] or "") != text:  # тот же номер, другой текст: подмена или коллизия, молча не глотаем
                    return {"ok": False, "reject": "msg_conflict", "reply": REJECT_TEXT["msg_conflict"]}
                # карточка создана, но сообщение в чате не дошло: отправляем заново (только если текст тот же,
                # чтобы повтором с чужим id нельзя было прочитать чужую карточку)
                resend = row["status"] == "pending" and not row["message_id"] and (row["heard"] or "") == text
                return {"ok": True, "card_id": row["id"], "duplicate": True, "resend": bool(resend)}
        pending = conn.execute("SELECT COUNT(*) FROM cards WHERE actor_id=? AND status='pending'",
                               (str(actor_id),)).fetchone()[0]
        recent = conn.execute("SELECT COUNT(*) FROM cards WHERE actor_id=? AND created_at>=?",
                              (str(actor_id), _iso(now - timedelta(hours=1)))).fetchone()[0]
        if pending >= MAX_PENDING or recent >= MAX_CREATED_PER_HOUR:
            code = "too_many" if pending >= MAX_PENDING else "rate_limit"
            conn.execute("INSERT INTO audit(ts, actor_id, action, entity, entity_id, via, detail) "
                         "VALUES (?,?,?,?,?,?,?)", (_iso(now), str(actor_id), "card_refused", "card", None, source,
                                                    json.dumps({"reason": code})))
            conn.execute("COMMIT")
            return {"ok": False, "reject": code, "reply": REJECT_TEXT[code]}
        cur = conn.execute("INSERT INTO cards(actor_id, chat_id, source, heard, draft, src_message_id, created_at) "
                           "VALUES (?,?,?,?,?,?,?)",
                           (str(actor_id), str(chat_id), source, text, json.dumps(draft, ensure_ascii=False), src,
                            _iso(now)))
        card_id = cur.lastrowid
        conn.execute("INSERT INTO audit(ts, actor_id, action, entity, entity_id, via, detail) VALUES (?,?,?,?,?,?,?)",
                     (_iso(now), str(actor_id), "card_create", "card", str(card_id), source, json.dumps({})))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"ok": True, "card_id": card_id, "duplicate": False}


def _last_currency(conn, direction: str) -> str:
    r = conn.execute("SELECT currency FROM transactions WHERE direction=? AND status='active' "
                     "AND source <> 'month-close' AND closed_by IS NULL "
                     "ORDER BY created_at DESC, id DESC LIMIT 1", (direction,)).fetchone()
    return r["currency"] if r else "TRY"


def _close(conn, card_id, status, now, tx_id=None):
    conn.execute("UPDATE cards SET status=?, closed_at=?, tx_id=COALESCE(?, tx_id) WHERE id=?",
                 (status, _iso(now), tx_id, card_id))


def _edit_in_chat(conn, row, text: str, buttons: list, now) -> None:
    if row["message_id"]:
        outbox_add(conn, "edit", row["chat_id"], {"message_id": row["message_id"], "text": text, "buttons": buttons}, now)


def expire_old(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Закрывает неподтверждённые черновики старше DRAFT_TTL_HOURS; сообщение в чате правится на «устарел»."""
    now = now or datetime.now(timezone.utc)
    limit = _iso(now - timedelta(hours=DRAFT_TTL_HOURS))
    if conn.execute("SELECT 1 FROM cards WHERE status='pending' AND created_at < ? LIMIT 1", (limit,)).fetchone() is None:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute("SELECT * FROM cards WHERE status='pending' AND created_at < ?", (limit,)).fetchall()
        for row in rows:
            d = _draft(row)
            d["expired"] = True
            conn.execute("UPDATE cards SET status='cancelled', closed_at=?, draft=? WHERE id=?",
                         (_iso(now), json.dumps(d, ensure_ascii=False), row["id"]))
            conn.execute("INSERT INTO audit(ts, actor_id, action, entity, entity_id, via, detail) "
                         "VALUES (?,?,?,?,?,?,?)", (_iso(now), row["actor_id"], "card_expire", "card", str(row["id"]),
                                                    "system", json.dumps({})))
            _edit_in_chat(conn, row, EXPIRED_TEXT, [], now)
        conn.execute("COMMIT")
        return len(rows)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def act(conn: sqlite3.Connection, actor_id, card_id, action: str, value: str | None = None, webapp_url: str = "",
        test: bool = False, fx_rates: dict | None = None, now: datetime | None = None) -> dict:
    """Нажатие callback-кнопки карточки. Остались только «Отмена» (no) и «Это не деньги» (notmoney): отмена
    черновика безвредна. Токен бота ограничен, поэтому ответ НЕ содержит текста карточки (только
    общий текст), а записать, отменить запись или прочитать карточку этим путём нельзя.
    -> {"answer": всплывающий текст, "edit": {"text", "buttons"} | None}."""
    now = now or datetime.now(timezone.utc)
    row = _load(conn, card_id)
    if row["actor_id"] != str(actor_id):
        raise CardError(403, "not_yours")
    if action not in ("no", "notmoney"):
        return {"answer": STALE_BUTTON, "edit": None}
    expire_old(conn, now)
    row = _load(conn, card_id)
    if row["status"] != "pending":
        return {"answer": "Карточка уже закрыта", "edit": None}
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("UPDATE cards SET status='cancelled', closed_at=? WHERE id=? AND status='pending'",
                           (_iso(now), card_id))
        if cur.rowcount:
            conn.execute("INSERT INTO audit(ts, actor_id, action, entity, entity_id, via, detail) "
                         "VALUES (?,?,?,?,?,?,?)", (_iso(now), str(actor_id), "card_cancel", "card", str(card_id),
                                                    "card", json.dumps({"action": action})))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    text = NOT_MONEY_TEXT if action == "notmoney" else CANCELLED_TEXT
    return {"answer": "Отменено", "edit": {"text": text, "buttons": []}}


# --- запись из формы по черновику --------------------------------------------------------------------------------------
def draft_for_form(conn: sqlite3.Connection, actor_id, card_id, now: datetime | None = None) -> dict:
    """Значения черновика для формы (подписанный запрос владельца). Закрытый или устаревший - 409."""
    now = now or datetime.now(timezone.utc)
    expire_old(conn, now)
    row = _load(conn, card_id)
    if row["actor_id"] != str(actor_id):
        raise CardError(403, "not_yours")
    if row["status"] != "pending":
        raise CardError(409, "draft_closed")
    d = _draft(row)
    today = db.today_local(now)
    delta = (date_cls.fromisoformat(today) - date_cls.fromisoformat(d["date"])).days
    if d.get("kind") == "exchange":
        return {"ok": True, "id": card_id, "kind": "exchange", "give": d["give"], "get": d["get"],
                "fee": d.get("fee"), "note": d["note"], "date": d["date"],
                "date_key": {0: "today", 1: "yesterday"}.get(delta, "pick"), "heard": row["heard"] or "",
                "source": row["source"]}
    return {"ok": True, "id": card_id, "direction": d["direction"], "amount": d["amount"], "currency": d["currency"],
            "note": d["note"], "category": d["category"], "method": d.get("method"), "date": d["date"],
            "date_key": {0: "today", 1: "yesterday"}.get(delta, "pick"), "heard": row["heard"] or "",
            "source": row["source"]}


def _cents_text(cents: int) -> str:
    whole, frac = divmod(cents, 100)
    return str(whole) if frac == 0 else "%d.%02d" % (whole, frac)


def _note_warned(conn, card_id, warnings: list, now) -> None:
    """Запомнить, что владельцу показали предупреждение (первый показ - начало отсчёта MIN_CONFIRM_SECONDS)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT warned, status FROM cards WHERE id=?", (card_id,)).fetchone()
        if row is not None and row["status"] == "pending":
            old = json.loads(row["warned"] or "[]")
            new = sorted(set(old) | set(warnings))
            if new != sorted(old):
                conn.execute("UPDATE cards SET warned=?, warned_at=? WHERE id=?",
                             (json.dumps(new), _iso(now), card_id))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def confirm_draft(conn: sqlite3.Connection, actor_id, card_id, data: dict, webapp_url: str, test: bool,
                  fx_rates: dict | None = None, now: datetime | None = None) -> dict:
    """«Записать» в форме, открытой по черновику (подписанный запрос владельца). Идемпотентно по 'card:<id>':
    повтор того же запроса даёт ту же запись, форма и карточка не пишут дважды. Предупреждение о сумме
    (outlier, возможный дубль) подтверждается только после показа и не раньше чем через MIN_CONFIRM_SECONDS:
    случайный двойной тап его не снимает."""
    now = now or datetime.now(timezone.utc)
    expire_old(conn, now)
    _load(conn, card_id)
    if not isinstance(data, dict):
        raise records.RecordError(422, "bad_payload")

    def guard(c):
        r = _load(c, card_id)
        if r["actor_id"] != str(actor_id):
            raise CardError(403, "not_yours")
        if r["status"] != "pending":
            raise CardError(409, "draft_closed")
        if _draft(r).get("kind") == "exchange":
            raise CardError(409, "draft_is_exchange")       # обмен записывается только через /api/exchanges

    def gate(c, warnings, confirm):
        r = _load(c, card_id)
        shown = json.loads(r["warned"] or "[]")
        try:
            age = (now - datetime.strptime(r["warned_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                   ).total_seconds() if r["warned_at"] else None
        except ValueError:
            age = None
        if age is None or age < MIN_CONFIRM_SECONDS:
            return []
        return [w for w in confirm if w in shown]

    def created(c, rec_id, info):
        r = _load(c, card_id)
        d = _draft(r)
        d.update(direction=info["direction"], amount=_cents_text(info["cents"]), currency=info["currency"],
                 note=info["note"] or "", category=info["category"], date=info["date"], method=info["method"],
                 currency_guess=False)
        c.execute("UPDATE cards SET draft=? WHERE id=?", (json.dumps(d, ensure_ascii=False), card_id))
        _close(c, card_id, "confirmed", now, rec_id)
        view = render(c, _load(c, card_id), webapp_url, test, now)
        _edit_in_chat(c, _load(c, card_id), view["text"], view["buttons"], now)

    try:
        return records.create_record(conn, actor_id, data, via="webapp", now=now, fx_rates=fx_rates,
                                     idem_key="card:%d" % card_id, guard=guard, confirm_gate=gate, on_created=created)
    except records.RecordError as exc:
        if exc.code == "confirm_required":
            _note_warned(conn, card_id, exc.extra.get("warnings", []), now)
        raise


def confirm_exchange_draft(conn: sqlite3.Connection, actor_id, card_id, data: dict, webapp_url: str, test: bool,
                           fx_rates: dict | None = None, now: datetime | None = None) -> dict:
    """«Записать» в форме обмена, открытой по черновику (подписанный запрос владельца). Идемпотентно по 'card:<id>';
    проверка черновика, запись обмена, закрытие черновика и правка карточки в чате - одна транзакция. Обычный черновик
    сюда не принимается (draft_not_exchange)."""
    now = now or datetime.now(timezone.utc)
    expire_old(conn, now)
    _load(conn, card_id)

    def guard(c):
        r = _load(c, card_id)
        if r["actor_id"] != str(actor_id):
            raise CardError(403, "not_yours")
        if r["status"] != "pending":
            raise CardError(409, "draft_closed")
        if _draft(r).get("kind") != "exchange":
            raise CardError(409, "draft_not_exchange")

    def created(c, ex_id):
        _close(c, card_id, "confirmed", now, ex_id)
        view = render(c, _load(c, card_id), webapp_url, test, now, fx_rates)
        _edit_in_chat(c, _load(c, card_id), view["text"], view["buttons"], now)

    return exchanges.create_exchange(conn, actor_id, data, via="webapp", now=now, fx_rates=fx_rates,
                                     idem_key="card:%d" % card_id, guard=guard, on_created=created)


def cancel_draft(conn: sqlite3.Connection, actor_id, card_id, webapp_url: str = "", now: datetime | None = None) -> dict:
    """Удалить черновик из «Входящих» (подписанный запрос владельца)."""
    now = now or datetime.now(timezone.utc)
    expire_old(conn, now)
    row = _load(conn, card_id)
    if row["actor_id"] != str(actor_id):
        raise CardError(403, "not_yours")
    if row["status"] != "pending":
        raise CardError(409, "draft_closed")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("UPDATE cards SET status='cancelled', closed_at=? WHERE id=? AND status='pending'",
                           (_iso(now), card_id))
        if cur.rowcount:
            conn.execute("INSERT INTO audit(ts, actor_id, action, entity, entity_id, via, detail) "
                         "VALUES (?,?,?,?,?,?,?)", (_iso(now), str(actor_id), "card_cancel", "card", str(card_id),
                                                    "webapp", json.dumps({})))
            _edit_in_chat(conn, _load(conn, card_id), CANCELLED_TEXT, [], now)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"ok": True, "id": card_id}


def inbox(conn: sqlite3.Connection, actor_id, now: datetime | None = None) -> list[dict]:
    """Открытые черновики владельца для вкладки «Входящие» (подписанный запрос)."""
    now = now or datetime.now(timezone.utc)
    expire_old(conn, now)
    today = db.today_local(now)
    out = []
    for row in conn.execute("SELECT * FROM cards WHERE actor_id=? AND status='pending' ORDER BY id DESC LIMIT 50",
                            (str(actor_id),)):
        d = _draft(row)
        if d.get("kind") == "exchange":
            gives = exchanges.money_text(money.parse_amount_to_cents(d["give"]["amount"]), d["give"]["currency"])
            gets = ", ".join(exchanges.money_text(money.parse_amount_to_cents(g["amount"]), g["currency"])
                             for g in d["get"])
            out.append({"id": row["id"], "source": row["source"], "heard": row["heard"] or "", "kind": "exchange",
                        "summary": "Обмен: отдал %s → получил %s" % (gives, gets),
                        "date_text": fmt_date(d["date"], today),
                        "method_title": _method_title(conn, d["give"].get("method")), "created_at": row["created_at"]})
            continue
        out.append({"id": row["id"], "source": row["source"], "heard": row["heard"] or "", "direction": d["direction"],
                    "amount": d["amount"], "currency": d["currency"], "category": d["category"],
                    "date_text": fmt_date(d["date"], today), "method_title": _method_title(conn, d.get("method")),
                    "created_at": row["created_at"]})
    return out


def set_message_id(conn: sqlite3.Connection, card_id, message_id, actor_id=None) -> None:
    """Запомнить id сообщения карточки (для правки в чате). actor_id задан - карточка должна быть его."""
    row = _load(conn, card_id)
    if actor_id is not None and row["actor_id"] != str(actor_id):
        raise CardError(403, "not_yours")
    conn.execute("UPDATE cards SET message_id=? WHERE id=? AND message_id IS NULL", (str(message_id), card_id))


def view(conn: sqlite3.Connection, card_id, webapp_url: str, test: bool, now: datetime | None = None,
         fx_rates: dict | None = None) -> dict:
    return render(conn, _load(conn, card_id), webapp_url, test, now, fx_rates)


class ExpireWorker:
    """Фон: раз в interval секунд закрывает просроченные черновики (правит их сообщения в чате)."""

    def __init__(self, connect, interval: float = 600.0):
        self.connect, self.interval = connect, interval

    def start(self):
        import threading
        threading.Thread(target=self._loop, name="kassa-expire", daemon=True).start()

    def _loop(self):
        import sys
        import time
        while True:
            try:
                conn = self.connect()
                try:
                    expire_old(conn)
                finally:
                    conn.close()
            except Exception as exc:        # noqa: BLE001
                sys.stderr.write("expire error: %s\n" % type(exc).__name__)
            time.sleep(self.interval)


# --- вечернее напоминание (выключено флагом KASSA_REMINDER_ENABLED, время - KASSA_REMINDER_TIME) ----------------------
REMINDER_HOUR, REMINDER_MINUTE = 21, 30
REMINDER_TEXT = "Сегодня в кассе нет записей. Если были траты или поступления, откройте Кассу."
# Отметки дня (daily_marks.kind): «деньги не тратились, записывать нечего». Ставятся кнопками напоминания и текстом
# «только карта» / «не тратил». Денег и транзакций не создают, поэтому доступны токену бота.
DAY_MARK_TEXT = {
    "cardonly": "Принято: {when} только карта. Нал 0, карту возьму из выписки.",
    "nospend": "Принято: {when} без трат.",
}
DAY_MARK_KINDS = tuple(DAY_MARK_TEXT)


def day_mark(conn: sqlite3.Connection, actor_id, kind: str, day: str | None = None,
             now: datetime | None = None, via: str = "card") -> dict:
    """Отметка дня «Только карта» (cardonly) / «Не тратил» (nospend): кнопка под напоминанием (day из callback_data)
    или текст в чате (day=None - сегодня по местному времени). Идемпотентно: повтор не добавляет ни отметку, ни строку
    audit. Денег и транзакций не создаёт, не читает и не отменяет, поэтому допустимо через внутренний токен бота
    (все денежные действия - только с подписью Mini App). Принимаются сегодня и вчера
    (напоминание могли нажать после полуночи), любой другой день - «кнопка устарела». via - откуда отметка, идёт в
    audit.via: 'card' (кнопка напоминания) или 'text' (фраза из чата).
    -> {"answer", "edit": {"text", "buttons"} | None, "text"?}."""
    if via not in ("card", "text"):
        raise CardError(422, "bad_request")
    now = now or datetime.now(timezone.utc)
    today = db.today_local(now)
    yesterday = (date_cls.fromisoformat(today) - timedelta(days=1)).isoformat()
    day = today if day is None else day
    if kind not in DAY_MARK_TEXT or day not in (today, yesterday):
        return {"answer": STALE_BUTTON, "edit": None}
    text = DAY_MARK_TEXT[kind].format(when="сегодня" if day == today else "вчера")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("INSERT OR IGNORE INTO daily_marks(day, kind, created_at) VALUES (?,?,?)",
                           (day, kind, _iso(now)))
        if cur.rowcount:
            conn.execute("INSERT INTO audit(ts, actor_id, action, entity, entity_id, via, detail) "
                         "VALUES (?,?,?,?,?,?,?)", (_iso(now), str(actor_id), "day_mark", "day", day, via,
                                                    json.dumps({"kind": kind})))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"answer": "Принято", "edit": {"text": text, "buttons": []}, "text": text}


def reminder_tick(conn: sqlite3.Connection, chat_id, webapp_url: str, now: datetime | None = None) -> bool:
    """Один тик: после 21:30 местного, если за день нет активных записей, день не отмечен («Только карта» /
    «Не тратил») и напоминание ещё не ставилось, кладёт сообщение в outbox (без сумм: «Открыть кассу» и под ней
    две кнопки отметки дня). Возвращает True, если поставило."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(db.LOCAL_TZ)
    if (local.hour, local.minute) < (REMINDER_HOUR, REMINDER_MINUTE):
        return False
    day = local.strftime("%Y-%m-%d")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM daily_marks WHERE day=? AND kind='reminder'", (day,)).fetchone():
            conn.execute("COMMIT")
            return False
        have = conn.execute("SELECT 1 FROM transactions WHERE occurred_on=? AND status='active' "
                            "AND source <> 'month-close' AND closed_by IS NULL LIMIT 1", (day,)).fetchone()
        marked = conn.execute("SELECT 1 FROM daily_marks WHERE day=? AND kind IN ('cardonly','nospend')",
                              (day,)).fetchone()
        conn.execute("INSERT INTO daily_marks(day, kind, created_at) VALUES (?, 'reminder', ?)", (day, _iso(now)))
        if not have and not marked:
            conn.execute("INSERT INTO outbox(created_at, kind, chat_id, payload) VALUES (?,?,?,?)",
                         (_iso(now), "send", str(chat_id), json.dumps(
                             {"text": REMINDER_TEXT,
                              "buttons": [[{"text": "Открыть кассу", "web_app": webapp_url}],
                                          [{"text": "💳 Только карта", "cb": "%s:day:cardonly:%s" % (CB_PREFIX, day)},
                                           {"text": "🚫 Не тратил", "cb": "%s:day:nospend:%s" % (CB_PREFIX, day)}]]},
                             ensure_ascii=False)))
        conn.execute("COMMIT")
        return not have and not marked
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


class ReminderWorker:
    def __init__(self, connect, chat_id, webapp_url, interval: float = 60.0):
        self.connect, self.chat_id, self.webapp_url, self.interval = connect, chat_id, webapp_url, interval

    def start(self):
        import threading
        threading.Thread(target=self._loop, name="kassa-reminder", daemon=True).start()

    def _loop(self):
        import sys
        import time
        while True:
            try:
                conn = self.connect()
                try:
                    reminder_tick(conn, self.chat_id, self.webapp_url)
                finally:
                    conn.close()
            except Exception as exc:        # noqa: BLE001
                sys.stderr.write("reminder error: %s\n" % type(exc).__name__)
            time.sleep(self.interval)
