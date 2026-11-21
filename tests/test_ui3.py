"""Форма в браузере (headless Chromium): выбор валюты с поиском, вкладки «Сегодня», «Месяц», «Последние»,
удаление/возврат, смена категории, плашка «Отменить». Каждый тест получает свою чистую базу."""
import json
import re
import threading
import urllib.parse
import uuid
from datetime import date, timedelta

import pytest

from helpers import BOT_ID, OWNER, make_init_data, new_key
from kassa_api import db
from kassa_api.server import Config, make_servers

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402
from test_ui import ONLINE, PRESS, STUB, browser, chip, main_button, open_form, pick_method, press  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not ONLINE, reason="telegram.org недоступен")

PRIV, PUB = new_key()
TOKEN = "V" * 8 + "abcdefghijklmnopqrstuvwxyz012345"
TODAY = db.today_local()


@pytest.fixture()
def server(tmp_path):
    db_path = str(tmp_path / "kassa-test.db")
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), "I" * 40, PUB, db_path)
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0))
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield pub.server_address[1], db_path
    for s in (pub, internal):
        s.shutdown()
        s.server_close()


@pytest.fixture()
def page(browser, server):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                              has_touch=True)
    ctx.add_init_script(STUB)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.js_errors = errors
    yield pg
    ctx.close()
    # страница не нарушала собственную политику безопасности (CSP) ни в одном тесте
    assert not [e for e in errors if "Content Security Policy" in e or "Refused" in e], errors


def open_app(page, port, user_id=OWNER, method="Наличные"):
    """Открывает форму; способ обязателен, поэтому по умолчанию сразу выбирается «Наличные» (method=None - не трогать)."""
    init = make_init_data(PRIV, user_id=user_id)
    hash_ = urllib.parse.urlencode({"tgWebAppData": init, "tgWebAppVersion": "8.0", "tgWebAppPlatform": "ios"})
    page.goto(f"http://127.0.0.1:{port}/{TOKEN}/#{hash_}")
    page.wait_for_selector("#form:not([hidden]), #stub:not([hidden])")
    if method and page.locator("#form").is_visible():
        pick_method(page, method)


def btn(page, container, text):
    return page.locator(f"#{container} button", has_text=re.compile(r"^\s*" + re.escape(text) + r"\s*$")).first


def tab(page, name):
    page.locator(f'#tabs [data-tab="{name}"]').click()


def seed(path, **kw):
    row = dict(direction="out", cents=10000, cur="TRY", day=TODAY, usd=None, category="кафе", method=None, note=None,
               status="active", created=None)
    row.update(kw)
    created = row["created"] or "%sT09:%02d:00Z" % (row["day"], uuid.uuid4().int % 60)
    c = db.connect(path)
    cur = c.execute(
        "INSERT INTO transactions(idem_key, direction, amount_cents, currency, usd_cents, category, note, method, "
        "occurred_on, created_at, status, cancelled_at, source, via, actor_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'kassa', 'webapp', '1')",
        ("seed-" + uuid.uuid4().hex, row["direction"], row["cents"], row["cur"], row["usd"], row["category"],
         row["note"], row["method"], row["day"], created, row["status"],
         created if row["status"] == "cancelled" else None))
    c.close()
    return cur.lastrowid


def fetch_row(path, rid):
    c = db.connect(path, readonly=True)
    r = c.execute("SELECT * FROM transactions WHERE id=?", (rid,)).fetchone()
    c.close()
    return dict(r) if r else None


