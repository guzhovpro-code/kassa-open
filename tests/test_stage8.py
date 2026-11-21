"""Бот и черновики-карточки (после ревью 15.10.2026): подтверждение записи только подписью Telegram,
внутренний токен создаёт и отменяет черновики и больше ничего. Лимиты, срок жизни, гонки, outbox, напоминание."""
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from helpers import OWNER, STRANGER, make_init_data
from kassa_api import cards, db, records
from test_api import INTERNAL, PRIV, TOKEN, env, req, rows  # noqa: F401  (фикстура env)

URL = "https://example.test/tok/"
NOW = datetime(2026, 10, 15, 9, 0, 0, tzinfo=timezone.utc)      # 12:00 по местному времени (UTC+3)


@pytest.fixture()
def conn(tmp_path):
    p = str(tmp_path / "kassa-test.db")
    db.init_db(p)
    c = db.connect(p)
    yield c
    c.close()


def make(conn, text="потратил 20 лир на обед", source="phrase", mid="10", now=NOW, **kw):
    return cards.create_from_text(conn, OWNER, OWNER, source, text, mid, now=now, **kw)


def tx_count(conn):
    return conn.execute("SELECT COUNT(*) FROM transactions WHERE source='kassa'").fetchone()[0]


def act(conn, cid, action, value=None, uid=OWNER, now=NOW):
    return cards.act(conn, uid, cid, action, value, URL, True, now=now)


def form(**kw):
    body = {"client_id": str(uuid.uuid4()), "direction": "out", "amount": "20", "currency": "TRY",
            "date": "2026-10-15", "method": "cash", "note": "обед"}
    body.update(kw)
    return body


def confirm(conn, cid, now=NOW, **kw):
    return cards.confirm_draft(conn, OWNER, cid, form(**kw), URL, True, now=now)


def draft(conn, cid):
    return json.loads(conn.execute("SELECT draft FROM cards WHERE id=?", (cid,)).fetchone()[0])


# --- создание черновика -------------------------------------------------------------------------------------------
def test_card_creation_does_not_write_money(conn):
    r = make(conn)
    assert r["ok"] and not r["duplicate"]
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    v = cards.view(conn, r["card_id"], URL, True, NOW)
    assert "20 TRY" in v["text"] and "кафе" in v["text"] and "ТЕСТ" in v["text"]
    flat = [b for row in v["buttons"] for b in row]
    labels = {b["text"]: b for b in flat}
    # «Записать», «Исправить», «Это поступление» открывают Mini App на экране этого черновика, а не шлют callback
    for name in ("✅ Записать", "✏️ Исправить", "🔁 Это поступление"):
        assert labels[name]["web_app"].startswith(URL + "?draft=%d" % r["card_id"]) and "cb" not in labels[name]
    assert labels["✅ Записать"]["web_app"].endswith("&ok=1") and labels["🔁 Это поступление"]["web_app"].endswith("&flip=1")
    # callback остались только у отмены черновика
    assert sorted(b["cb"].split(":")[1] for b in flat if "cb" in b) == ["notmoney"]
    assert all(len(b["cb"].encode()) <= 64 for b in flat if "cb" in b)


def test_voice_card_has_verno_in_miniapp_and_cancel_callback(conn):
    cid = make(conn, "пятьсот лир кофе", source="voice")["card_id"]
    flat = [b for row in cards.view(conn, cid, URL, True, NOW)["buttons"] for b in row]
    assert {b["text"] for b in flat} == {"✅ Верно", "✏️ Исправить", "✖ Отмена"}
    assert [b["cb"] for b in flat if "cb" in b] == ["kassa:no:%d" % cid]
    assert all("web_app" in b for b in flat if "cb" not in b)


def test_same_source_message_gives_one_card(conn):
    a, b = make(conn), make(conn)
    assert b["duplicate"] and a["card_id"] == b["card_id"]
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1


