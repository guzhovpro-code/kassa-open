"""Процесс бота (bot.py) против настоящего бэкенда на временной базе; Telegram подменён записывающим клиентом.
Расшифровщик голоса подменён: faster-whisper в тестах не нужен."""
import sqlite3
import threading

import pytest

from helpers import BOT_ID, OWNER, STRANGER
from kassa_api import bot as kbot, db
from kassa_api.server import Config, make_servers
from test_api import INTERNAL, PUB, TOKEN

URL = "https://kassa.example.org/" + TOKEN + "/"


class FakeTG:
    def __init__(self):
        self.calls, self.files, self.fail = [], {"f1": b"OggS-voice"}, set()
        self._mid = 100

    def call(self, method, payload=None, timeout=20):
        self.calls.append((method, payload or {}))
        if method in self.fail:
            raise kbot.TelegramError("%s: boom" % method)
        if method == "sendMessage":
            self._mid += 1
            return {"message_id": self._mid}
        if method == "getFile":
            return {"file_path": "voice/%s.oga" % payload["file_id"]}
        return True

    def download(self, file_path, limit=0):
        return self.files[file_path.split("/")[-1].split(".")[0]]

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


@pytest.fixture()
def world(tmp_path):
    dbp = str(tmp_path / "kassa-test.db")
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), INTERNAL, PUB, dbp, public_base="https://kassa.example.org")
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0))
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    tg = FakeTG()
    heard = []

    def transcribe(path):
        heard.append(open(path, "rb").read())
        return "кофе 150 лир"

    backend = kbot.Backend("http://127.0.0.1:%d" % internal.server_address[1], INTERNAL)
    b = kbot.Bot(cfg, tg, backend, transcribe, voice_enabled=True)
    yield b, tg, dbp, heard
    for s in (pub, internal):
        s.shutdown()
        s.server_close()


def msg(text=None, uid=OWNER, mid=1, voice=None, chat_type="private"):
    m = {"message_id": mid, "from": {"id": uid, "is_bot": False}, "chat": {"id": uid, "type": chat_type}}
    if text is not None:
        m["text"] = text
    if voice is not None:
        m["voice"] = voice
    return {"update_id": mid, "message": m}


def q(dbp, sql):
    c = sqlite3.connect(dbp)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_start_sends_the_kassa_button_and_sets_the_menu(world):
    b, tg, _, _ = world
    b.handle_update(msg("/start"))
    start = tg.sent()[0]
    assert start["reply_markup"]["inline_keyboard"][0][0] == {"text": "Касса", "web_app": {"url": URL}}
    menu = tg.sent("setChatMenuButton")[0]
    assert menu["menu_button"]["type"] == "web_app" and menu["menu_button"]["web_app"]["url"] == URL


def test_text_makes_a_card_with_buttons_and_remembers_its_message(world):
    b, tg, dbp, _ = world
    b.handle_update(msg("кофе 150 лир", mid=7))
    card = tg.sent()[0]
    assert "150" in card["text"] and card["reply_parameters"]["message_id"] == 7
    kinds = [("web_app" in btn) for row in card["reply_markup"]["inline_keyboard"] for btn in row]
    assert any(kinds) and not all(kinds)                       # есть и кнопки формы, и «Отмена»
    assert q(dbp, "SELECT status, message_id FROM cards") == [("pending", "101")]
    assert q(dbp, "SELECT COUNT(*) FROM transactions") == [(0,)]          # карточка денег не пишет


def test_stranger_gets_silence_and_nothing_is_created(world):
    b, tg, dbp, _ = world
    b.handle_update(msg("кофе 150 лир", uid=STRANGER))
    b.handle_update(msg("/start", uid=STRANGER))
    b.handle_update(msg("кофе 150 лир", chat_type="group"))
    assert tg.calls == [] and q(dbp, "SELECT COUNT(*) FROM cards") == [(0,)]


def test_text_without_money_gets_a_polite_refusal(world):
    b, tg, dbp, _ = world
    b.handle_update(msg("привет, как дела"))
    assert len(tg.sent()) == 1
    assert "reply_markup" not in tg.sent()[0] and q(dbp, "SELECT COUNT(*) FROM cards") == [(0,)]


def test_repeated_delivery_of_the_same_message_gives_one_card(world):
    b, tg, dbp, _ = world
    upd = msg("кофе 150 лир", mid=9)
    b.handle_update(upd)
    b.handle_update(upd)
    assert q(dbp, "SELECT COUNT(*) FROM cards") == [(1,)] and len(tg.sent()) == 1


def test_cancel_button_closes_the_card_and_edits_the_message(world):
    b, tg, dbp, _ = world
    b.handle_update(msg("кофе 150 лир"))
    cb = [btn["callback_data"] for row in tg.sent()[0]["reply_markup"]["inline_keyboard"] for btn in row
          if btn.get("callback_data", "").startswith("kassa:no")][0]
    b.handle_update({"callback_query": {"id": "c1", "from": {"id": OWNER}, "data": cb,
                                        "message": {"message_id": 101, "chat": {"id": OWNER}}}})
    assert q(dbp, "SELECT status FROM cards") == [("cancelled",)]
    assert "ничего не записано" in tg.sent("editMessageText")[0]["text"] and tg.sent("answerCallbackQuery")


