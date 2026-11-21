"""Telegram-бот кассы: отдельный процесс (long polling), тот же образ, другая команда: python -m kassa_api.bot

Бот только транспорт. Он принимает сообщения допущенных пользователей (KASSA_ALLOWED_IDS; остальным - молчание) и
отдаёт их бэкенду по внутреннему порту (/internal/*, Bearer KASSA_INTERNAL_TOKEN). Разбор денег, карточки, лимиты и
запись делает бэкенд (moneytext.py, cards.py, records.py); здесь их нет.

  /start                    приветствие и кнопка «Касса» (web_app на KASSA_PUBLIC_URL), кнопка меню у поля ввода
  «кофе 150 лир»            карточка с кнопками «Верно / Исправить / Отмена»
  голосовое сообщение       расшифровка локальным faster-whisper (KASSA_VOICE_ENABLED=1, модель KASSA_WHISPER_MODEL),
                            дальше как текст
  «Только карта», «не тратил»   отметка дня (денег не создаёт)
  очередь исходящих         напоминание и правки карточек после записи в форме приходят из бэкенда (outbox)

Только стандартная библиотека; faster-whisper импортируется лениво при первом голосовом сообщении.
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict

from .server import Config

POLL_TIMEOUT = 30                 # секунд long polling у Telegram
BACKEND_TIMEOUT = 8
MAX_VOICE_SECONDS = 180           # длиннее не расшифровываем: это не одна трата
MAX_VOICE_BYTES = 20 * 1024 * 1024
MENU_TEXT = "Касса"

DOWN_TEXT = "Касса недоступна, ничего не записано. Попробуйте ещё раз чуть позже."
REJECTED_TEXT = "Касса не приняла сообщение, ничего не записано."
STT_FAIL_TEXT = "Не расшифровалось, ничего не записано. Попробуйте ещё раз или напишите текстом."
VOICE_OFF_TEXT = "Голосовые сообщения выключены. Напишите текстом, например: «кофе 150 лир»."
VOICE_LONG_TEXT = "Голосовое слишком длинное, ничего не записано. Запишите одну трату короче."
START_TEXT = ("Касса. Пишите траты и поступления как в чате, например: «кофе 150 лир», «получил 100 долларов на карту», "
              "или присылайте голосовое. Я пришлю карточку: «Верно» запишет, «Отмена» ничего не запишет. "
              "Кнопка «Касса» открывает форму, отчёты и список записей.")

CB_RE = re.compile(r"^kassa:([a-z]+):([0-9]{1,12})$")
DAY_CB_RE = re.compile(r"^kassa:day:(cardonly|nospend):([0-9]{4}-[0-9]{2}-[0-9]{2})$")
LIVE_ACTIONS = ("no", "notmoney")

# «Только карта» / «не тратил» текстом: совпадение только со всем сообщением, «карта 500 лир» остаётся денежным.
_CASH = r"(?:нал|налом|налич\w*)"
_ZERO = r"(?:ноль|0)"
CARD_ONLY_RE = re.compile(
    r"^(?:сегодня\s+)?(?:"
    r"только\s+(?:по\s+)?карт(?:а|ой|е|у|ы)|"
    r"%s\s+%s|%s\s+%s"
    r")(?:\s+сегодня)?[.!\s]*$" % (_CASH, _ZERO, _ZERO, _CASH), re.IGNORECASE)
NO_SPEND_RE = re.compile(
    r"^(?:сегодня\s+)?(?:"
    r"(?:я\s+)?ничего\s+не\s+(?:по)?трат(?:ил|ила|или)|"
    r"нет\s+трат\w*|без\s+трат\w*|0\s+трат\w*|трат\s+не\s+было"
    r")(?:\s+сегодня)?[.!\s]*$", re.IGNORECASE)


def day_mark_kind(text) -> str | None:
    t = " ".join(str(text or "").split())
    if CARD_ONLY_RE.match(t):
        return "cardonly"
    if NO_SPEND_RE.match(t):
        return "nospend"
    return None


def log(msg: str) -> None:
    sys.stderr.write("%s %s\n" % (time.strftime("%d/%b/%Y %H:%M:%S"), msg))


class TelegramError(Exception):
    pass


class Telegram:
    """Минимальный клиент Bot API на urllib. Токен в сообщения об ошибках не попадает."""

    def __init__(self, token: str, base: str = "https://api.telegram.org"):
        self._token, self._base = token, base

    def call(self, method: str, payload: dict | None = None, timeout: float = 20):
        req = urllib.request.Request("%s/bot%s/%s" % (self._base, self._token, method),
                                     data=json.dumps(payload or {}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                body = json.load(exc)
            except Exception:       # noqa: BLE001
                body = {"ok": False, "description": "HTTP %s" % exc.code}
        except Exception as exc:    # noqa: BLE001
            raise TelegramError("%s: %s" % (method, type(exc).__name__)) from None
        if not body.get("ok"):
            raise TelegramError("%s: %s" % (method, body.get("description")))
        return body["result"]

    def download(self, file_path: str, limit: int = MAX_VOICE_BYTES) -> bytes:
        url = "%s/file/bot%s/%s" % (self._base, self._token, file_path)
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                data = resp.read(limit + 1)
        except Exception as exc:    # noqa: BLE001
            raise TelegramError("download: %s" % type(exc).__name__) from None
        if len(data) > limit:
            raise TelegramError("download: too large")
        return data


class BackendDown(Exception):
    pass


class Backend:
    """Внутренний порт kassa-api. call -> (status, dict)."""

    def __init__(self, base: str, token: str):
        self._base, self._token = base.rstrip("/"), token

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = BACKEND_TIMEOUT):
        req = urllib.request.Request(self._base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + self._token,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.load(exc)
            except Exception:       # noqa: BLE001
                return exc.code, {}
        except Exception as exc:    # noqa: BLE001
            raise BackendDown(type(exc).__name__) from None


class WhisperTranscriber:
    """Расшифровка локальным faster-whisper. Модель грузится при первом вызове (скачивается в download_root)."""

    def __init__(self, model_name: str = "small", download_root: str = "/data/whisper"):
        self.model_name, self.download_root = model_name, download_root
        self._model = None
        self._lock = threading.Lock()

    def __call__(self, path: str) -> str:
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel           # ленивый импорт: тесты идут без него
                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                           download_root=self.download_root)
            segments, _info = self._model.transcribe(path, vad_filter=True)       # язык определяется сам
            return " ".join(seg.text.strip() for seg in segments).strip()


def markup(buttons):
    """Кнопки бэкенда ({"text","cb"} или {"text","web_app"}) -> inline_keyboard Telegram."""
    if not buttons:
        return None
    rows = []
    for row in buttons:
        out = []
        for b in row:
            if "web_app" in b:
                out.append({"text": b["text"], "web_app": {"url": b["web_app"]}})
            else:
                out.append({"text": b["text"], "callback_data": b["cb"]})
        rows.append(out)
    return {"inline_keyboard": rows}


class Bot:
    def __init__(self, config: Config, tg, backend, transcribe=None, voice_enabled: bool = True):
        self.config, self.tg, self.backend = config, tg, backend
        self.transcribe, self.voice_enabled = transcribe, voice_enabled
        self._sent_outbox: OrderedDict = OrderedDict()      # id очереди -> message_id (отправлено, ack не дошёл)

    # --- отправка ---------------------------------------------------------------------------------------------
    def send(self, chat_id, text, buttons=None, reply_to=None):
        payload = {"chat_id": int(chat_id), "text": text}
        if buttons:
            payload["reply_markup"] = markup(buttons)
        if reply_to:
            payload["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        return self.tg.call("sendMessage", payload)

    def edit(self, chat_id, message_id, text, buttons=None):
        payload = {"chat_id": int(chat_id), "message_id": int(message_id), "text": text,
                   "reply_markup": markup(buttons) or {"inline_keyboard": []}}
        try:
            self.tg.call("editMessageText", payload)
        except TelegramError as exc:
            if "not modified" not in str(exc).lower():
                raise

    def _say(self, chat_id, text, reply_to=None):
        """Ответ «по возможности»: сбой отправки только в журнал."""
        try:
            self.send(chat_id, text, reply_to=reply_to)
        except TelegramError as exc:
            log("kassa-bot: ответ не доставлен: %s" % exc)

    # --- кнопка меню ------------------------------------------------------------------------------------------
    def set_menu(self, chat_id) -> bool:
        try:
            self.tg.call("setChatMenuButton", {"chat_id": int(chat_id), "menu_button": {
                "type": "web_app", "text": MENU_TEXT, "web_app": {"url": self.config.webapp_url}}})
            return True
        except TelegramError as exc:
            log("kassa-bot: кнопка меню не поставлена: %s" % exc)
            return False

    def set_menu_for_all(self) -> None:
        for uid in sorted(self.config.allowed_ids):
            self.set_menu(uid)

    # --- обновления -------------------------------------------------------------------------------------------
    def handle_update(self, update: dict) -> None:
        try:
            if "callback_query" in update:
                self._callback(update["callback_query"])
            elif "message" in update:
                self._message(update["message"])
        except Exception as exc:        # noqa: BLE001 - один плохой апдейт не должен ронять цикл
            log("kassa-bot: сбой обработки: %s" % type(exc).__name__)

    def _allowed(self, user) -> bool:
        return isinstance(user, dict) and not user.get("is_bot") and user.get("id") in self.config.allowed_ids

    def _message(self, msg: dict) -> None:
        user, chat = msg.get("from"), msg.get("chat") or {}
        if not self._allowed(user) or chat.get("type") != "private":
            return                                              # чужим - молчание
        uid, mid = str(user["id"]), msg.get("message_id")
        text = msg.get("text")
        if isinstance(text, str):
            if text.startswith("/start"):
                return self._on_start(chat["id"])
            if text.startswith("/"):
                return
            kind = day_mark_kind(text)
            if kind:
                return self._on_day_text(uid, chat["id"], mid, kind)
            return self._on_phrase(uid, chat["id"], mid, "/internal/phrase", text)
        if isinstance(msg.get("voice"), dict):
            return self._on_voice(uid, chat["id"], mid, msg["voice"])

    def _on_start(self, chat_id) -> None:
        self.send(chat_id, START_TEXT, [[{"text": MENU_TEXT, "web_app": self.config.webapp_url}]])
        self.set_menu(chat_id)

    def _on_day_text(self, uid, chat_id, mid, kind) -> None:
        try:
            status, res = self.backend.call("POST", "/internal/day-mark", {"user_id": uid, "kind": kind, "via": "text"})
        except BackendDown as exc:
            log("kassa-bot: бэкенд недоступен (отметка дня): %s" % exc)
            return self._say(chat_id, DOWN_TEXT, mid)
        if status == 200 and res.get("ok") and res.get("text"):
            self._say(chat_id, res["text"], mid)
        else:
            self._say(chat_id, REJECTED_TEXT, mid)

    def _on_phrase(self, uid, chat_id, mid, path, text) -> None:
        try:
            status, res = self.backend.call("POST", path, {"user_id": uid, "chat_id": chat_id, "message_id": mid,
                                                            "text": text})
        except BackendDown as exc:
            log("kassa-bot: бэкенд недоступен (фраза): %s" % exc)
            return self._say(chat_id, DOWN_TEXT, mid)
        if status != 200:
            return self._say(chat_id, REJECTED_TEXT, mid)
        if not res.get("ok"):
            return self._say(chat_id, res.get("reply") or "Не получилось, ничего не записано.", mid)
        if not res.get("text"):                                 # повторная доставка без потери карточки: молчим
            return
        sent = self.send(chat_id, res["text"], res.get("buttons"), reply_to=mid)
        try:
            self.backend.call("POST", "/internal/card-message",
                              {"user_id": uid, "card_id": res["card_id"], "message_id": sent["message_id"]})
        except BackendDown:
            log("kassa-bot: не удалось сохранить id сообщения карточки")

    def _on_voice(self, uid, chat_id, mid, voice) -> None:
        if not self.voice_enabled or self.transcribe is None:
            return self._say(chat_id, VOICE_OFF_TEXT, mid)
        if int(voice.get("duration") or 0) > MAX_VOICE_SECONDS:
            return self._say(chat_id, VOICE_LONG_TEXT, mid)
        text = ""
        path = None
        try:
            info = self.tg.call("getFile", {"file_id": voice["file_id"]})
            data = self.tg.download(info["file_path"])
            fd, path = tempfile.mkstemp(suffix=".ogg")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            text = (self.transcribe(path) or "").strip()
        except Exception as exc:        # noqa: BLE001
            log("kassa-bot: расшифровка не удалась: %s" % type(exc).__name__)
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
        if not text:
            return self._say(chat_id, STT_FAIL_TEXT, mid)
        self._on_phrase(uid, chat_id, mid, "/internal/voice-text", text)

    def _callback(self, cq: dict) -> None:
        cqid, data = cq.get("id"), cq.get("data") or ""
        if not self._allowed(cq.get("from")):
            return self._answer(cqid, "Нет доступа")
        uid = str(cq["from"]["id"])
        msg = cq.get("message") or {}
        day = DAY_CB_RE.match(data)
        if day:
            kind, when = day.groups()
            route, body = "/internal/day-mark", {"user_id": uid, "kind": kind, "day": when, "via": "card"}
        else:
            m = CB_RE.match(data)
            if not m:
                return self._answer(cqid, None)
            if m.group(1) not in LIVE_ACTIONS:
                return self._answer(cqid, "Эта кнопка устарела. Откройте Кассу.", alert=True)
            route, body = "/internal/card-action", {"user_id": uid, "action": m.group(1), "card_id": int(m.group(2))}
        try:
            status, res = self.backend.call("POST", route, body)
        except BackendDown as exc:
            log("kassa-bot: бэкенд недоступен (кнопка): %s" % exc)
            return self._answer(cqid, "Касса недоступна, ничего не записано", alert=True)
        if status != 200 or not res.get("ok"):
            return self._answer(cqid, "Не получилось, ничего не записано", alert=True)
        e = res.get("edit")
        self._answer(cqid, res.get("answer") or None, alert=bool(day and not e))
        if e and msg.get("message_id") and (msg.get("chat") or {}).get("id"):
            try:
                self.edit(msg["chat"]["id"], msg["message_id"], e["text"], e.get("buttons"))
            except TelegramError as exc:
                log("kassa-bot: правка сообщения не удалась: %s" % exc)

    def _answer(self, cqid, text, alert: bool = False) -> None:
        payload = {"callback_query_id": cqid}
        if text:
            payload["text"] = text
            payload["show_alert"] = alert
        try:
            self.tg.call("answerCallbackQuery", payload)
        except TelegramError as exc:
            log("kassa-bot: answerCallbackQuery: %s" % exc)

    # --- очередь исходящих ------------------------------------------------------------------------------------
    def outbox_once(self, wait: int = 25) -> int:
        """Забирает сообщения очереди бэкенда (напоминание, правка карточки после записи в форме) и отправляет.
        Отправленное, но не подтверждённое (обрыв перед ack) повторно не шлётся: только ack."""
        status, res = self.backend.call("GET", "/internal/outbox?wait=%d" % wait, timeout=wait + 10)
        if status != 200:
            raise BackendDown("outbox status %s" % status)
        items = res.get("items", [])
        for item in items:
            ok, message_id = False, None
            try:
                p = item["payload"]
                if item["id"] in self._sent_outbox:
                    ok, message_id = True, self._sent_outbox[item["id"]]
                elif item["kind"] == "edit":
                    self.edit(item["chat_id"], p["message_id"], p["text"], p.get("buttons"))
                    ok = True
                else:
                    sent = self.send(item["chat_id"], p["text"], p.get("buttons"))
                    ok, message_id = True, sent.get("message_id")
                    self._sent_outbox[item["id"]] = message_id
                    while len(self._sent_outbox) > 500:
                        self._sent_outbox.popitem(last=False)
            except TelegramError as exc:
                log("kassa-bot: outbox отправка не удалась: %s" % exc)
            body = {"id": item["id"], "ok": ok}
            if isinstance(message_id, int) and not isinstance(message_id, bool):
                body["message_id"] = message_id
            self.backend.call("POST", "/internal/outbox-ack", body)
        return len(items)

    def outbox_loop(self, sleep=time.sleep) -> None:
        while True:
            try:
                self.outbox_once()
            except BackendDown:
                sleep(10)
            except Exception as exc:    # noqa: BLE001
                log("kassa-bot: сбой цикла outbox: %s" % type(exc).__name__)
                sleep(10)

    # --- главный цикл -----------------------------------------------------------------------------------------
    def poll_once(self, offset: int | None) -> int | None:
        payload = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        updates = self.tg.call("getUpdates", payload, timeout=POLL_TIMEOUT + 10)
        for upd in updates:
            self.handle_update(upd)
            offset = upd["update_id"] + 1
        return offset

    def run(self) -> None:
        self.set_menu_for_all()
        threading.Thread(target=self.outbox_loop, name="kassa-outbox", daemon=True).start()
        offset = None
        while True:
            try:
                offset = self.poll_once(offset)
            except TelegramError as exc:
                log("kassa-bot: getUpdates: %s" % exc)
                time.sleep(5)


def main() -> None:
    config = Config.from_env()
    token = os.environ.get("KASSA_BOT_TOKEN", "")
    voice_enabled = (os.environ.get("KASSA_VOICE_ENABLED") or "1") == "1"
    transcriber = WhisperTranscriber(os.environ.get("KASSA_WHISPER_MODEL") or "small",
                                     os.environ.get("KASSA_WHISPER_DIR") or "/data/whisper")
    backend = Backend(os.environ.get("KASSA_INTERNAL_URL") or "http://api:8081", config.internal_token)
    log("kassa-bot started")
    Bot(config, Telegram(token), backend, transcriber, voice_enabled).run()


if __name__ == "__main__":
    main()