def fmt(cents, signed=False):
    """Как fmtCents в app.js: узкий неразрывный пробел между тысячами, запятая, копейки только если есть."""
    neg, a = cents < 0, abs(cents)
    s = "{:,}".format(a // 100).replace(",", " ")
    if a % 100:
        s += ",%02d" % (a % 100)
    return ("−" if neg else ("+" if signed else "")) + s


def shown(page, selector):
    """Секция показана, если у неё нет атрибута hidden (пустая секция для is_visible() невидима: размер 0)."""
    return page.locator(selector).get_attribute("hidden") is None


def main_button_visible(page):
    return page.evaluate("""() => {
      var ev = window.__tg_events.filter(e => e[0] === 'web_app_setup_main_button').slice(-1)[0];
      var d = typeof ev[1] === 'string' ? JSON.parse(ev[1]) : ev[1];
      return d.is_visible;
    }""")


# --- валюты ---------------------------------------------------------------------------------------------------
def test_main_currency_chips_in_history_order(page, server):
    port, _ = server
    open_app(page, port)
    assert page.locator("#cur button").all_inner_texts() == ["TRY", "USDT", "USD", "RUB", "Ещё ▾"]
    assert page.locator("#cur button.on").inner_text() == "TRY"
    assert page.locator("#cur-panel").is_hidden()
    assert not page.js_errors


def open_search(page):
    btn(page, "cur", "Ещё ▾").click()
    page.wait_for_selector("#cur-panel:not([hidden])")


def result_codes(page):
    return page.eval_on_selector_all("#cur-results [data-code]", "els => els.map(e => e.getAttribute('data-code'))")


@pytest.mark.parametrize("query,first,exactly", [
    ("лира", "TRY", ["TRY"]),
    ("Лира", "TRY", ["TRY"]),
    ("лиру", "TRY", ["TRY"]),
    ("тенге", "KZT", ["KZT"]),
    ("ТЕНГЕ", "KZT", ["KZT"]),
    ("usd", "USD", None),
    ("try", "TRY", None),
    ("евро", "EUR", None),
    ("доллар", "USD", None),
    ("долларов", "USD", None),
    ("рубль", "RUB", None),
    ("юань", "CNY", ["CNY"]),
    ("дирхам", "AED", None),
    ("лари", "GEL", ["GEL"]),
    ("тезер", "USDT", ["USDT"]),
    ("биткоин", "BTC", ["BTC"]),
    ("bitcoin", "BTC", ["BTC"]),
    ("tenge", "KZT", ["KZT"]),
    ("турция", "TRY", ["TRY"]),
    ("золото", "XAU", None),
    ("kzt", "KZT", ["KZT"]),
], ids=lambda v: v if isinstance(v, str) and v.isascii() else None)
def test_currency_search(page, server, query, first, exactly):
    port, _ = server
    open_app(page, port)
    open_search(page)
    page.fill("#cur-search", query)
    codes = result_codes(page)
    assert codes and codes[0] == first, codes
    if exactly is not None:
        assert codes == exactly


def test_search_lira_and_tenge_headless(page, server):
    """Из критериев готовности: «лира» -> TRY, «тенге» -> KZT."""
    port, _ = server
    open_app(page, port)
    open_search(page)
    page.fill("#cur-search", "лира")
    assert result_codes(page)[0] == "TRY"
    assert "Турецкая лира" in page.locator('#cur-results [data-code="TRY"]').inner_text()
    page.fill("#cur-search", "тенге")
    assert result_codes(page) == ["KZT"]


@pytest.mark.parametrize("query", ["расчетный", "Расчётный", "РАСЧЕТНАЯ"])
def test_search_treats_yo_as_ye(page, server, query):
    port, _ = server
    open_app(page, port)
    open_search(page)
    page.fill("#cur-search", query)
    assert {"XAD", "UYW"} & set(result_codes(page))


def test_search_pick_for_income_is_remembered_only_for_income(page, server):
    port, path = server
    open_app(page, port)
    btn(page, "direction", "Поступление").click()
    open_search(page)
    page.fill("#cur-search", "тенге")
    page.locator('#cur-results [data-code="KZT"]').click()
    assert page.locator("#cur button.on").inner_text() == "KZT"
    btn(page, "direction", "Расход").click()
    assert page.locator("#cur button.on").inner_text() == "TRY"
    btn(page, "direction", "Поступление").click()
    assert page.locator("#cur button.on").inner_text() == "KZT"
    page.fill("#amount", "10")
    pick_method(page, "Redot")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    open_app(page, port)
    btn(page, "direction", "Поступление").click()
    assert page.locator("#cur button.on").inner_text() == "KZT"


def test_search_dollar_lists_all_dollars_main_first(page, server):
    port, _ = server
    open_app(page, port)
    open_search(page)
    page.fill("#cur-search", "доллар")
    codes = result_codes(page)
    assert codes[0] == "USD" and {"AUD", "CAD", "HKD", "NZD", "SGD", "XCD"} <= set(codes)
    assert "TRY" not in codes and "KZT" not in codes


def test_search_no_match_message_and_empty_query_full_list(page, server):
    port, _ = server
    open_app(page, port)
    open_search(page)
    page.fill("#cur-search", "qqqq")
    assert result_codes(page) == []
    assert "Ничего не найдено" in page.locator("#cur-results").inner_text()
    page.fill("#cur-search", "")
    codes = result_codes(page)
    assert len(codes) >= 176 + 4 and codes[:5] == ["TRY", "USDT", "USD", "RUB", "EUR"]
    assert {"USDC", "BTC", "ETH", "KZT", "AED", "GEL", "SCR", "JPY"} <= set(codes)


def test_search_pick_is_selected_remembered_and_saved(page, server):
    port, path = server
    open_app(page, port)
    open_search(page)
    page.fill("#cur-search", "тенге")
    page.locator('#cur-results [data-code="KZT"]').click()
    assert page.locator("#cur-panel").is_hidden()
    assert page.locator("#cur button.on").inner_text() == "KZT"
    assert page.locator("#cur button").all_inner_texts()[:5] == ["TRY", "USDT", "USD", "RUB", "KZT"]
    # у направления «Поступление» свой выбор, возврат к «Расходу» помнит KZT
    btn(page, "direction", "Поступление").click()
    assert page.locator("#cur button.on").inner_text() == "TRY"
    btn(page, "direction", "Расход").click()
    assert page.locator("#cur button.on").inner_text() == "KZT"
    page.fill("#amount", "250")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    c = db.connect(path, readonly=True)
    assert c.execute("SELECT currency FROM transactions ORDER BY id DESC LIMIT 1").fetchone()[0] == "KZT"
    # следующее открытие формы: KZT уже выбрана (last_currency направления)
    open_app(page, port)
    assert page.locator("#cur button.on").inner_text() == "KZT"
    assert page.locator("#cur button").all_inner_texts() == ["TRY", "USDT", "USD", "RUB", "KZT", "Ещё ▾"]


def test_recent_currencies_shown_above_search(page, server):
    port, path = server
    seed(path, cur="AED")
    seed(path, cur="AED")
    seed(path, cur="GEL")
    seed(path, cur="CNY", day=(date.fromisoformat(TODAY) - timedelta(days=90)).isoformat())     # старше 60 дней
    seed(path, cur="USD")                                                                        # основная
    open_app(page, port)
    open_search(page)
    assert page.locator("#cur-recent button").all_inner_texts() == ["AED", "GEL"]
    assert page.locator("#cur-recent-block").bounding_box()["y"] < page.locator("#cur-search").bounding_box()["y"]
    page.locator("#cur-recent button", has_text="GEL").click()
    assert page.locator("#cur button.on").inner_text() == "GEL"
    assert page.locator("#cur-panel").is_hidden()


def test_no_recent_block_without_history(page, server):
    port, path = server
    seed(path, cur="TRY")
    open_app(page, port)
    open_search(page)
    assert page.locator("#cur-recent-block").is_hidden()


def test_search_result_text_is_not_html(page, server):
    port, _ = server
    open_app(page, port)
    open_search(page)
    page.fill("#cur-search", "<img src=x onerror=__p=1>")
    assert page.evaluate("window.__p") is None
    assert "Ничего не найдено" in page.locator("#cur-results").inner_text()


# --- плашка «Отменить» -----------------------------------------------------------------------------------------
def test_undo_plate_cancels_just_saved_record(page, server):
    port, path = server
    open_app(page, port)
    page.fill("#amount", "321")
    page.fill("#note", "кафе")
    press(page)
    page.wait_for_selector("#undo:not([hidden])")
    rid = int(re.search(r"№(\d+)", page.locator("#ok-text").inner_text()).group(1))
    assert fetch_row(path, rid)["status"] == "active"
    page.locator("#undo").click()
    expect(page.locator("#ok-text")).to_contain_text("отменена")
    assert page.locator("#undo").is_hidden()
    row = fetch_row(path, rid)
    assert row["status"] == "cancelled" and row["cancelled_at"] and row["amount_cents"] == 32100


# --- вкладки ---------------------------------------------------------------------------------------------------
def test_tabs_switch_and_main_button_only_on_record(page, server):
    port, _ = server
    open_app(page, port)
    assert page.locator("#tabs button").all_inner_texts() == ["Запись", "Входящие", "Сегодня", "Месяц", "Последние", "Обмены"]
    assert page.locator("#form").is_visible() and main_button_visible(page) is True
    tab(page, "today")
    assert shown(page, "#view-today") and not shown(page, "#form")
    assert main_button_visible(page) is False
    tab(page, "month")
    assert shown(page, "#view-month") and not shown(page, "#view-today")
    tab(page, "recent")
    assert shown(page, "#view-recent") and not shown(page, "#view-month")
    tab(page, "record")
    assert page.locator("#form").is_visible() and main_button_visible(page) is True
    assert not page.js_errors


def test_today_view_totals_and_list(page, server):
    port, path = server
    seed(path, direction="out", cents=150050, cur="TRY", usd=4500, category="продукты")
    seed(path, direction="out", cents=20000, cur="TRY", usd=600, category="кафе", note="кофе")
    seed(path, direction="in", cents=10000, cur="USDT", usd=10000, category="доход", method="bybit")
    seed(path, direction="out", cents=99999, cur="TRY", status="cancelled")
    seed(path, direction="out", cents=77700, cur="TRY", day=(date.fromisoformat(TODAY) - timedelta(days=1)).isoformat())
    open_app(page, port)
    tab(page, "today")
    page.wait_for_selector("#today-totals .card")
    cards = {c.query_selector(".card-title").inner_text(): c.inner_text()
             for c in page.query_selector_all("#today-totals .card")}
    assert set(cards) == {"TRY", "USDT"}
    assert fmt(170050) in cards["TRY"] and fmt(0) in cards["TRY"] and fmt(-170050, True) in cards["TRY"]
    assert fmt(10000, True) in cards["USDT"]
    assert fmt(5100) in page.locator("#today-usd").inner_text()                       # 4500 + 600 расходов
    assert "≈ USD" in page.locator("#today-usd").inner_text()
    rows = page.locator("#today-list li").all_inner_texts()
    assert len(rows) == 3 and any("кофе" in r for r in rows) and any("Bybit" in r for r in rows)
    assert not any("удалена" in r for r in rows)


def test_today_view_flags_records_without_rate(page, server):
    port, path = server
    seed(path, cents=1000, usd=None)
    open_app(page, port)
    tab(page, "today")
    page.wait_for_selector("#today-usd:not(:empty)")
    assert page.locator("#today-usd").inner_text() == "≈ USD: нет курса для 1 записи"


def test_today_empty(page, server):
    port, _ = server
    open_app(page, port)
    tab(page, "today")
    page.wait_for_selector("#today-totals .note")
    assert "Пока пусто" in page.locator("#today-totals").inner_text()


def test_month_view_navigation_and_groups(page, server):
    port, path = server
    first_of_month = TODAY[:8] + "01"
    prev_day = (date.fromisoformat(first_of_month) - timedelta(days=1)).isoformat()
    seed(path, cents=300000, cur="TRY", category="жильё", usd=7000, day=first_of_month)
    seed(path, cents=1000, cur="USD", direction="in", category="доход", method=None, usd=1000)
    seed(path, cents=555500, cur="TRY", category="бар", day=prev_day)
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-usd .sum-row")
    months = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
             "декабрь"]
    assert page.locator("#month-title").inner_text().lower().startswith(months[int(TODAY[5:7]) - 1])
    assert page.locator("#month-next").is_disabled()
    cats = page.locator("#month-cats-bars .bar-row")
    assert cats.count() == 1 and "жильё" in cats.first.inner_text()
    assert page.locator("#month-cats-bars .bar-detail").count() == 0                   # детали закрыты, пока не нажали
    cats.first.click()                                                                  # нажатие раскрывает детали
    assert fmt(300000) + " TRY" in page.locator("#month-cats-bars .bar-detail").inner_text()
    methods = page.locator("#month-methods-bars .bar-row")
    assert methods.count() == 1 and "без способа" in methods.first.inner_text()
    page.locator("#month-prev").click()
    expect(page.locator("#month-next")).to_be_enabled()
    page.locator("#month-cur-toggle").click()                                          # суммы по валютам: свёрнуты внизу
    assert fmt(555500) in page.locator("#month-totals").inner_text()
    assert "бар" in page.locator("#month-cats-bars").inner_text()
    assert page.locator("#month-methods-bars").inner_text().strip() == "Поступлений нет"
    page.locator("#month-next").click()
    expect(page.locator("#month-next")).to_be_disabled()
    assert "жильё" in page.locator("#month-cats-bars").inner_text()