def test_duplicate_is_resent_only_when_chat_message_is_missing_and_text_matches(conn):
    cid = make(conn)["card_id"]
    again = make(conn)
    assert again["duplicate"] and again["resend"]                      # карточка есть, сообщения в чате нет
    other = make(conn, "потратил 99 лир на секретное")                 # тот же id, другой текст: чужую карточку не отдаём,
    assert other["ok"] is False and other["reject"] == "msg_conflict"  # и не молчим: отказ с подсказкой (ревью 15.10, P2-1 в)
    cards.set_message_id(conn, cid, 700)
    assert not make(conn)["resend"]                                    # сообщение в чате есть: повторять нечего


def test_unsure_and_no_amount_give_no_card_and_hint_diary(conn):
    for text, code in [("потратил несколько тысяч лир", "unsure_amount"), ("кофе без суммы", "no_amount")]:
        r = make(conn, text, mid=text)
        assert not r["ok"] and r["reject"] == code and "Откройте Кассу" in r["reply"] and "Дневник:" in r["reply"]
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0


def test_voice_uses_armed_direction_default(conn):
    r = make(conn, "пятьсот кофе", source="voice", default_direction="in")
    assert draft(conn, r["card_id"])["direction"] == "in" and draft(conn, r["card_id"])["amount"] == "500"
    assert draft(conn, r["card_id"])["currency_guess"] is True


# --- лимиты и срок жизни (токен доступен модели) --------------------------------------------------------------------
def test_pending_limit_blocks_new_drafts(conn):
    for i in range(cards.MAX_PENDING):
        assert make(conn, mid="m%d" % i)["ok"]
    r = make(conn, mid="over")
    assert not r["ok"] and r["reject"] == "too_many" and "Входящие" in r["reply"]
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == cards.MAX_PENDING
    cards.cancel_draft(conn, OWNER, 1, URL, NOW)                         # освободили место
    assert make(conn, mid="after")["ok"]


def test_hourly_rate_limit_counts_closed_drafts_too(conn):
    for i in range(cards.MAX_CREATED_PER_HOUR):
        cid = make(conn, mid="r%d" % i)["card_id"]
        act(conn, cid, "no")                                            # закрыли: pending не копится
    r = make(conn, mid="hour")
    assert not r["ok"] and r["reject"] == "rate_limit"
    later = NOW + timedelta(hours=1, minutes=1)
    assert make(conn, mid="later", now=later)["ok"]
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action='card_refused'").fetchone()[0] == 1


def test_drafts_expire_after_a_day_and_chat_message_is_edited(conn):
    cid = make(conn)["card_id"]
    cards.set_message_id(conn, cid, 555)
    day = NOW + timedelta(hours=cards.DRAFT_TTL_HOURS - 1)
    assert cards.expire_old(conn, day) == 0
    late = NOW + timedelta(hours=cards.DRAFT_TTL_HOURS + 1)
    assert cards.expire_old(conn, late) == 1
    assert conn.execute("SELECT status FROM cards WHERE id=?", (cid,)).fetchone()[0] == "cancelled"
    ob = conn.execute("SELECT kind, payload FROM outbox").fetchone()
    assert ob["kind"] == "edit" and json.loads(ob["payload"])["message_id"] == "555"
    assert "устарел" in json.loads(ob["payload"])["text"]
    with pytest.raises(cards.CardError) as e:
        cards.draft_for_form(conn, OWNER, cid, late)
    assert e.value.status == 409
    with pytest.raises(cards.CardError):
        confirm(conn, cid, now=late)
    assert tx_count(conn) == 0


# --- токен бота: только отмена черновика ------------------------------------------------------------------------------
def test_act_accepts_only_cancel_actions_and_answers_without_card_text(conn):
    cid = make(conn, "потратил 777 лир на секретное")["card_id"]
    for action in ("ok", "m", "more", "flip", "undo", "redo", "okx", "что-угодно"):
        res = act(conn, cid, action, "cash")
        assert res["edit"] is None and "устарела" in res["answer"]
        assert "777" not in json.dumps(res, ensure_ascii=False) and "секретн" not in json.dumps(res, ensure_ascii=False)
    assert tx_count(conn) == 0 and conn.execute("SELECT status FROM cards").fetchone()[0] == "pending"
    res = act(conn, cid, "no")
    assert res["answer"] == "Отменено" and "777" not in json.dumps(res, ensure_ascii=False)
    assert conn.execute("SELECT status FROM cards").fetchone()[0] == "cancelled"
    again = act(conn, cid, "no")
    assert "закрыта" in again["answer"] and again["edit"] is None


