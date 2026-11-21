"""База kassa: схема, миграции, подключение. Только sqlite3 из стандартной библиотеки.

Схема 1: transactions (записи), audit, methods, представление v_legacy.
Схема 2: transactions.closed_by и таблица closed_months (закрытые месяцы; в открытой версии не заполняются).
Схема 3: cards (карточки «Верно / Исправить / Отмена»), awaiting_voice (кнопка «Надиктовать» взвела приём голоса на
10 минут), outbox (исходящие сообщения для бота), daily_marks (метки «за день уже сделано», напоминание).
Схема 4: зарезервирована (пустая).
Схема 5: cards.warned_at (когда показано предупреждение о сумме: подтверждение раньше 2 секунд не принимается),
outbox.message_id (id сообщения в Telegram, чтобы править его после ответа в Mini App).
Схема 6: transactions.payer (кто платит, текст) и transactions.segment ('b2b' | 'b2c' | NULL, только у поступлений).
Схема 7: обмен валют: exchanges (обмен: отдано, дата, способ отдачи) и exchange_legs (1-3 полученные валюты с долей
отданного), transactions.exchange_id (комиссия обмена - обычный расход, связанный с обменом) и exchange_removed
(убранная комиссия). В доходы и расходы обмен не попадает: он живёт в своих таблицах.

Часовой пояс «дня» - переменная KASSA_TZ (имя IANA, по умолчанию UTC).
"""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