def test_recent_delete_with_confirmation_and_restore(page, server):
    port, path = server
    keep = seed(path, cents=11100, category="кафе", created="%sT08:00:00Z" % TODAY)
    target = seed(path, cents=22200, category="бар", created="%sT09:00:00Z" % TODAY)
    open_app(page, port)
    tab(page, "recent")
    page.wait_for_selector("#recent-list li")
    first = page.locator("#recent-list li").first
    assert "бар" in first.inner_text()                                                   # свежие сверху
    assert page.locator("#recent-list button").count() == 0                              # кнопок в списке нет
    first.click()
    first.locator("button", has_text="Удалить").click()
    assert "Удалить запись" in first.inner_text()
    first.locator("button", has_text="Нет").click()
    assert fetch_row(path, target)["status"] == "active"                                 # «Нет» ничего не удалило
    first = page.locator("#recent-list li").first
    first.locator("button", has_text="Удалить").click()
    first.locator("button", has_text="Да, удалить").click()
    page.wait_for_selector("#recent-list li.gone")
    row = fetch_row(path, target)
    assert row["status"] == "cancelled" and row["cancelled_at"] and row["amount_cents"] == 22200
    assert fetch_row(path, keep)["status"] == "active"
    assert "удалена" in page.locator("#recent-list li.gone").inner_text()
    assert "удалена" in page.locator("#recent-msg").inner_text()
    tab(page, "today")                                                                    # из отчёта исчезла
    page.wait_for_selector("#today-totals .card")
    assert fmt(22200) not in page.locator("#today-totals").inner_text()
    tab(page, "recent")
    page.wait_for_selector("#recent-list li.gone")
    page.locator("#recent-list li.gone").click()
    page.locator("#recent-list li.gone button", has_text="Вернуть").click()
    page.wait_for_selector("#recent-list li:not(.gone) >> text=бар")
    assert fetch_row(path, target)["status"] == "active" and fetch_row(path, target)["cancelled_at"] is None
    assert page.locator("#recent-list li.gone").count() == 0