def test_notmoney_closes_with_diary_hint(conn):
    cid = make(conn)["card_id"]
    res = act(conn, cid, "notmoney")
    assert "Дневник:" in res["edit"]["text"] and tx_count(conn) == 0


def test_foreign_user_cannot_act_or_confirm(conn):
    cid = make(conn)["card_id"]
    with pytest.raises(cards.CardError) as e:
        act(conn, cid, "no", uid=STRANGER)
    assert e.value.status == 403
    with pytest.raises(cards.CardError) as e:
        cards.confirm_draft(conn, STRANGER, cid, form(), URL, True, now=NOW)
    assert e.value.status == 403 and tx_count(conn) == 0
    assert conn.execute("SELECT status FROM cards").fetchone()[0] == "pending"


# --- запись из Mini App по черновику ------------------------------------------------------------------------------------
def test_confirm_writes_once_closes_draft_and_edits_chat(conn):
    cid = make(conn)["card_id"]
    cards.set_message_id(conn, cid, 555)
    res = confirm(conn, cid, amount="25", note="обед и кофе")
    assert res["ok"] and not res["duplicate_request"] and tx_count(conn) == 1
    row = conn.execute("SELECT * FROM transactions WHERE source='kassa'").fetchone()
    assert (row["idem_key"], row["amount_cents"], row["via"], row["method"]) == ("card:%d" % cid, 2500, "webapp", "cash")
    card = conn.execute("SELECT * FROM cards").fetchone()
    assert card["status"] == "confirmed" and card["tx_id"] == row["id"]
    assert draft(conn, cid)["amount"] == "25" and draft(conn, cid)["note"] == "обед и кофе"      # записано то, что на экране
    ob = json.loads(conn.execute("SELECT payload FROM outbox").fetchone()[0])
    assert ob["message_id"] == "555" and "Записано №%d" % row["id"] in ob["text"] and "25 TRY" in ob["text"]
    assert ob["buttons"][0][0]["web_app"] == URL + "?tab=recent"
    again = confirm(conn, cid, amount="25", note="обед и кофе")                 # повтор того же запроса (двойной тап)
    assert again["duplicate_request"] and again["id"] == res["id"] and tx_count(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_confirm_with_other_values_after_write_conflicts_not_duplicates(conn):
    cid = make(conn)["card_id"]
    confirm(conn, cid)
    with pytest.raises(records.RecordError) as e:
        confirm(conn, cid, amount="999")
    assert e.value.code == "idem_conflict" and tx_count(conn) == 1


def test_confirm_requires_open_draft(conn):
    cid = make(conn)["card_id"]
    act(conn, cid, "no")
    with pytest.raises(cards.CardError) as e:
        confirm(conn, cid)
    assert e.value.code == "draft_closed" and tx_count(conn) == 0


def test_confirm_requires_method(conn):
    cid = make(conn)["card_id"]
    with pytest.raises(records.RecordError) as e:
        confirm(conn, cid, method=None)
    assert e.value.code == "method_required" and tx_count(conn) == 0
    assert conn.execute("SELECT status FROM cards").fetchone()[0] == "pending"


def test_closed_month_refused_and_draft_stays_open(conn):
    conn.execute("INSERT INTO closed_months(month, closed_at, source_name, source_sha, expected, rows_in, rows_out) "
                 "VALUES ('2026-09','x','f','s','{}',0,0)")
    cid = make(conn, "потратил 20 лир")["card_id"]
    with pytest.raises(records.RecordError) as e:
        confirm(conn, cid, date="2026-09-20")
    assert e.value.code == "month_closed" and tx_count(conn) == 0
    assert conn.execute("SELECT status FROM cards").fetchone()[0] == "pending"


def seed_expenses(conn):
    for i in range(10):
        records.create_record(conn, OWNER, {"client_id": "c%015d" % i, "direction": "out", "amount": str(100 + i),
                                           "currency": "TRY", "category": "кафе", "date": "2026-10-14",
                                           "method": "cash"}, now=NOW)


def test_outlier_warning_is_not_cleared_by_double_tap(conn):
    seed_expenses(conn)
    cid = make(conn, "потратил 5000 лир")["card_id"]
    with pytest.raises(records.RecordError) as e:
        confirm(conn, cid, amount="5000")
    assert e.value.code == "confirm_required" and "outlier" in e.value.extra["warnings"]
    # второй тап сразу (меньше двух секунд) предупреждение не снимает, даже если форма прислала confirm
    with pytest.raises(records.RecordError) as e:
        confirm(conn, cid, amount="5000", confirm=["outlier"], now=NOW + timedelta(seconds=1))
    assert e.value.code == "confirm_required" and tx_count(conn) == 10
    ok = confirm(conn, cid, amount="5000", confirm=["outlier"], now=NOW + timedelta(seconds=4))
    assert ok["ok"] and tx_count(conn) == 11


def test_confirm_list_without_shown_warning_is_ignored(conn):
    seed_expenses(conn)
    cid = make(conn, "потратил 5000 лир")["card_id"]
    with pytest.raises(records.RecordError) as e:
        confirm(conn, cid, amount="5000", confirm=["outlier"], now=NOW + timedelta(seconds=30))
    assert e.value.code == "confirm_required" and tx_count(conn) == 10


def test_parallel_confirms_write_one_record(tmp_path):
    p = str(tmp_path / "kassa-test.db")
    db.init_db(p)
    c0 = db.connect(p)
    cid = make(c0)["card_id"]
    c0.close()
    results, errors = [], []

    def worker():
        c = db.connect(p)
        try:
            results.append(cards.confirm_draft(c, OWNER, cid, form(client_id=str(uuid.uuid4())), URL, True, now=NOW))
        except Exception as exc:        # noqa: BLE001
            errors.append(exc)
        finally:
            c.close()
    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    c = db.connect(p)
    assert c.execute("SELECT COUNT(*) FROM transactions WHERE source='kassa'").fetchone()[0] == 1
    assert not errors and len({r["id"] for r in results}) == 1 and sum(not r["duplicate_request"] for r in results) == 1
    c.close()


# --- «Входящие» -----------------------------------------------------------------------------------------------------------
def test_inbox_lists_pending_only_and_cancel_edits_chat(conn):
    a, b = make(conn, mid="1")["card_id"], make(conn, "получил 300 usdt", mid="2")["card_id"]
    cards.set_message_id(conn, a, 701)
    items = cards.inbox(conn, OWNER, NOW)
    assert [i["id"] for i in items] == [b, a] and items[1]["heard"] == "потратил 20 лир на обед"
    assert cards.inbox(conn, STRANGER, NOW) == []
    cards.cancel_draft(conn, OWNER, a, URL, NOW)
    assert [i["id"] for i in cards.inbox(conn, OWNER, NOW)] == [b]
    ob = json.loads(conn.execute("SELECT payload FROM outbox").fetchone()[0])
    assert ob["message_id"] == "701" and "Отменено" in ob["text"]
    with pytest.raises(cards.CardError):
        cards.cancel_draft(conn, STRANGER, b, URL, NOW)


# --- форма: значения черновика ----------------------------------------------------------------------------------------------
def test_draft_for_form_values(conn):
    cid = make(conn)["card_id"]
    d = cards.draft_for_form(conn, OWNER, cid, NOW)
    assert d["amount"] == "20" and d["date_key"] == "today" and d["direction"] == "out" and d["source"] == "phrase"
    with pytest.raises(cards.CardError) as e:
        cards.draft_for_form(conn, STRANGER, cid, NOW)
    assert e.value.status == 403


# --- взвод голоса -------------------------------------------------------------------------------------------------------
def test_arm_expires_after_ten_minutes(conn):
    cards.arm_voice(conn, OWNER, "in", NOW)
    assert cards.armed(conn, OWNER, NOW + timedelta(minutes=9)) == "in"
    assert cards.armed(conn, OWNER, NOW + timedelta(minutes=11)) is None
    assert cards.armed(conn, STRANGER, NOW) is None


# --- outbox ---------------------------------------------------------------------------------------------------------------
def test_outbox_lease_ack_and_retry(conn):
    i = cards.outbox_add(conn, "send", OWNER, {"text": "x"}, NOW)
    got = cards.outbox_claim(conn, NOW)
    assert [g["id"] for g in got] == [i]
    assert cards.outbox_claim(conn, NOW + timedelta(seconds=5)) == []                 # занято
    assert [g["id"] for g in cards.outbox_claim(conn, NOW + timedelta(seconds=90))] == [i]   # аренда вышла
    assert cards.outbox_ack(conn, i, True, NOW, message_id=4242) == "sent"
    assert conn.execute("SELECT message_id FROM outbox WHERE id=?", (i,)).fetchone()[0] == "4242"
    assert cards.outbox_claim(conn, NOW + timedelta(hours=1)) == []


def test_outbox_gives_up_after_attempts(conn):
    i = cards.outbox_add(conn, "send", OWNER, {"text": "x"}, NOW)
    t = NOW
    status = "pending"
    for _ in range(6):
        t += timedelta(minutes=5)
        cards.outbox_claim(conn, t)
        status = cards.outbox_ack(conn, i, False, t)
    assert status == "failed"


def test_outbox_never_acked_item_is_not_handed_out_forever(conn):
    i = cards.outbox_add(conn, "send", OWNER, {"text": "x"}, NOW)
    t, handed = NOW, 0
    for _ in range(12):                              # бот берёт и падает до ack каждый раз
        t += timedelta(minutes=5)
        handed += len(cards.outbox_claim(conn, t))
    assert handed == 5
    assert conn.execute("SELECT status FROM outbox WHERE id=?", (i,)).fetchone()[0] == "failed"


# --- напоминание (флаг, по умолчанию выкл.) ------------------------------------------------------------------------------------
def test_reminder_only_without_records_after_2130_and_once(conn):
    early = datetime(2026, 10, 15, 17, 0, tzinfo=timezone.utc)       # 20:00 по местному времени (UTC+3)
    late = datetime(2026, 10, 15, 18, 40, tzinfo=timezone.utc)       # 21:40
    assert cards.reminder_tick(conn, OWNER, URL, early) is False
    assert cards.reminder_tick(conn, OWNER, URL, late) is True
    assert cards.reminder_tick(conn, OWNER, URL, late + timedelta(minutes=1)) is False
    item = conn.execute("SELECT payload FROM outbox").fetchone()[0]
    assert "web_app" in item and "TRY" not in item and not any(ch.isdigit() for ch in json.loads(item)["text"])


def test_reminder_silent_when_day_has_records(conn):
    late = datetime(2026, 10, 15, 18, 40, tzinfo=timezone.utc)
    records.create_record(conn, OWNER, {"client_id": "r" * 16, "direction": "out", "amount": "5", "currency": "TRY",
                                      "category": "кафе", "date": "2026-10-15", "method": "cash"}, now=late)
    assert cards.reminder_tick(conn, OWNER, URL, late) is False
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_alarm_once_per_day_per_kind(conn):
    assert cards.alarm_once(conn, OWNER, "rate:ecb:TRY", "курс", NOW) is True
    assert cards.alarm_once(conn, OWNER, "rate:ecb:TRY", "курс", NOW + timedelta(hours=1)) is False
    assert cards.alarm_once(conn, OWNER, "rate:ecb:RUB", "курс", NOW) is True
    assert cards.alarm_once(conn, OWNER, "rate:ecb:TRY", "курс", NOW + timedelta(days=1)) is True
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 3


# --- HTTP: внутренний и публичный порты -----------------------------------------------------------------------------------
def internal_port(env_):
    return env_[2]


@pytest.fixture()
def srv(tmp_path):
    from helpers import BOT_ID
    from kassa_api.server import Config, make_servers
    from test_api import PUB
    dbp = str(tmp_path / "kassa-test.db")
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), INTERNAL, PUB, dbp)
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0))
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield pub.server_address[1], internal.server_address[1], dbp
    for s in (pub, internal):
        s.shutdown()
        s.server_close()