def _load_tz(name: str):
    if name in ("", "UTC"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as exc:        # noqa: BLE001
        raise SystemExit("KASSA_TZ: неизвестный часовой пояс %r (нужно имя IANA, например Europe/Berlin)" % name) from exc


LOCAL_TZ = _load_tz(os.environ.get("KASSA_TZ", "UTC"))

# Разгон id для новых записей ТЕСТОВОЙ базы (файл *-test.db).
TEST_ID_FLOOR = 100000

SCHEMA_VERSION = 7

SCHEMA_V1 = """
CREATE TABLE transactions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  idem_key      TEXT NOT NULL UNIQUE,
  direction     TEXT NOT NULL CHECK (direction IN ('in','out')),
  amount_cents  INTEGER NOT NULL CHECK (amount_cents <> 0),
  amount_orig   TEXT,
  currency      TEXT NOT NULL,
  usd_cents     INTEGER,
  category      TEXT,
  note          TEXT,
  method        TEXT REFERENCES methods(code),
  occurred_on   TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled')),
  cancelled_at  TEXT,
  source        TEXT NOT NULL,
  via           TEXT NOT NULL,
  actor_id      TEXT,
  CHECK (via = 'finbot-import' OR actor_id IS NOT NULL)
);
CREATE INDEX ix_tx_occurred ON transactions(occurred_on, status);
CREATE INDEX ix_tx_created ON transactions(created_at);

CREATE TABLE audit (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  actor_id   TEXT,
  action     TEXT NOT NULL,
  entity     TEXT NOT NULL,
  entity_id  TEXT,
  via        TEXT,
  detail     TEXT
);

CREATE VIEW v_legacy AS
SELECT id, occurred_on AS date, direction, amount_cents/100.0 AS amount, currency,
       usd_cents/100.0 AS amount_usd, category, note,
       CASE WHEN source='kassa' THEN 'bot' ELSE source END AS source, created_at, method
FROM transactions WHERE status='active';
"""

# scope: 'in' - способ поступления, 'out' - способ расхода, 'both' - для обоих направлений.
METHODS_SEED = [
    ("bybit", "Bybit", 10, "in"),
    ("tinkoff", "Тинькофф", 20, "in"),
    ("sber", "Сбер", 30, "in"),
    ("redot", "Redot", 40, "in"),
    ("kaspi", "Kaspi", 50, "in"),
    ("cash", "Наличные", 60, "both"),
    ("cold_wallet", "Холодный кошелёк", 70, "in"),
    ("card", "Картой", 80, "out"),
]


# Схема 2. closed_by: у импортированных записей (и любых не-закрытия) в закрытом месяце ставится месяц закрытия; такие
# записи остаются в базе, но в отчётах не участвуют (источник правды - строки source='month-close').
SCHEMA_V2_STEPS = [
    "ALTER TABLE transactions ADD COLUMN closed_by TEXT",
    "CREATE INDEX ix_tx_closed ON transactions(closed_by)",
    """CREATE TABLE closed_months (
  month        TEXT PRIMARY KEY CHECK (length(month) = 7),
  closed_at    TEXT NOT NULL,
  source_name  TEXT NOT NULL,
  source_sha   TEXT NOT NULL,
  rates        TEXT,
  rates_source TEXT,
  expected     TEXT NOT NULL,
  rows_in      INTEGER NOT NULL,
  rows_out     INTEGER NOT NULL
)""",
    "DROP VIEW v_legacy",
    # то, что записали бот и Касса; строки закрытия месяца (итог закрытия) в представление не входят
    """CREATE VIEW v_legacy AS
SELECT id, occurred_on AS date, direction, amount_cents/100.0 AS amount, currency,
       usd_cents/100.0 AS amount_usd, category, note,
       CASE WHEN source='kassa' THEN 'bot' ELSE source END AS source, created_at, method
FROM transactions WHERE status='active' AND source <> 'month-close'""",
    # способ обязателен и у расхода: общие способы работают для обоих направлений, «Картой» заменён конкретными
    "UPDATE methods SET scope='both' WHERE code IN ('bybit','tinkoff','sber','redot','kaspi')",
    "UPDATE methods SET active=0 WHERE code='card'",
]

# Схема 3. Деньги по карточкам пишет только код бэкенда (records.create_record с idem_key card:<id>) после «Верно».
SCHEMA_V3_STEPS = [
    """CREATE TABLE cards (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id       TEXT NOT NULL,
  chat_id        TEXT NOT NULL,
  source         TEXT NOT NULL CHECK (source IN ('voice','phrase')),
  heard          TEXT,
  draft          TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','cancelled','superseded')),
  tx_id          INTEGER,
  message_id     TEXT,
  src_message_id TEXT,
  warned         TEXT,
  created_at     TEXT NOT NULL,
  closed_at      TEXT
)""",
    "CREATE UNIQUE INDEX ux_cards_src ON cards(chat_id, src_message_id) WHERE src_message_id IS NOT NULL",
    """CREATE TABLE awaiting_voice (
  actor_id    TEXT PRIMARY KEY,
  direction   TEXT NOT NULL CHECK (direction IN ('in','out')),
  armed_until TEXT NOT NULL
)""",
    """CREATE TABLE outbox (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at   TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('send','edit')),
  chat_id      TEXT NOT NULL,
  payload      TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  leased_until TEXT,
  sent_at      TEXT
)""",
    "CREATE INDEX ix_outbox_status ON outbox(status, id)",
    """CREATE TABLE daily_marks (
  day        TEXT NOT NULL,
  kind       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (day, kind)
)""",
]

# Схема 4 зарезервирована: шаг пустой, нумерация версий сохранена.
SCHEMA_V4_STEPS: list = []

# Схема 5. Подтверждение записи идёт только из Mini App (подписанный initData): «когда показали предупреждение» и
# «какое сообщение в чате править» хранятся на стороне кассы.
SCHEMA_V5_STEPS = [
    "ALTER TABLE cards ADD COLUMN warned_at TEXT",
    "ALTER TABLE outbox ADD COLUMN message_id TEXT",
    "CREATE INDEX ix_cards_status ON cards(status, created_at)",
]

# Схема 6. segment только у поступлений (CHECK в самой базе: расходу сегмент поставить нельзя).
SCHEMA_V6_STEPS = [
    "ALTER TABLE transactions ADD COLUMN payer TEXT",
    "ALTER TABLE transactions ADD COLUMN segment TEXT "
    "CHECK (segment IS NULL OR (segment IN ('b2b','b2c') AND direction = 'in'))",
]

# Схема 7. Обмен валют. give_part_cents у ноги - сколько из отданного пошло на эту валюту (NULL: доля не посчитана, нет
# курса для оценки); part_estimated=1 - доля не указана владельцем, а оценена по рыночной стоимости полученного.
# «Получена другая валюта, чем отдана» и «сумма долей не больше отданного» проверяет код (exchanges.py). Курс в базе не
# хранится: он считается при чтении как give_part / get. transactions.exchange_id: NULL у всех прочих записей.
SCHEMA_V7_STEPS = [
    """CREATE TABLE exchanges (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  idem_key      TEXT NOT NULL UNIQUE,
  occurred_on   TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  give_cents    INTEGER NOT NULL CHECK (give_cents > 0),
  give_currency TEXT NOT NULL,
  give_method   TEXT REFERENCES methods(code),
  note          TEXT,
  status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled')),
  cancelled_at  TEXT,
  via           TEXT NOT NULL,
  actor_id      TEXT NOT NULL
)""",
    "CREATE INDEX ix_ex_occurred ON exchanges(occurred_on, status)",
    """CREATE TABLE exchange_legs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  exchange_id     INTEGER NOT NULL REFERENCES exchanges(id),
  get_cents       INTEGER NOT NULL CHECK (get_cents > 0),
  get_currency    TEXT NOT NULL,
  get_method      TEXT REFERENCES methods(code),
  give_part_cents INTEGER CHECK (give_part_cents IS NULL OR give_part_cents > 0),
  part_estimated  INTEGER NOT NULL DEFAULT 0 CHECK (part_estimated IN (0,1))
)""",
    "CREATE INDEX ix_exl_exchange ON exchange_legs(exchange_id)",
    "ALTER TABLE transactions ADD COLUMN exchange_id INTEGER REFERENCES exchanges(id)",
    # 1 - комиссия убрана правкой обмена: связь остаётся (через /api/records её не вернуть и не изменить), но с обменом
    # она больше не двигается и в «текущую комиссию» обмена не входит
    "ALTER TABLE transactions ADD COLUMN exchange_removed INTEGER CHECK (exchange_removed IS NULL OR exchange_removed = 1)",
    "CREATE INDEX ix_tx_exchange ON transactions(exchange_id)",
]

# Порядок способов на форме до накопления истории; дальше чипсы строятся по частоте за 90 дней.
DEFAULT_METHOD_ORDER = {
    "out": ["cash", "redot", "tinkoff", "kaspi", "bybit", "sber"],
    "in": ["bybit", "tinkoff", "sber", "redot", "kaspi", "cash", "cold_wallet"],
}


def now_utc_iso() -> str:
    """Тот же формат, что у finance.db: 2026-10-14T12:00:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_local(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def is_test_path(path) -> bool:
    return Path(path).name.endswith("-test.db")


def check_path_allowed(path, env=os.environ) -> bool:
    """True, если база тестовая. Боевая - только при явном KASSA_ALLOW_PROD_DB=1, иначе SystemExit."""
    if is_test_path(path):
        return True
    if env.get("KASSA_ALLOW_PROD_DB") == "1":
        return False
    raise SystemExit("База %s не тестовая: боевая база открывается только при KASSA_ALLOW_PROD_DB=1" % Path(path).name)


def connect(path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)   # транзакциями управляем сами
        conn.execute("PRAGMA synchronous=FULL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _create_v1(conn, test: bool) -> None:
    conn.execute("CREATE TABLE methods (code TEXT PRIMARY KEY, title TEXT NOT NULL, "
                 "sort INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, "
                 "scope TEXT NOT NULL DEFAULT 'in' CHECK (scope IN ('in','out','both')))")
    for stmt in [part for part in SCHEMA_V1.split(";" + chr(10)) if part.strip()]:
        conn.execute(stmt)
    conn.executemany("INSERT INTO methods(code, title, sort, scope) VALUES (?,?,?,?)", METHODS_SEED)
    if test:
        conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('transactions', ?)", (TEST_ID_FLOOR,))


def _migrate_v2(conn) -> None:
    for stmt in SCHEMA_V2_STEPS:
        conn.execute(stmt)


def _migrate_v3(conn) -> None:
    for stmt in SCHEMA_V3_STEPS:
        conn.execute(stmt)


def _migrate_v4(conn) -> None:
    for stmt in SCHEMA_V4_STEPS:
        conn.execute(stmt)


def _migrate_v5(conn) -> None:
    for stmt in SCHEMA_V5_STEPS:
        conn.execute(stmt)


def _migrate_v6(conn) -> None:
    for stmt in SCHEMA_V6_STEPS:
        conn.execute(stmt)


def _migrate_v7(conn) -> None:
    for stmt in SCHEMA_V7_STEPS:
        conn.execute(stmt)


def init_db(path, env=os.environ) -> bool:
    """Создаёт/обновляет схему. Возвращает True для тестовой базы."""
    test = check_path_allowed(path, env)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise SystemExit("База новее кода: user_version=%d" % version)
        if version < SCHEMA_VERSION:
            if version == 0:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                if version == 0:
                    _create_v1(conn, test)
                if version < 2:
                    _migrate_v2(conn)        # 0 -> 2 и 1 -> 2 одним путём
                if version < 3:
                    _migrate_v3(conn)        # 0/1/2 -> 3
                if version < 4:
                    _migrate_v4(conn)        # 0..3 -> 4
                if version < 5:
                    _migrate_v5(conn)        # 0..4 -> 5
                if version < 6:
                    _migrate_v6(conn)        # 0..5 -> 6
                _migrate_v7(conn)            # 0..6 -> 7
                conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()
    return test


def closed_months(conn) -> set:
    """Закрытые месяцы ('YYYY-MM')."""
    return {r[0] for r in conn.execute("SELECT month FROM closed_months")}
