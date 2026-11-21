"""kassa-api: бэкенд мини-приложения «Касса» (форма записи, отчёты, правка и удаление, обмен валют, курсы USD).

Два порта:
  8080 публичный (за HTTPS-прокси или туннелем): /api/* с заголовком Authorization: tma <initData>
        (подпись Ed25519 + список допущенных id). Если задан KASSA_PATH_TOKEN, адрес живёт под префиксом
        /<токен>/..., остальные пути - 404.
  8081 внутренний (только сеть docker-compose): Bearer-токен KASSA_INTERNAL_TOKEN, ходит только процесс бота
        (bot.py). Токен МОЖЕТ: создать черновик из текста или расшифровки (лимит и срок жизни), отменить черновик
        («Отмена», «Это не деньги»), поставить отметку дня «Только карта» / «Не тратил» (денег не создаёт), забрать и
        подтвердить очередь исходящих, прочитать итоги поступлений (GET /internal/income, только чтение). НЕ МОЖЕТ:
        записать деньги, отменить или вернуть запись, прочитать карточку. Запись и удаление - только /api/* с initData.
В журнал не пишутся суммы, заметки и id пользователей: только исходы («record ok», «record deny: <код>»).
"""
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import cards, categorize, currencies, db, exchanges, rates, records, reports, tgauth

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MAX_BODY = 8192
DEFAULT_DB_PATH = "/data/kassa-test.db"
PATH_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,}$")      # необязателен: пустой токен - адрес без префикса
RECORD_ACTION_RE = re.compile(r"api/records/([0-9]{1,12})/(cancel|restore|update)")
EXCHANGE_ACTION_RE = re.compile(r"api/exchanges/([0-9]{1,12})/(cancel|restore)")
EXCHANGE_ITEM_RE = re.compile(r"api/exchanges/([0-9]{1,12})")

CSP = ("default-src 'none'; script-src 'self' https://telegram.org; style-src 'self'; "
       "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
       "frame-ancestors https://web.telegram.org https://*.telegram.org")

STATIC_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "app.css": ("app.css", "text/css; charset=utf-8"),
}


@dataclass(frozen=True)
class Config:
    path_token: str
    bot_id: str
    allowed_ids: frozenset
    internal_token: str
    public_key_hex: str = tgauth.TELEGRAM_PUBLIC_KEY_PROD_HEX
    db_path: str = DEFAULT_DB_PATH
    public_base: str = "https://kassa.example.com"
    reminder_enabled: bool = False        # вечернее напоминание (время - KASSA_REMINDER_TIME), по умолчанию выключено

    @property
    def webapp_url(self) -> str:
        if not self.path_token:
            return self.public_base.rstrip("/") + "/"
        return "%s/%s/" % (self.public_base.rstrip("/"), self.path_token)

    @property
    def owner_chat_id(self) -> str:
        return str(min(self.allowed_ids))

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        path_token = env.get("KASSA_PATH_TOKEN", "")
        if path_token and not PATH_TOKEN_RE.match(path_token):
            raise SystemExit("KASSA_PATH_TOKEN: пусто или не меньше 32 символов [A-Za-z0-9_-]")
        bot_id = env.get("KASSA_BOT_ID", "") or env.get("KASSA_BOT_TOKEN", "").split(":", 1)[0]
        if not bot_id.isdigit():
            raise SystemExit("KASSA_BOT_TOKEN: нужен токен от @BotFather (число:строка)")
        raw_ids = [x.strip() for x in env.get("KASSA_ALLOWED_IDS", "").split(",") if x.strip()]
        if not raw_ids or not all(x.isdigit() for x in raw_ids):
            raise SystemExit("KASSA_ALLOWED_IDS: список числовых id Telegram через запятую")
        internal = env.get("KASSA_INTERNAL_TOKEN", "")
        if len(internal) < 32:
            raise SystemExit("KASSA_INTERNAL_TOKEN: не меньше 32 символов")
        public_base = env.get("KASSA_PUBLIC_URL", "").strip().rstrip("/")
        if not public_base.startswith("https://"):
            raise SystemExit("KASSA_PUBLIC_URL: нужен адрес https://... (мини-приложение Telegram открывается только по HTTPS)")
        return cls(path_token, bot_id, frozenset(int(x) for x in raw_ids), internal,
                   db_path=env.get("KASSA_DB_PATH", DEFAULT_DB_PATH), public_base=public_base,
                   reminder_enabled=env.get("KASSA_REMINDER_ENABLED", "") == "1")