def ireq(srv, method, path, body=None, token=INTERNAL):
    h = {"Authorization": "Bearer " + token} if token else {}
    return req(srv[1], method, path, body=body, headers=h)


def pub_post(srv, path, body, init="default"):
    return req(srv[0], "POST", "/%s/%s" % (TOKEN, path), body=body,
               init=make_init_data(PRIV) if init == "default" else init)


def pub_get(srv, path, init="default"):
    return req(srv[0], "GET", "/%s/%s" % (TOKEN, path), init=make_init_data(PRIV) if init == "default" else init)


def phrase(srv, text="потратил 20 лир на обед", mid=9):
    return ireq(srv, "POST", "/internal/phrase", {"user_id": OWNER, "text": text, "message_id": mid})


def test_internal_api_needs_token(srv):
    for m, path in [("GET", "/internal/armed?user=%d" % OWNER), ("GET", "/internal/outbox"),
                    ("POST", "/internal/voice-text"), ("POST", "/internal/phrase"), ("POST", "/internal/card-action")]:
        assert ireq(srv, m, path, body={} if m == "POST" else None, token="")[0] == 401
        assert ireq(srv, m, path, body={} if m == "POST" else None, token="x" * 40)[0] == 401


def test_internal_rejects_unknown_user(srv):
    st, _ = ireq(srv, "POST", "/internal/phrase", {"user_id": STRANGER, "text": "потратил 20 лир", "message_id": 1})
    assert st == 403 and rows(srv[2]) == 0
    assert ireq(srv, "GET", "/internal/armed?user=%d" % STRANGER)[0] == 403


