"""Форма в браузере (headless Chromium) с синтетическим initData, подписанным тестовым ключом.

Нужны playwright и доступ к telegram.org (оттуда грузится telegram-web-app.js, как в окне Telegram).
Нажатие нативной MainButton имитируется событием main_button_pressed, как его шлёт клиент Telegram.
"""
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from helpers import BOT_ID, OWNER, make_init_data, new_key
from kassa_api import db
from kassa_api.server import Config, make_servers

playwright_sync = pytest.importorskip("playwright.sync_api")

try:
    urllib.request.urlopen("https://telegram.org/js/telegram-web-app.js", timeout=8).read(10)
    ONLINE = True
except (urllib.error.URLError, OSError):
    ONLINE = False
pytestmark = pytest.mark.skipif(not ONLINE, reason="telegram.org недоступен")

PRIV, PUB = new_key()
TOKEN = "U" * 8 + "abcdefghijklmnopqrstuvwxyz012345"

STUB = """
window.__tg_events = [];
window.TelegramWebviewProxy = { postEvent: function (type, data) { window.__tg_events.push([type, data]); } };
"""
PRESS = "Telegram.WebView.receiveEvent('main_button_pressed', {})"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    db_path = str(tmp_path_factory.mktemp("ui") / "kassa-test.db")
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), "I" * 40, PUB, db_path)
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0))
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield pub.server_address[1], db_path
    for s in (pub, internal):
        s.shutdown()
        s.server_close()


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture()
def page(browser, server):
    port, _ = server
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                              has_touch=True)
    ctx.add_init_script(STUB)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.csp_errors = errors
    yield pg
    ctx.close()


def open_form(page, port, user_id=OWNER, init=None, method="Наличные"):
    """Открывает форму. Способ обязателен, поэтому по умолчанию сразу выбирается «Наличные» (method=None - не трогать)."""
    init = init or make_init_data(PRIV, user_id=user_id)
    hash_ = urllib.parse.urlencode({"tgWebAppData": init, "tgWebAppVersion": "8.0", "tgWebAppPlatform": "ios"})
    page.goto(f"http://127.0.0.1:{port}/{TOKEN}/#{hash_}")
    page.wait_for_selector("#form:not([hidden]), #stub:not([hidden])")
    if method and page.locator("#form").is_visible():
        pick_method(page, method)


def pick_method(page, title):
    """Выбирает способ на главном экране: чипс, а если среди чипсов его нет - через «Ещё» и список."""
    loc = page.locator("#method button", has_text=re.compile(r"^\s*" + re.escape(title) + r"\s*$"))
    if loc.count():
        loc.first.click()
        return
    chip(page, "method", "Ещё ▾")
    page.select_option("#method-select", label=title)


def main_button(page):
    """Последнее состояние нативной кнопки «Записать», как его видит Telegram: {text, is_active, is_visible, ...}."""
    raw = page.evaluate("window.__tg_events.filter(e => e[0] === 'web_app_setup_main_button').slice(-1)[0][1]")
    return json.loads(raw) if isinstance(raw, str) else raw


def count(db_path, where="1=1"):
    c = db.connect(db_path, readonly=True)
    try:
        return c.execute("SELECT COUNT(*) FROM transactions WHERE " + where).fetchone()[0]
    finally:
        c.close()


def chip(page, container, text):
    """Нажимает кнопку с ТОЧНЫМ текстом (USD не должен попасть на USDT)."""
    page.locator(f"#{container} button", has_text=re.compile(r"^\s*" + re.escape(text) + r"\s*$")).first.click()


def press(page):
    page.evaluate(PRESS)


def test_form_opens_in_telegram_window(page, server):
    port, path = server
    open_form(page, port)
    assert page.locator("#form").is_visible()
    assert page.locator("#badge-test").is_visible()                       # пометка ТЕСТ
    assert page.locator("#direction button.on").inner_text() == "Расход"   # расход по умолчанию
    events = page.evaluate("window.__tg_events.map(e => e[0])")
    assert "web_app_ready" in events and "web_app_setup_main_button" in events
    assert not [e for e in page.csp_errors if "Content Security Policy" in e or "Refused" in e]