def _load_static() -> dict:
    files = {name: (Path(STATIC_DIR, fname).read_bytes(), ctype)
             for name, (fname, ctype) in STATIC_FILES.items()}
    # справочник валют (публичные данные ISO 4217): страница берёт его один раз, не вместе с каждым /api/meta
    files["currencies.json"] = (json.dumps(currencies.catalog(), ensure_ascii=False, separators=(",", ":")).encode(),
                                "application/json; charset=utf-8")
    return files


class _Base(BaseHTTPRequestHandler):
    timeout = 10                     # не держим зависшие соединения
    protocol_version = "HTTP/1.1"
    server_version = "kassa"
    sys_version = ""
    config: Config                   # задаётся в подклассе

    def _mask(self, text: str) -> str:
        return text.replace(self.config.path_token, "*") if self.config.path_token else text

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), self._mask(fmt % args)))

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _not_found(self):
        # тот же текст, что у Traefik для неизвестного адреса
        if self.command not in ("GET", "HEAD"):
            self.close_connection = True          # тело запроса не читали
        self._send(404, b"404 page not found\n", "text/plain; charset=utf-8")

    def _json(self, status: int, payload: dict):
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _read_json(self):
        """Читает тело запроса. При ошибке сам отвечает и возвращает None."""
        self.close_connection = True          # тело могло остаться непрочитанным
        if not (self.headers.get("Content-Type", "").split(";")[0].strip().lower() == "application/json"):
            self._json(415, {"ok": False, "error": "bad_content_type"})
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(411, {"ok": False, "error": "length_required"})
            return None
        if length < 0 or length > MAX_BODY:
            self._json(413, {"ok": False, "error": "too_large"})
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):      # RecursionError: глубоко вложенный JSON
            self._json(400, {"ok": False, "error": "bad_json"})
            return None
        self.close_connection = False
        return data

    # любой метод, кроме GET/HEAD, не раскрывает ничего
    def do_POST(self): self._not_found()
    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST


class PublicHandler(_Base):
    statics: dict = {}
    rate_store: rates.RateStore | None = None          # кэш курсов; сеть - только в фоновом RateWorker

    def _rest(self) -> str | None:
        """Часть пути после секретного префикса или None, если префикс не тот."""
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        prefix = "/" + self.config.path_token + "/" if self.config.path_token else "/"
        if not (len(path) >= len(prefix) and hmac.compare_digest(path[:len(prefix)].encode(), prefix.encode())):
            return None
        return path[len(prefix):]

    def do_GET(self):
        rest = self._rest()
        if rest is None:
            return self._not_found()
        if rest in self.statics:
            body, ctype = self.statics[rest]
            return self._send(200, body, ctype)
        if rest == "healthz":
            return self._send(200, b"ok\n", "text/plain; charset=utf-8")
        if rest == "api/whoami":
            return self._whoami()
        if rest == "api/meta":
            return self._meta()
        if rest == "api/report":
            return self._report()
        if rest == "api/recent":
            return self._recent()
        if rest == "api/draft":
            return self._draft()
        if rest == "api/inbox":
            return self._inbox()
        if rest == "api/exchanges":
            return self._exchanges()
        self._not_found()

    do_HEAD = do_GET

    def handle_one_request(self):
        """Страховка: любая непредвиденная ошибка в обработчике - ответ 500 и строка в журнале (только тип
        ошибки, без данных), а не обрыв соединения."""
        try:
            super().handle_one_request()
        except (ConnectionError, TimeoutError):
            raise
        except Exception as exc:        # noqa: BLE001
            self.log_message("unhandled error: %s", type(exc).__name__)
            self.close_connection = True
            try:
                self._json(500, {"ok": False, "error": "server_error"})
            except OSError:
                pass

    def do_POST(self):
        rest = self._rest()
        if rest == "api/records":
            return self._create_record()
        if rest == "api/categorize":
            return self._categorize()
        if rest == "api/voice/arm":
            return self._voice_arm()
        if rest == "api/draft/cancel":
            return self._draft_cancel()
        if rest == "api/exchanges":
            return self._create_exchange()
        m = RECORD_ACTION_RE.fullmatch(rest or "")
        if m:
            return self._change_record(int(m.group(1)), m.group(2))
        m = EXCHANGE_ACTION_RE.fullmatch(rest or "")
        if m:
            return self._change_exchange(int(m.group(1)), m.group(2))
        self._not_found()

    def do_PATCH(self):
        m = EXCHANGE_ITEM_RE.fullmatch(self._rest() or "")
        if m:
            return self._change_exchange(int(m.group(1)), "update")
        self._not_found()

    do_PUT = do_DELETE = do_OPTIONS = _Base.do_POST

    # --- авторизация ---------------------------------------------------------------------------------------
    def _authenticate(self, tag: str):
        """Возвращает проверенного пользователя или None (ответ 401/403 уже отправлен)."""
        header = self.headers.get("Authorization", "")
        if not header.startswith("tma "):
            self.log_message("%s deny: no_header", tag)
            self._json(401, {"valid": False, "allowed": False})
            return None
        try:
            info = tgauth.verify_init_data(header[4:], self.config.bot_id, self.config.public_key_hex)
        except tgauth.AuthError as exc:
            self.log_message("%s deny: %s", tag, exc)
            self._json(401, {"valid": False, "allowed": False})
            return None
        if not tgauth.is_allowed(info["user"], self.config.allowed_ids):
            self.log_message("%s deny: not_allowed", tag)
            self._json(403, {"valid": True, "allowed": False})
            return None
        return info["user"]

    def _whoami(self):
        if self._authenticate("whoami") is None:
            return
        self.log_message("whoami ok")
        self._json(200, {"valid": True, "allowed": True})

    # --- данные --------------------------------------------------------------------------------------------
    def _meta(self):
        if self._authenticate("meta") is None:
            return
        try:
            conn = db.connect(self.config.db_path)
            try:
                payload = records.meta(conn, db.is_test_path(self.config.db_path))
            finally:
                conn.close()
            payload["payers"] = []        # готовых меток плательщиков нет: страница даёт ввод своего текста
        except sqlite3.Error as exc:
            self.log_message("meta error: %s", type(exc).__name__)
            return self._json(500, {"ok": False, "error": "server_error"})
        self._json(200, payload)

    def _rates(self) -> dict:
        return self.rate_store.usable() if self.rate_store is not None else {}

    def _query(self) -> dict | None:
        """Параметры строки запроса: по одному значению на имя, не больше 5 полей. Мусор -> 400 и None."""
        qs = self.path.split("#", 1)[0].partition("?")[2]
        try:
            return {k: v[0] for k, v in parse_qs(qs, keep_blank_values=True, max_num_fields=5).items()}
        except ValueError:
            self._json(400, {"ok": False, "error": "bad_query"})
            return None

    def _db_call(self, tag: str, fn):
        """Выполняет fn(conn): RecordError -> его статус и код, ошибка базы -> 500; в журнал только исход."""
        try:
            conn = db.connect(self.config.db_path)
            try:
                return fn(conn)
            finally:
                conn.close()
        except records.RecordError as exc:
            self.log_message("%s deny: %s", tag, exc.code)
            self._json(exc.status, exc.payload())
        except cards.CardError as exc:
            self.log_message("%s deny: draft_%s", tag, exc.code)
            self._json(exc.status, {"ok": False, "error": exc.code})
        except sqlite3.Error as exc:
            self.log_message("%s error: %s", tag, type(exc).__name__)
            self._json(500, {"ok": False, "error": "server_error"})
        return None

    def _report(self):
        if self._authenticate("report") is None:
            return
        q = self._query()
        if q is None:
            return
        period = q.get("period", "day")
        key = q.get("month") if period == "month" else q.get("date")
        if period == "trend":
            result = self._db_call("report", lambda c: reports.trend(c, db.today_local()))
        else:
            if period == "range":
                key, to = q.get("from"), q.get("to")
            else:
                to = None
            result = self._db_call("report", lambda c: reports.report(c, period, key or None, db.today_local(), to or None))
        if result is not None:
            self._json(200, result)

    def _recent(self):
        if self._authenticate("recent") is None:
            return
        q = self._query()
        if q is None:
            return
        flt = q.get("filter", "")
        if flt not in ("", "no_method", "closed"):
            return self._json(422, {"ok": False, "error": "bad_filter"})
        result = self._db_call("recent", lambda c: reports.recent(c, no_method=(flt == "no_method"),
                                                                  closed=(flt == "closed")))
        if result is not None:
            self._json(200, result)

    def _change_record(self, rec_id: int, action: str):
        user = self._authenticate(action)
        if user is None:
            self.close_connection = True
            return
        data = self._read_json()
        if data is None:
            return

        def run(conn):
            if action == "cancel":
                res = records.cancel_record(conn, user["id"], rec_id)
            elif action == "restore":
                res = records.restore_record(conn, user["id"], rec_id)
            else:
                res = records.update_record(conn, user["id"], rec_id, data, fx_rates=self._rates())
            res["record"] = reports.get_item(conn, rec_id)
            return res

        result = self._db_call(action, run)
        if result is not None:
            self.log_message("%s ok", action)
            self._json(200, result)

    def _exchanges(self):
        """Список обменов месяца (подписанный запрос): ?month=YYYY-MM, по умолчанию текущий."""
        if self._authenticate("exchanges") is None:
            return
        q = self._query()
        if q is None:
            return
        result = self._db_call("exchanges", lambda c: exchanges.list_month(c, q.get("month") or None,
                                                                          db.today_local()))
        if result is not None:
            self._json(200, result)

    def _create_exchange(self):
        """Запись обмена: подпись Telegram, client_id для идемпотентности, draft_id для черновика из чата (запись,
        закрытие черновика и правка карточки - одна транзакция). Порядок проверок тот же, что у /api/records."""
        user = self._authenticate("exchange")
        if user is None:
            self.close_connection = True
            return
        data = self._read_json()
        if data is None:
            return
        draft_id = data.pop("draft_id", None) if isinstance(data, dict) else None
        if draft_id is not None and (not isinstance(draft_id, int) or isinstance(draft_id, bool)):
            return self._json(422, {"ok": False, "error": "bad_draft"})

        def run(conn):
            if draft_id is not None:
                return cards.confirm_exchange_draft(conn, user["id"], draft_id, data, self.config.webapp_url,
                                                    db.is_test_path(self.config.db_path), fx_rates=self._rates())
            return exchanges.create_exchange(conn, user["id"], data, fx_rates=self._rates())

        result = self._db_call("exchange", run)
        if result is not None:
            self.log_message("exchange %s", "repeat" if result["duplicate_request"] else "ok")
            self._json(200, result)

    def _change_exchange(self, ex_id: int, action: str):
        """cancel / restore (POST) и правка (PATCH) обмена; только подписанный запрос."""
        user = self._authenticate(action)
        if user is None:
            self.close_connection = True
            return
        data = self._read_json()
        if data is None:
            return

        def run(conn):
            if action == "cancel":
                res = exchanges.cancel_exchange(conn, user["id"], ex_id)
            elif action == "restore":
                res = exchanges.restore_exchange(conn, user["id"], ex_id)
            else:
                res = exchanges.update_exchange(conn, user["id"], ex_id, data, fx_rates=self._rates())
            res["exchange"] = exchanges.get_item(conn, ex_id)
            return res

        result = self._db_call("exchange_" + action, run)
        if result is not None:
            self.log_message("exchange_%s ok", action)
            self._json(200, result)

    def _categorize(self):
        """Подсказка категории для формы по тексту «на что» (тот же код, что и при записи). Текст не пишется
        ни в базу, ни в журнал; POST, чтобы он не попал в строку запроса."""
        user = self._authenticate("categorize")
        if user is None:
            self.close_connection = True
            return
        data = self._read_json()
        if data is None:
            return
        if not isinstance(data, dict) or data.get("direction") not in ("in", "out"):
            return self._json(422, {"ok": False, "error": "bad_direction"})
        text = data.get("text")
        if text is not None and (not isinstance(text, str) or len(text) > 400):
            return self._json(422, {"ok": False, "error": "bad_note"})
        self._json(200, {"ok": True, "category": categorize.categorize(text, data["direction"])})

    def _create_record(self):
        user = self._authenticate("record")
        if user is None:
            self.close_connection = True
            return
        data = self._read_json()
        if data is None:
            return
        draft_id = data.pop("draft_id", None) if isinstance(data, dict) else None
        if draft_id is not None and (not isinstance(draft_id, int) or isinstance(draft_id, bool)):
            return self._json(422, {"ok": False, "error": "bad_draft"})
        try:
            conn = db.connect(self.config.db_path)
            try:
                if draft_id is not None:          # форма открыта из карточки в чате: запись, закрытие черновика и
                    result = cards.confirm_draft(   # правка карточки - одна транзакция, ключ идемпотентности card:<id>
                        conn, user["id"], draft_id, data, self.config.webapp_url, db.is_test_path(self.config.db_path),
                        fx_rates=self._rates())
                else:
                    result = records.create_record(conn, user["id"], data, fx_rates=self._rates())
            finally:
                conn.close()
        except cards.CardError as exc:
            self.log_message("record deny: draft_%s", exc.code)
            return self._json(exc.status, {"ok": False, "error": exc.code})
        except records.RecordError as exc:
            self.log_message("record deny: %s", exc.code)
            return self._json(exc.status, exc.payload())
        except sqlite3.Error as exc:
            self.log_message("record error: %s", type(exc).__name__)
            return self._json(500, {"ok": False, "error": "server_error"})
        self.log_message("record %s", "repeat" if result["duplicate_request"] else "ok")
        self._json(200, result)

    def _voice_arm(self):
        """«🎙 Надиктовать» в форме: ближайшее голосовое от этого пользователя в течение 10 минут пойдёт в кассу."""
        user = self._authenticate("arm")
        if user is None:
            self.close_connection = True
            return
        data = self._read_json()
        if data is None:
            return
        direction = data.get("direction") if isinstance(data, dict) else None
        try:
            conn = db.connect(self.config.db_path)
            try:
                until = cards.arm_voice(conn, user["id"], direction)
            finally:
                conn.close()
        except cards.CardError as exc:
            return self._json(exc.status, {"ok": False, "error": exc.code})
        except sqlite3.Error as exc:
            self.log_message("arm error: %s", type(exc).__name__)
            return self._json(500, {"ok": False, "error": "server_error"})
        self.log_message("arm ok")
        self._json(200, {"ok": True, "until": until})

    def _draft(self):
        user = self._authenticate("draft")
        if user is None:
            return
        q = self._query()
        if q is None:
            return
        try:
            draft_id = int(q.get("id", ""))
        except ValueError:
            return self._json(422, {"ok": False, "error": "bad_id"})
        try:
            conn = db.connect(self.config.db_path)
            try:
                result = cards.draft_for_form(conn, user["id"], draft_id)
            finally:
                conn.close()
        except cards.CardError as exc:
            self.log_message("draft deny: %s", exc.code)
            return self._json(exc.status, {"ok": False, "error": exc.code})
        except sqlite3.Error as exc:
            self.log_message("draft error: %s", type(exc).__name__)
            return self._json(500, {"ok": False, "error": "server_error"})
        self._json(200, result)


    def _inbox(self):
        """Вкладка «Входящие»: открытые черновики из чата (подписанный запрос)."""
        user = self._authenticate("inbox")
        if user is None:
            return
        test = db.is_test_path(self.config.db_path)
        result = self._db_call("inbox", lambda c: {"ok": True, "test": test, "drafts": cards.inbox(c, user["id"])})
        if result is not None:
            self._json(200, result)

    def _draft_cancel(self):
        user = self._authenticate("draft_cancel")
        if user is None:
            self.close_connection = True
            return
        data = self._read_json()
        if data is None:
            return
        draft_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(draft_id, int) or isinstance(draft_id, bool):
            return self._json(422, {"ok": False, "error": "bad_id"})
        try:
            conn = db.connect(self.config.db_path)
            try:
                result = cards.cancel_draft(conn, user["id"], draft_id, self.config.webapp_url)
            finally:
                conn.close()
        except cards.CardError as exc:
            self.log_message("draft_cancel deny: %s", exc.code)
            return self._json(exc.status, {"ok": False, "error": exc.code})
        except sqlite3.Error as exc:
            self.log_message("draft_cancel error: %s", type(exc).__name__)
            return self._json(500, {"ok": False, "error": "server_error"})
        self.log_message("draft_cancel ok")
        self._json(200, result)