def test_token_cannot_write_cancel_or_read_anything(srv):
    """Эталонная проверка P1-4: всё, что токен умеет, - черновик, его отмена и очередь исходящих."""
    st, b = phrase(srv, "потратил 777 лир на секретное")
    cid = b["card_id"]
    assert st == 200 and "777" in b["text"]                 # текст видит тот, кто сам прислал фразу
    # 1. подтвердить запись нельзя ни одним действием
    for action in ("ok", "m", "more", "flip", "undo", "redo", "okx", "confirm", "write"):
        st, out = ireq(srv, "POST", "/internal/card-action",
                       {"user_id": OWNER, "action": action, "card_id": cid, "value": "cash"})
        assert st == 200 and "устарела" in out["answer"] and out["edit"] is None
        assert "777" not in json.dumps(out, ensure_ascii=False)
    assert rows(srv[2]) == 0
    # 2. на публичной стороне токен не работает: нужна подпись Telegram
    for path in ("api/records", "api/draft/cancel", "api/voice/arm"):
        assert req(srv[0], "POST", "/%s/%s" % (TOKEN, path), body={"id": cid}, headers={"Authorization": "Bearer " + INTERNAL})[0] == 401
    assert req(srv[0], "POST", "/%s/api/records" % TOKEN, body=dict(form(), draft_id=cid),
               headers={"Authorization": "tma " + INTERNAL})[0] == 401
    assert req(srv[0], "GET", "/%s/api/draft?id=%d" % (TOKEN, cid), headers={"Authorization": "Bearer " + INTERNAL})[0] == 401
    assert req(srv[0], "GET", "/%s/api/inbox" % TOKEN, headers={"Authorization": "Bearer " + INTERNAL})[0] == 401
    # 3. записей токеном не отменить и не вернуть: этих путей на внутреннем порту нет
    for path in ("/internal/records", "/internal/cancel", "/internal/restore", "/api/records/1/cancel"):
        assert ireq(srv, "POST", path, {"user_id": OWNER, "id": 1})[0] == 404
    assert rows(srv[2]) == 0
    # 4. единственное, что токен делает с карточкой, - отмена; ответ без текста карточки
    st, out = ireq(srv, "POST", "/internal/card-action", {"user_id": OWNER, "action": "no", "card_id": cid})
    assert st == 200 and out["answer"] == "Отменено" and "777" not in json.dumps(out, ensure_ascii=False)