def test_stale_and_foreign_buttons(world):
    b, tg, dbp, _ = world
    b.handle_update({"callback_query": {"id": "c1", "from": {"id": OWNER}, "data": "kassa:ok:5", "message": {}}})
    assert "устарела" in tg.sent("answerCallbackQuery")[-1]["text"]
    b.handle_update({"callback_query": {"id": "c2", "from": {"id": STRANGER}, "data": "kassa:no:5", "message": {}}})
    assert tg.sent("answerCallbackQuery")[-1]["text"] == "Нет доступа"
    b.handle_update({"callback_query": {"id": "c3", "from": {"id": OWNER}, "data": "garbage", "message": {}}})
    assert "text" not in tg.sent("answerCallbackQuery")[-1]


def test_day_mark_text_and_button(world):
    b, tg, dbp, _ = world
    b.handle_update(msg("только карта"))
    assert tg.sent()[-1]["text"].startswith("Принято") and q(dbp, "SELECT kind FROM daily_marks") == [("cardonly",)]
    b.handle_update(msg("карта 500 лир", mid=2))                   # денежная фраза, не отметка
    assert q(dbp, "SELECT COUNT(*) FROM cards") == [(1,)]
    day = db.today_local()
    b.handle_update({"callback_query": {"id": "d", "from": {"id": OWNER}, "data": "kassa:day:nospend:" + day,
                                        "message": {"message_id": 5, "chat": {"id": OWNER}}}})
    assert sorted(r[0] for r in q(dbp, "SELECT kind FROM daily_marks")) == ["cardonly", "nospend"]
    assert tg.sent("editMessageText")[-1]["text"].startswith("Принято")


@pytest.mark.parametrize("text", ["только карта", "Сегодня ничего не потратил", "без трат", "налом 0"])
def test_day_mark_phrases(text):
    assert kbot.day_mark_kind(text) in ("cardonly", "nospend")


@pytest.mark.parametrize("text", ["карта 500 лир", "кофе 150", "картой 300", "привет"])
def test_not_day_mark_phrases(text):
    assert kbot.day_mark_kind(text) is None


def test_voice_is_transcribed_and_becomes_a_card(world):
    b, tg, dbp, heard = world
    b.handle_update(msg(voice={"file_id": "f1", "duration": 4}, mid=3))
    assert heard == [b"OggS-voice"]
    assert "150" in tg.sent()[0]["text"]
    assert q(dbp, "SELECT source, heard FROM cards") == [("voice", "кофе 150 лир")]


def test_voice_disabled_answers_and_does_not_download(world):
    b, tg, dbp, heard = world
    b.voice_enabled = False
    b.handle_update(msg(voice={"file_id": "f1", "duration": 4}))
    assert tg.sent()[0]["text"] == kbot.VOICE_OFF_TEXT and heard == [] and not tg.sent("getFile")


def test_long_voice_is_refused(world):
    b, tg, _, heard = world
    b.handle_update(msg(voice={"file_id": "f1", "duration": kbot.MAX_VOICE_SECONDS + 1}))
    assert tg.sent()[0]["text"] == kbot.VOICE_LONG_TEXT and heard == []


def test_failed_transcription_writes_nothing(world):
    b, tg, dbp, _ = world

    def boom(path):
        raise RuntimeError("model missing")
    b.transcribe = boom
    b.handle_update(msg(voice={"file_id": "f1", "duration": 4}))
    assert tg.sent()[0]["text"] == kbot.STT_FAIL_TEXT and q(dbp, "SELECT COUNT(*) FROM cards") == [(0,)]
    b.transcribe = lambda path: "   "
    b.handle_update(msg(voice={"file_id": "f1", "duration": 4}, mid=4))
    assert tg.sent()[-1]["text"] == kbot.STT_FAIL_TEXT


def test_backend_down_is_a_message_not_a_crash(world):
    b, tg, _, _ = world
    b.backend = kbot.Backend("http://127.0.0.1:9", INTERNAL)
    b.handle_update(msg("кофе 150 лир"))
    assert tg.sent()[0]["text"] == kbot.DOWN_TEXT


def test_outbox_sends_edits_and_acks(world):
    b, tg, dbp, _ = world
    c = db.connect(dbp)
    from kassa_api import cards
    cards.outbox_add(c, "send", OWNER, {"text": "Напоминание", "buttons": [[{"text": "Открыть кассу", "web_app": URL}]]})
    cards.outbox_add(c, "edit", OWNER, {"message_id": 55, "text": "Записано", "buttons": []})
    assert b.outbox_once(wait=0) == 2
    assert tg.sent()[0]["text"] == "Напоминание" and tg.sent("editMessageText")[0]["message_id"] == 55
    assert q(dbp, "SELECT status FROM outbox ORDER BY id") == [("sent",), ("sent",)]
    assert b.outbox_once(wait=0) == 0


def test_outbox_failure_is_not_acked_as_sent(world):
    b, tg, dbp, _ = world
    from kassa_api import cards
    cards.outbox_add(db.connect(dbp), "send", OWNER, {"text": "x", "buttons": []})
    tg.fail.add("sendMessage")
    b.outbox_once(wait=0)
    assert q(dbp, "SELECT status FROM outbox") == [("pending",)]


def test_poll_once_advances_the_offset(world):
    b, tg, _, _ = world
    tg.call = lambda method, payload=None, timeout=20: (
        [msg("/start", mid=41), msg("/start", mid=42)] if method == "getUpdates" else FakeTG.call(tg, method, payload))
    assert b.poll_once(None) == 43


def test_markup_conversion():
    out = kbot.markup([[{"text": "A", "cb": "kassa:no:1"}, {"text": "B", "web_app": URL}]])
    assert out == {"inline_keyboard": [[{"text": "A", "callback_data": "kassa:no:1"},
                                        {"text": "B", "web_app": {"url": URL}}]]}
    assert kbot.markup([]) is None