def test_recent_edit_category_and_method_keeps_amount(page, server):
    port, path = server
    rid = seed(path, direction="in", cents=12345, cur="USD", usd=12345, category="доход", method=None)
    open_app(page, port)
    tab(page, "recent")
    page.wait_for_selector("#recent-list li")
    li = page.locator("#recent-list li").first
    li.click()
    li.locator("button", has_text="Изменить").click()
    ed = li.locator(".editor")
    ed.locator("select").nth(1).select_option("возврат")
    ed.locator("select").nth(2).select_option("sber")
    ed.locator("button", has_text="Сохранить").click()
    expect(page.locator("#recent-msg")).to_contain_text("изменена")
    row = fetch_row(path, rid)
    assert (row["category"], row["method"], row["amount_cents"], row["currency"], row["usd_cents"]) == \
        ("возврат", "sber", 12345, "USD", 12345)
    c = db.connect(path, readonly=True)
    a = c.execute("SELECT action, actor_id, entity_id FROM audit WHERE action='update'").fetchall()
    assert [(x["actor_id"], x["entity_id"]) for x in a] == [(str(OWNER), str(rid))]


def test_recent_no_method_filter(page, server):
    port, path = server
    seed(path, direction="in", category="доход", method=None, cur="USD")
    seed(path, direction="in", category="доход", method="cash", cur="USD")
    seed(path, direction="out", category="кафе")
    open_app(page, port)
    tab(page, "recent")
    page.wait_for_selector("#recent-list li")
    assert page.locator("#recent-list li").count() == 3
    btn(page, "recent-filter", "Без способа (1)").click()
    expect(page.locator("#recent-list li")).to_have_count(1)
    assert "без способа" in page.locator("#recent-list li").first.inner_text()


def test_untrusted_text_in_reports_is_not_rendered_as_html(page, server):
    port, path = server
    evil = "<img src=x onerror=__p=1>"
    seed(path, category=evil, note=evil, direction="in", method=None, cur="USD")
    open_app(page, port)
    for name, sel in (("today", "#today-list"), ("month", "#month-cats-bars"), ("recent", "#recent-list")):
        tab(page, name)
        page.wait_for_timeout(400)
        assert page.evaluate("window.__p") is None
    assert evil in page.locator("#today-list").inner_text() or evil in page.locator("#recent-list").inner_text()
    assert page.locator("#recent-list img, #today-list img, #month-cats-bars img").count() == 0


def test_tab_requests_need_auth_stub_for_stranger(page, server):
    port, _ = server
    open_app(page, port, user_id=555)
    assert page.locator("#tabs").is_hidden() and page.locator("#form").is_hidden()
    assert "Нет доступа" in page.locator("#stub").inner_text()