def test_token_draft_creation_is_rate_limited(srv):
    ok = 0
    for i in range(cards.MAX_PENDING + 3):
        st, b = phrase(srv, "потратил %d лир" % (10 + i), mid=100 + i)
        assert st == 200
        ok += bool(b["ok"])
    assert ok == cards.MAX_PENDING and b["reject"] == "too_many" and rows(srv[2]) == 0


def test_voice_flow_over_http_needs_signed_confirm(srv):
    assert ireq(srv, "GET", "/internal/armed?user=%d" % OWNER)[1]["armed"] is None
    st, b = pub_post(srv, "api/voice/arm", {"direction": "out"})
    assert st == 200 and b["ok"]
    assert ireq(srv, "GET", "/internal/armed?user=%d" % OWNER)[1]["armed"] == "out"
    st, b = ireq(srv, "POST", "/internal/voice-text", {"user_id": OWNER, "text": "пятьсот лир кофе", "message_id": 42})
    assert st == 200 and b["ok"] and b["card_id"] and "500 TRY" in b["text"] and "Услышал" in b["text"]
    assert rows(srv[2]) == 0                                              # карточка денег не пишет
    assert ireq(srv, "GET", "/internal/armed?user=%d" % OWNER)[1]["armed"] is None      # взвод снят после карточки
    cid = b["card_id"]
    body = form(draft_id=cid, amount="500", note="кофе")
    st, r = pub_post(srv, "api/records", body, init=None)                  # «Верно» без подписи
    assert st == 401 and rows(srv[2]) == 0
    st, r = pub_post(srv, "api/records", body)                             # «Записать» с подписью Telegram
    assert st == 200 and r["ok"] and rows(srv[2]) == 1
    st, r2 = pub_post(srv, "api/records", dict(body, client_id=str(uuid.uuid4())))   # повтор: дубля нет
    assert st == 200 and r2["duplicate_request"] and rows(srv[2]) == 1