class InternalHandler(_Base):
    """Внутренний порт 8081 (сеть kassa_internal), для процесса бота (bot.py). Bearer-токен на каждом запросе.
    Операции «выдать базу» нет. Бот присылает текст и нажатия, все решения принимает код здесь."""
    rate_store: rates.RateStore | None = None

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = "Bearer " + self.config.internal_token
        if hmac.compare_digest(header.encode(), expected.encode()):
            return True
        self._json(401, {"ok": False})
        return False

    def _user(self, data) -> int | None:
        """Допущенный пользователь из тела запроса (id Telegram). Чужой -> 403 и None."""
        uid = data.get("user_id") if isinstance(data, dict) else None
        if isinstance(uid, str) and uid.isdigit():
            uid = int(uid)
        if not isinstance(uid, int) or isinstance(uid, bool) or uid not in self.config.allowed_ids:
            self.log_message("internal deny: not_allowed")
            self._json(403, {"ok": False, "error": "not_allowed"})
            return None
        return uid

    def _db(self, tag, fn):
        """fn(conn) -> результат; на ошибку сам отвечает и возвращает None (результат None у fn не допускается)."""
        try:
            conn = db.connect(self.config.db_path)
            try:
                return fn(conn)
            finally:
                conn.close()
        except cards.CardError as exc:
            self.log_message("%s deny: %s", tag, exc.code)
            self._json(exc.status, {"ok": False, "error": exc.code})
        except records.RecordError as exc:
            self.log_message("%s deny: %s", tag, exc.code)
            self._json(exc.status, exc.payload())
        except sqlite3.Error as exc:
            self.log_message("%s error: %s", tag, type(exc).__name__)
            self._json(500, {"ok": False, "error": "server_error"})
        return None

    def _test(self) -> bool:
        return db.is_test_path(self.config.db_path)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path not in ("/internal/ping", "/internal/armed", "/internal/outbox", "/internal/income"):
            return self._not_found()
        if not self._authorized():
            return
        if path == "/internal/ping":
            return self._json(200, {"ok": True})
        try:
            q = {k: v[0] for k, v in parse_qs(qs, keep_blank_values=True, max_num_fields=5).items()}
        except ValueError:
            return self._json(400, {"ok": False, "error": "bad_query"})
        if path == "/internal/armed":
            if self._user({"user_id": q.get("user")}) is None:
                return
            res = self._db("armed", lambda c: {"armed": cards.armed(c, q["user"])})
            if res is not None:
                self._json(200, {"ok": True, **res})
            return
        if path == "/internal/income":
            return self._income(q)
        return self._outbox_poll(q)

    def _income(self, q):
        """Поступления периода только на чтение: итоги, без расходов, заметок и
        списка записей; период week (пн-вс по KASSA_TZ) или month. Единственная новая ручка токена бота, пишущих
        запросов у неё нет."""
        period = q.get("period", "week")
        res = self._db("income", lambda c: reports.income(c, period, q.get("date") or None, db.today_local()))
        if res is not None:
            self.log_message("income ok")
            self._json(200, res)

    do_HEAD = do_GET

    def _outbox_poll(self, q):
        """Длинный запрос: ждёт до wait секунд, пока в очереди появится сообщение, и отдаёт до 10 штук."""
        try:
            wait = max(0, min(25, int(q.get("wait", "0"))))
        except ValueError:
            wait = 0
        deadline = time.monotonic() + wait
        while True:
            try:
                conn = db.connect(self.config.db_path)
                try:
                    items = cards.outbox_claim(conn)
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self.log_message("outbox error: %s", type(exc).__name__)
                return self._json(500, {"ok": False, "error": "server_error"})
            if items or time.monotonic() >= deadline:
                return self._json(200, {"ok": True, "items": items})
            time.sleep(1.0)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        routes = {"/internal/voice-text": self._voice_text, "/internal/phrase": self._phrase,
                  "/internal/card-action": self._card_action, "/internal/card-message": self._card_message,
                  "/internal/outbox-ack": self._outbox_ack, "/internal/disarm": self._disarm,
                  "/internal/day-mark": self._day_mark}
        fn = routes.get(path)
        if fn is None:
            return self._not_found()
        if not self._authorized():
            self.close_connection = True
            return
        data = self._read_json()
        if data is None:
            return
        fn(data)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _Base.do_POST

    def _from_text(self, data, source):
        uid = self._user(data)
        if uid is None:
            return
        # чат карточки всегда личный чат владельца: id чата из тела запроса не принимаем (правки уходят только туда)
        text, src, chat = data.get("text"), data.get("message_id"), uid
        if not isinstance(text, str):
            return self._json(422, {"ok": False, "error": "bad_text"})

        def run(conn):
            direction = cards.armed(conn, uid) if source == "voice" else None
            res = cards.create_from_text(conn, uid, chat, source, text, src, direction)
            out = {"ok": res["ok"], "duplicate": res.get("duplicate", False)}
            if not res["ok"]:
                out["reply"] = res["reply"]
                out["reject"] = res["reject"]
            else:
                out["card_id"] = res["card_id"]
                if not res.get("duplicate") or res.get("resend"):      # text/buttons есть - бот шлёт карточку
                    v = cards.view(conn, res["card_id"], self.config.webapp_url, self._test(),
                                   fx_rates=self.rate_store.usable() if self.rate_store is not None else {})
                    out["text"], out["buttons"] = v["text"], v["buttons"]
                if source == "voice" and not res.get("duplicate"):
                    cards.disarm(conn, uid)                             # взвод снимаем только после карточки
            return out
        result = self._db(source, run)
        if result is not None:
            self.log_message("%s ok", source)
            self._json(200, result)

    def _voice_text(self, data):
        self._from_text(data, "voice")

    def _phrase(self, data):
        self._from_text(data, "phrase")

    def _disarm(self, data):
        uid = self._user(data)
        if uid is None:
            return
        if self._db("disarm", lambda c: cards.disarm(c, uid) or {}) is not None:
            self._json(200, {"ok": True})

    def _card_action(self, data):
        uid = self._user(data)
        if uid is None:
            return
        action, card_id, value = data.get("action"), data.get("card_id"), data.get("value")
        if not isinstance(action, str) or not isinstance(card_id, int) or isinstance(card_id, bool) \
                or (value is not None and not isinstance(value, str)):
            return self._json(422, {"ok": False, "error": "bad_request"})

        def run(conn):
            # токен бота: только «Отмена» и «Это не деньги»; прочее (в том числе старые кнопки ok, m, undo и
            # прочее) получает «кнопка устарела». Текста карточки в ответе нет.
            return {"ok": True, **cards.act(conn, uid, card_id, action, value, self.config.webapp_url, self._test())}
        name = action if action in cards.CALLBACK_ACTIONS else "unknown"      # в журнал - только известное имя действия
        result = self._db("card_" + name, run)
        if result is not None:
            self.log_message("card %s ok", name)
            self._json(200, result)

    def _day_mark(self, data):
        """«💳 Только карта» / «🚫 Не тратил» (кнопка напоминания 21:30 или текст в чате): отметка дня в daily_marks.
        Отдельный маршрут, а не card-action: карточки у отметки нет, дата берётся из кнопки. Денег не пишет и не
        читает, поэтому внутренний токен (досягаемый моделью) здесь допустим; ответ - как у card-action
        (answer, edit) плюс text для текстового ответа в чат. Тело: user_id, kind, day (необязательно, по умолчанию
        сегодня), via ('card' - кнопка, 'text' - фраза; в audit.via, по умолчанию 'card')."""
        uid = self._user(data)
        if uid is None:
            return
        kind, day, via = data.get("kind"), data.get("day"), data.get("via", "card")
        if not isinstance(kind, str) or (day is not None and not isinstance(day, str)) or not isinstance(via, str):
            return self._json(422, {"ok": False, "error": "bad_request"})
        result = self._db("day_mark", lambda c: {"ok": True, **cards.day_mark(c, uid, kind, day, via=via)})
        if result is not None:
            self.log_message("day_mark ok")
            self._json(200, result)

    def _card_message(self, data):
        uid = self._user(data)
        if uid is None:
            return
        card_id, mid = data.get("card_id"), data.get("message_id")
        if not isinstance(card_id, int) or isinstance(card_id, bool) or not isinstance(mid, int)                 or isinstance(mid, bool) or mid <= 0:
            return self._json(422, {"ok": False, "error": "bad_request"})
        if self._db("card_message", lambda c: cards.set_message_id(c, card_id, mid, uid) or {}) is not None:
            self._json(200, {"ok": True})

    def _outbox_ack(self, data):
        item, ok, mid = data.get("id"), data.get("ok"), data.get("message_id")
        if not isinstance(item, int) or isinstance(item, bool) or not isinstance(ok, bool) \
                or (mid is not None and (not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0)):
            return self._json(422, {"ok": False, "error": "bad_request"})
        res = self._db("outbox_ack", lambda c: {"status": cards.outbox_ack(c, item, ok, message_id=mid)})
        if res is not None:
            self._json(200, {"ok": True, **res})