def test_unknown_user_gets_stub_not_form(page, server):
    port, path = server
    open_form(page, port, user_id=555)
    assert page.locator("#form").is_hidden()
    assert "Нет доступа" in page.locator("#stub").inner_text()


def test_expense_via_main_button_and_double_press(page, server):
    port, path = server
    open_form(page, port)
    before = count(path)
    page.fill("#amount", "500")
    page.fill("#note", "еда в кафе")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    assert "кафе" in page.locator("#ok-text").inner_text()
    press(page)                         # форма очищена: второе нажатие не создаёт записи
    page.wait_for_selector("#err:not([hidden])")
    assert "Записано №" in page.locator("#ok").inner_text() or "Сумма" in page.locator("#err").inner_text()
    assert count(path) == before + 1
    c = db.connect(path, readonly=True)
    r = c.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
    assert (r["amount_cents"], r["direction"], r["category"], r["via"], r["actor_id"]) == \
        (50000, "out", "кафе", "webapp", str(OWNER))
    assert r["note"] == "еда в кафе"


def test_two_fast_presses_make_one_record(page, server):
    port, path = server
    open_form(page, port)
    before = count(path)
    page.fill("#amount", "321")
    page.fill("#note", "продукты")
    page.evaluate(PRESS + "; " + PRESS)
    page.wait_for_selector("#ok:not([hidden])")
    page.wait_for_timeout(300)
    assert count(path) == before + 1


def test_income_requires_method_in_form(page, server):
    port, path = server
    open_form(page, port)
    chip(page, "direction", "Поступление")
    page.fill("#amount", "100")
    before = count(path)
    assert main_button(page)["is_active"] is False                        # способ не выбран: «Записать» неактивна
    assert page.locator("#method-hint").is_visible()
    press(page)               # библиотека Telegram не пропускает нажатие неактивной кнопки: записи нет, подсказка на месте
    page.wait_for_timeout(400)
    assert page.locator("#method-hint").is_visible() and page.locator("#ok").is_hidden()
    assert count(path) == before
    assert count(path, "via='webapp' AND direction='in' AND method IS NULL") == 0
    assert page.locator("#method button").all_inner_texts() == ["Bybit", "Тинькофф", "Сбер", "Ещё ▾"]
    chip(page, "method", "Ещё ▾")
    titles = page.locator("#method-select option").all_inner_texts()
    assert titles == ["Выберите способ", "Bybit", "Тинькофф", "Сбер", "Redot", "Kaspi", "Наличные", "Холодный кошелёк",
                      "Другой…"]
    page.select_option("#method-select", "redot")
    assert main_button(page)["is_active"] is True and page.locator("#method-hint").is_hidden()
    chip(page, "cur", "USD")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    c = db.connect(path, readonly=True)
    r = c.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
    assert (r["direction"], r["method"], r["currency"], r["amount_cents"]) == ("in", "redot", "USD", 10000)


def test_other_method_creates_row(page, server):
    port, path = server
    open_form(page, port)
    chip(page, "direction", "Поступление")
    page.fill("#amount", "42,5")
    page.fill("#note", "возврат от друга")
    chip(page, "method", "Ещё ▾")
    page.select_option("#method-select", "__other")
    assert main_button(page)["is_active"] is False                        # «Другой» без названия - способа нет
    page.fill("#method-other", "Бабушкин сундук")
    assert main_button(page)["is_active"] is True
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    c = db.connect(path, readonly=True)
    m = c.execute("SELECT * FROM methods WHERE title='Бабушкин сундук'").fetchone()
    assert m is not None
    r = c.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
    assert r["method"] == m["code"] and r["amount_cents"] == 4250 and r["category"] == "возврат"
    # новый способ сразу виден в списке
    chip(page, "direction", "Расход")
    chip(page, "direction", "Поступление")
    assert "Бабушкин сундук" in page.locator("#method button").all_inner_texts()    # он последний: стоит чипсом
    chip(page, "method", "Ещё ▾")
    assert "Бабушкин сундук" in page.locator("#method-select option").all_inner_texts()