def test_voice_reject_keeps_arm_so_user_can_retry(srv):
    pub_post(srv, "api/voice/arm", {"direction": "in"})
    st, b = ireq(srv, "POST", "/internal/voice-text", {"user_id": OWNER, "text": "несколько тысяч лир", "message_id": 6})
    assert st == 200 and b["ok"] is False and "Не уверен" in b["reply"] and "card_id" not in b
    assert ireq(srv, "GET", "/internal/armed?user=%d" % OWNER)[1]["armed"] == "in"


def test_voice_without_arm_takes_default_direction(srv):
    st, b = ireq(srv, "POST", "/internal/voice-text", {"user_id": OWNER, "text": "двадцать лир такси", "message_id": 5})
    assert st == 200 and b["ok"]


def test_outbox_http_roundtrip_and_long_poll_timeout(srv):
    t0 = time.monotonic()
    st, b = ireq(srv, "GET", "/internal/outbox?wait=1")
    assert st == 200 and b["items"] == [] and time.monotonic() - t0 >= 0.9
    c = db.connect(srv[2])
    cards.outbox_add(c, "send", OWNER, {"text": "привет"})
    c.close()
    st, b = ireq(srv, "GET", "/internal/outbox?wait=0")
    assert len(b["items"]) == 1
    st, a = ireq(srv, "POST", "/internal/outbox-ack", {"id": b["items"][0]["id"], "ok": True, "message_id": 321})
    assert a["status"] == "sent"
    assert ireq(srv, "GET", "/internal/outbox?wait=0")[1]["items"] == []
    assert ireq(srv, "POST", "/internal/outbox-ack", {"id": 1, "ok": True, "message_id": "x"})[0] == 422