def make_rate_store(config: Config, fetch=None) -> rates.RateStore:
    """Кэш курсов лежит рядом с базой (в смонтированном каталоге данных), права файла 600. Курс, прыгнувший больше
    чем на 30% от прошлого, не применяется; владельцу уходит тревога (раз в сутки по ключу)."""
    def alarm(kind: str, text: str) -> None:
        try:
            conn = db.connect(config.db_path)
            try:
                cards.alarm_once(conn, config.owner_chat_id, kind, text)
            finally:
                conn.close()
        except Exception as exc:        # noqa: BLE001
            sys.stderr.write("rates alarm error: %s\n" % type(exc).__name__)
    return rates.RateStore(Path(config.db_path).with_name(rates.CACHE_NAME), fetch=fetch,
                           log=lambda msg: sys.stderr.write("%s %s\n" % (time.strftime("%d/%b/%Y %H:%M:%S"), msg)),
                           alarm=alarm)


def make_servers(config: Config, public_addr=("0.0.0.0", 8080), internal_addr=("0.0.0.0", 8081), rate_store=None):
    db.init_db(config.db_path)
    statics = _load_static()
    rate_store = rate_store or make_rate_store(config)
    pub = type("Pub", (PublicHandler,), {"config": config, "statics": statics, "rate_store": rate_store})
    internal = type("Int", (InternalHandler,), {"config": config, "rate_store": rate_store})
    servers = []
    for addr, handler in ((public_addr, pub), (internal_addr, internal)):
        srv = ThreadingHTTPServer(addr, handler)
        srv.daemon_threads = True
        servers.append(srv)
    return servers


def _set_reminder_time(text: str) -> None:
    m = re.fullmatch(r"([01]?[0-9]|2[0-3]):([0-5][0-9])", text.strip())
    if not m:
        raise SystemExit("KASSA_REMINDER_TIME: время вида ЧЧ:ММ, например 21:30")
    cards.REMINDER_HOUR, cards.REMINDER_MINUTE = int(m.group(1)), int(m.group(2))


def main():
    config = Config.from_env()
    store = make_rate_store(config)
    pub, internal = make_servers(config, rate_store=store)
    threading.Thread(target=internal.serve_forever, daemon=True).start()
    rates.RateWorker(store, lambda: db.connect(config.db_path)).start()
    cards.ExpireWorker(lambda: db.connect(config.db_path)).start()
    if config.reminder_enabled:
        _set_reminder_time(os.environ.get("KASSA_REMINDER_TIME") or "21:30")
        cards.ReminderWorker(lambda: db.connect(config.db_path), config.owner_chat_id, config.webapp_url).start()
    sys.stderr.write("kassa-api started %s\n" % time.strftime("%Y-%m-%dT%H:%M:%S"))
    pub.serve_forever()


if __name__ == "__main__":
    main()