def test_backdated_record(page, server):
    port, path = server
    open_form(page, port)
    page.fill("#amount", "77")
    page.fill("#note", "бар")
    chip(page, "date", "Вчера")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    c = db.connect(path, readonly=True)
    r = c.execute("SELECT occurred_on, created_at FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
    meta_today = db.today_local()
    from datetime import date, timedelta
    assert r["occurred_on"] == (date.fromisoformat(meta_today) - timedelta(days=1)).isoformat()
    assert r["created_at"][:10] >= r["occurred_on"]


def test_duplicate_warning_needs_second_press(page, server):
    port, path = server
    open_form(page, port)
    page.fill("#amount", "888")
    page.fill("#note", "кафе")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    before = count(path)
    page.fill("#amount", "888")
    page.fill("#note", "кафе")
    press(page)
    page.wait_for_selector("#warn:not([hidden])")
    assert count(path) == before
    texts = page.evaluate("window.__tg_events.filter(e => e[0] === 'web_app_setup_main_button').slice(-1)[0][1]")
    assert "Записать всё равно" in str(texts)
    press(page)                                  # второй тап сразу после предупреждения не считается
    page.wait_for_timeout(400)
    assert count(path) == before
    page.wait_for_timeout(1800)                  # осознанное подтверждение после паузы
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    assert count(path) == before + 1


def test_outlier_needs_yes(page, server):
    port, path = server
    c = db.connect(path)
    for i in range(10):
        c.execute("INSERT INTO transactions(idem_key, direction, amount_cents, currency, category, occurred_on, "
                  "created_at, source, via, actor_id) VALUES (?, 'out', 10000, 'KZT', 'x', ?, '2026-10-01T00:00:00Z', "
                  "'kassa', 'webapp', '1')", ("seed%d" % i, db.today_local()))
    c.close()
    open_form(page, port)
    before = count(path)
    chip(page, "cur", "Ещё ▾")
    page.fill("#cur-search", "kzt")
    page.locator('#cur-results [data-code="KZT"]').click()
    page.fill("#amount", "5000")
    press(page)
    page.wait_for_selector("#warn:not([hidden])")
    assert count(path) == before
    last = page.evaluate("window.__tg_events.filter(e => e[0] === 'web_app_setup_main_button').slice(-1)[0][1]")
    assert "Да, столько" in str(last)
    page.wait_for_timeout(2100)                  # предупреждение надо успеть прочитать: тап раньше 2 секунд не считается
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    assert count(path) == before + 1


def test_bad_amount_is_blocked_in_form(page, server):
    port, path = server
    open_form(page, port)
    before = count(path)
    for bad in ("", "abc", "0", "1,234"):
        page.fill("#amount", bad)
        page.fill("#note", "кафе")
        press(page)
        page.wait_for_selector("#err:not([hidden])")
        assert count(path) == before


def test_untrusted_text_is_not_rendered_as_html(page, server):
    port, path = server
    open_form(page, port)
    chip(page, "direction", "Поступление")
    page.fill("#amount", "9")
    page.fill("#note", "<img src=x onerror=__p=2>")
    chip(page, "method", "Ещё ▾")
    page.select_option("#method-select", "__other")
    page.fill("#method-other", "<img src=x onerror=__p=1>")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    chip(page, "direction", "Расход")
    chip(page, "direction", "Поступление")
    assert page.evaluate("window.__p") is None
    assert "<img src=x onerror=__p=1>" in page.locator("#method button").all_inner_texts()
    chip(page, "method", "Ещё ▾")
    assert "<img src=x onerror=__p=1>" in page.locator("#method-select option").all_inner_texts()