def test_form_with_draft_id_writes_closes_card_and_blocks_double_write(srv):
    st, b = phrase(srv)
    cid = b["card_id"]
    ireq(srv, "POST", "/internal/card-message", {"user_id": OWNER, "card_id": cid, "message_id": 700})
    st, d = pub_get(srv, "api/draft?id=%d" % cid)
    assert st == 200 and d["amount"] == "20"
    body = dict(form(date=db.today_local(), amount="25"), draft_id=cid)
    st, r = pub_post(srv, "api/records", body)
    assert st == 200 and rows(srv[2], "via='webapp'") == 1
    st, b = ireq(srv, "POST", "/internal/card-action", {"user_id": OWNER, "action": "no", "card_id": cid})
    assert "закрыта" in b["answer"] and rows(srv[2]) == 1             # отмена закрытой карточки запись не трогает
    st, r = pub_post(srv, "api/records", dict(body, amount="30", client_id=str(uuid.uuid4())))
    assert st == 409 and r["error"] == "idem_conflict" and rows(srv[2]) == 1       # другое значение: не дубль, а отказ
    assert pub_get(srv, "api/draft?id=%d" % cid)[0] == 409
    items = ireq(srv, "GET", "/internal/outbox?wait=0")[1]["items"]
    assert items and items[0]["kind"] == "edit" and items[0]["payload"]["message_id"] == "700"
    assert "Записано №" in items[0]["payload"]["text"]


def test_inbox_and_draft_cancel_over_http(srv):
    a = phrase(srv, mid=1)[1]["card_id"]
    b = phrase(srv, "получил 300 usdt", mid=2)[1]["card_id"]
    st, out = pub_get(srv, "api/inbox")
    assert st == 200 and [d["id"] for d in out["drafts"]] == [b, a] and out["test"] is True
    assert pub_get(srv, "api/inbox", init=make_init_data(PRIV, user_id=STRANGER))[0] == 403
    st, out = pub_post(srv, "api/draft/cancel", {"id": a})
    assert st == 200 and rows(srv[2]) == 0
    assert pub_post(srv, "api/draft/cancel", {"id": a})[0] == 409
    assert pub_post(srv, "api/draft/cancel", {"id": "x"})[0] == 422
    assert pub_post(srv, "api/draft/cancel", {"id": b}, init=make_init_data(PRIV, user_id=STRANGER))[0] == 403
    assert [d["id"] for d in pub_get(srv, "api/inbox")[1]["drafts"]] == [b]


def test_draft_endpoint_rejects_foreign_card(srv):
    st, b = phrase(srv, "потратил 20 лир", mid=11)
    other = make_init_data(PRIV, user_id=STRANGER)
    assert req(srv[0], "GET", "/%s/api/draft?id=%d" % (TOKEN, b["card_id"]), init=other)[0] == 403


def test_public_arm_needs_signature(srv):
    assert req(srv[0], "POST", "/%s/api/voice/arm" % TOKEN, body={"direction": "out"})[0] == 401


def test_repeated_delivery_race_gives_one_card(srv):
    results = []

    def go():
        results.append(phrase(srv, mid=77))
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(st == 200 for st, _ in results)
    assert len({b["card_id"] for _, b in results}) == 1 and sum(not b["duplicate"] for _, b in results) == 1
    c = db.connect(srv[2], readonly=True)
    assert c.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
    c.close()


def test_duplicate_over_http_resends_card_when_chat_message_missing(srv):
    first = phrase(srv, mid=88)[1]
    again = phrase(srv, mid=88)[1]
    assert again["duplicate"] and again["text"] == first["text"]                 # сообщение в чате не отмечено: шлём снова
    ireq(srv, "POST", "/internal/card-message", {"user_id": OWNER, "card_id": first["card_id"], "message_id": 900})
    assert "text" not in phrase(srv, mid=88)[1]


def test_internal_log_has_no_text(srv, capfd):
    phrase(srv, "потратил 777 лир на секретное", mid=12)
    out = capfd.readouterr().err
    assert "секретн" not in out and "777" not in out
