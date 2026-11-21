"""Этап 7 в браузере: диаграммы (расходы по категориям полосами, поступления по способам, динамика по месяцам) и ширина
iPhone без горизонтальной прокрутки. Цифры на диаграммах сверяются с собственным SQL по базе и с текстом отчёта."""
import json
import re
import urllib.parse
from datetime import date, timedelta

import pytest

from helpers import OWNER, make_init_data
from kassa_api import db
from test_ui import ONLINE, browser, chip, press  # noqa: F401
from test_ui3 import PUB, PRIV, TODAY, TOKEN, fmt, open_app, page, seed, server, tab  # noqa: F401

pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.skipif(not ONLINE, reason="telegram.org недоступен")

LIGHT = {"bg_color": "#ffffff", "text_color": "#000000", "hint_color": "#707579", "button_color": "#3390ec",
         "button_text_color": "#ffffff", "secondary_bg_color": "#f4f4f5", "accent_text_color": "#3390ec",
         "destructive_text_color": "#e53935", "section_separator_color": "#e7e8ec"}
DARK = {"bg_color": "#212121", "text_color": "#ffffff", "hint_color": "#aaaaaa", "button_color": "#8774e1",
        "button_text_color": "#ffffff", "secondary_bg_color": "#181818", "accent_text_color": "#8774e1",
        "destructive_text_color": "#ec3942", "section_separator_color": "#2c2c2c"}


def prev_month(key):
    y, m = int(key[:4]), int(key[5:7]) - 1
    if m == 0:
        y, m = y - 1, 12
    return "%04d-%02d" % (y, m)


MONTH0 = TODAY[:7]
MONTH1 = prev_month(MONTH0)
MONTH2 = prev_month(MONTH1)


def themed(page, port, theme):
    init = make_init_data(PRIV, user_id=OWNER)
    h = urllib.parse.urlencode({"tgWebAppData": init, "tgWebAppVersion": "8.0", "tgWebAppPlatform": "ios",
                                "tgWebAppThemeParams": json.dumps(theme)})
    page.goto("http://127.0.0.1:%d/%s/#%s" % (port, TOKEN, h))
    page.wait_for_selector("#form:not([hidden])")


def usd_sum(path, month=None, day=None, direction="out", cat=None):
    c = db.connect(path, readonly=True)
    q = "SELECT COALESCE(SUM(usd_cents),0) FROM transactions WHERE status='active' AND closed_by IS NULL AND direction=?"
    args = [direction]
    if month:
        q += " AND substr(occurred_on,1,7)=?"
        args.append(month)
    if day:
        q += " AND occurred_on=?"
        args.append(day)
    if cat:
        q += " AND category=?"
        args.append(cat)
    v = c.execute(q, args).fetchone()[0]
    c.close()
    return v


def card(page):
    """Крупная карточка итога: (поступления, расходы, нетто) в центах USD из data-cents."""
    g = lambda k: int(page.locator("#month-usd [data-k=%s]" % k).get_attribute("data-cents"))
    return g("in"), g("out"), g("net")


def bar_rows(page, box):
    return page.eval_on_selector_all(
        "#%s .bar-row" % box,
        """rows => rows.map(r => ({name: r.querySelector('.bar-name').textContent,
                                   cents: +r.querySelector('.bar-val').getAttribute('data-cents'),
                                   text: r.querySelector('.bar-val').textContent,
                                   width: r.querySelector('.bar-fill').style.width}))""")


def seed_month(path, day):
    seed(path, cents=1000000, cur="TRY", category="жильё", usd=30000, day=day)
    seed(path, cents=300000, cur="TRY", category="продукты", usd=9000, day=day)
    seed(path, cents=120000, cur="TRY", category="кафе", usd=3600, day=day)
    seed(path, cents=40000, cur="TRY", category="бар", usd=1200, day=day)
    seed(path, cents=9000, cur="USD", category="такси", usd=900, day=day)
    seed(path, cents=500000, cur="USD", direction="in", category="доход", method="bybit", usd=500000, day=day)
    seed(path, cents=100000, cur="USD", direction="in", category="доход", method="tinkoff", usd=100000, day=day)


# --- расходы по категориям --------------------------------------------------------------------------------------
def test_month_category_bars_sorted_descending_and_sum_matches_the_report(page, server):
    port, path = server
    seed_month(path, TODAY)
    seed(path, cents=777000, cur="TRY", category="кафе", usd=None, day=TODAY)           # без курса
    seed(path, cents=1500, cur="USD", category="бар", usd=1500, day=TODAY, status="cancelled")   # удалённая не в счёт
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-cats-bars .bar-row")
    rows = bar_rows(page, "month-cats-bars")
    assert [r["cents"] for r in rows] == sorted((r["cents"] for r in rows), reverse=True)           # по убыванию
    assert [r["name"] for r in rows] == ["жильё", "продукты", "кафе", "бар", "такси"]
    assert rows[0]["width"] == "100%" and rows[0]["text"] == "≈ " + fmt(30000)
    widths = [float(r["width"].rstrip("%")) for r in rows]
    assert widths == sorted(widths, reverse=True)
    # сумма диаграммы = итог отчёта = собственный SQL
    total = sum(r["cents"] for r in rows)
    assert total == usd_sum(path, month=MONTH0) == 30000 + 9000 + 3600 + 1200 + 900
    i = usd_sum(path, month=MONTH0, direction="in")
    assert card(page) == (i, total, i - total)                                  # крупная карточка итога = SQL
    for r in rows:
        assert r["cents"] == usd_sum(path, month=MONTH0, cat=r["name"])
    # запись без курса не прячется: пометка у категории «кафе»
    assert "не вошло: нет курса для 1 записи" in page.locator('#month-cats-bars .bar-row', has_text="кафе").inner_text()


def test_category_without_any_rate_says_so_instead_of_zero(page, server):
    port, path = server
    seed(path, cents=5000, cur="BTC", category="прочее", usd=None)
    seed(path, cents=2000, cur="USD", category="кафе", usd=2000)
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-cats-bars .bar-row")
    rows = {r["name"]: r for r in bar_rows(page, "month-cats-bars")}
    assert rows["прочее"]["text"] == "нет курса" and rows["прочее"]["width"] == "0%"
    assert rows["кафе"]["text"] == "≈ " + fmt(2000)


def test_income_by_method_bars(page, server):
    port, path = server
    seed_month(path, TODAY)
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-methods-bars .bar-row")
    rows = bar_rows(page, "month-methods-bars")
    assert [r["name"] for r in rows] == ["Bybit", "Тинькофф"] and rows[0]["width"] == "100%"
    assert sum(r["cents"] for r in rows) == usd_sum(path, month=MONTH0, direction="in") == 600000


def test_empty_month_says_so_in_every_block(page, server):
    port, path = server
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-usd .note")
    assert "Записей нет" in page.locator("#month-usd").inner_text()
    assert page.locator("#month-cats-bars").inner_text().strip() == "Расходов нет"
    assert page.locator("#month-methods-bars").inner_text().strip() == "Поступлений нет"


# --- «Сегодня»: диаграмма только от трёх категорий -----------------------------------------------------------------
def test_today_chart_only_from_three_categories(page, server):
    port, path = server
    seed(path, cents=1000, cur="USD", category="кафе", usd=1000)
    seed(path, cents=2500, cur="USD", category="бар", usd=2500)
    open_app(page, port)
    tab(page, "today")
    page.wait_for_selector("#today-list li")
    assert page.locator("#today-chart").is_hidden() and page.locator("#today-bars .bar-row").count() == 0
    seed(path, cents=700, cur="USD", category="такси", usd=700)
    seed(path, cents=300, cur="USD", category="кафе", usd=300)                       # вторая запись той же категории
    tab(page, "month")
    tab(page, "today")
    page.wait_for_selector("#today-bars .bar-row")
    rows = bar_rows(page, "today-bars")
    assert [(r["name"], r["cents"]) for r in rows] == [("бар", 2500), ("кафе", 1300), ("такси", 700)]
    assert sum(r["cents"] for r in rows) == usd_sum(path, day=TODAY) == 4500
    assert "расходы " + fmt(4500) in page.locator("#today-usd").inner_text()


def test_today_chart_ignores_income_and_deleted_when_counting_categories(page, server):
    port, path = server
    seed(path, cents=1000, cur="USD", category="кафе", usd=1000)
    seed(path, cents=2500, cur="USD", category="бар", usd=2500)
    seed(path, cents=9900, cur="USD", direction="in", category="доход", method="bybit", usd=9900)
    seed(path, cents=700, cur="USD", category="такси", usd=700, status="cancelled")
    open_app(page, port)
    tab(page, "today")
    page.wait_for_selector("#today-list li")
    assert page.locator("#today-chart").is_hidden()                              # две категории расхода


# --- динамика по месяцам ------------------------------------------------------------------------------------------
def trend_data(page):
    return page.evaluate("""() => {
      const svg = document.querySelector('#month-trend svg');
      const q = (sel) => Array.from(svg.querySelectorAll(sel));
      return {
        months: q('text[data-month]').map(t => t.getAttribute('data-month')),
        nets: q('text.t-net').map(t => +t.getAttribute('data-cents')),
        ins: q('rect.t-in').map(r => +r.getAttribute('data-cents')),
        outs: q('rect.t-out').map(r => +r.getAttribute('data-cents')),
        checks: q('path.t-check').length,
        selected: q('text.t-sel').map(t => t.getAttribute('data-month')),
        hint: document.getElementById('month-trend-hint').textContent
      };
    }""")


def test_trend_matches_month_reports_and_marks_closed_months(page, server):
    port, path = server
    seed_month(path, TODAY)                                                          # текущий месяц
    seed(path, cents=777000, cur="TRY", category="кафе", usd=20000, day=MONTH1 + "-10")
    seed(path, cents=300000, cur="USD", direction="in", category="доход", method="bybit", usd=300000,
         day=MONTH1 + "-11")
    c = db.connect(path)
    c.execute("INSERT INTO closed_months(month, closed_at, source_name, source_sha, expected, rows_in, rows_out) "
              "VALUES (?,?,?,?,?,0,0)", ("2026-07", "x", "x", "x", json.dumps(
                  {"income_usd_cents": 0, "expense_usd_cents": 0, "net_usd_cents": 0, "by_category": {}})))
    c.close()
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-trend svg")
    d = trend_data(page)
    assert d["months"][0] == "2026-05" and d["months"][-1] == MONTH0 and len(d["months"]) >= 6
    assert d["checks"] == 1 and "закрыт" in d["hint"]                                   # закрыт один месяц (июль)
    assert d["selected"] == [MONTH0]                                                    # выбранный месяц выделен
    # сверка с SQL по всем месяцам: нетто, столбцы поступлений и расходов
    exp_nets, exp_ins, exp_outs = [], [], []
    for m in d["months"]:
        i, o = usd_sum(path, month=m, direction="in"), usd_sum(path, month=m)
        exp_nets.append(i - o)
        if i > 0:
            exp_ins.append(i)
        if o > 0:
            exp_outs.append(o)
    assert d["nets"] == exp_nets and d["ins"] == exp_ins and d["outs"] == exp_outs
    # и с самим отчётом месяца в приложении: прошлый месяц
    page.locator("#month-prev").click()
    page.wait_for_selector('#month-trend text.t-sel[data-month="%s"]' % MONTH1, state="attached")
    page.wait_for_selector("#month-usd .sum-row")
    k = d["months"].index(MONTH1)
    assert card(page) == (300000, usd_sum(path, month=MONTH1), 300000 - 20000)
    assert d["nets"][k] == 300000 - 20000


def test_trend_with_no_data_says_so(page, server):
    port, _ = server
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-trend-box:not([hidden]) .note")
    assert "Данных пока нет" in page.locator("#month-trend").inner_text()


def test_trend_flags_months_with_records_without_rate(page, server):
    port, path = server
    seed(path, cents=1000, cur="USD", usd=1000, day=MONTH1 + "-05")
    seed(path, cents=3000, cur="BTC", usd=None, day=MONTH1 + "-06")
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-trend svg")
    assert page.locator('#month-trend text[data-month="%s"]' % MONTH1).text_content().endswith("*")
    assert "нет курса" in page.locator("#month-trend-hint").inner_text()


# --- вёрстка: ширина iPhone, тема, CSP, текст как текст ---------------------------------------------------------
def long_data(path):
    seed_month(path, TODAY)
    for i, name in enumerate(["очень длинное название категории расходов без пробелов" + "ы" * 30,
                              "Подписки на программы и онлайн-сервисы для работы"]):
        seed(path, cents=100000 + i, cur="TRY", category=name, usd=2000 + i, day=TODAY)
    for k in (2, 3, 4, 5):
        seed(path, cents=1000, cur="USD", category="кат%d" % k, usd=1000, day=TODAY)
    for m in range(5, int(MONTH0[5:7])):
        seed(path, cents=2000000 + m * 100, cur="USD", category="жильё", usd=2000000 + m * 100,
             day="2026-%02d-05" % m)
        seed(path, cents=900000, cur="USD", direction="in", category="доход", method="bybit", usd=900000,
             day="2026-%02d-06" % m)


@pytest.mark.parametrize("theme_name,theme", [("light", LIGHT), ("dark", DARK)])
def test_no_horizontal_scroll_at_375_and_colors_come_from_the_telegram_theme(browser, server, theme_name, theme):
    from test_ui import STUB
    port, path = server
    long_data(path)
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, device_scale_factor=2, is_mobile=True,
                              has_touch=True)
    ctx.add_init_script(STUB)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    themed(pg, port, theme)
    pg.locator('#tabs [data-tab="month"]').click()
    pg.wait_for_selector("#month-trend svg")
    pg.wait_for_selector("#month-cats-bars .bar-row")
    pg.locator("#month-cats-bars .bar-row").first.click()                      # раскрытые детали и валюты тоже влезают
    pg.locator("#month-cur-toggle").click()
    pg.wait_for_selector("#month-totals .card")
    for sel in ("body", "#month-cats-bars", "#month-trend", "#month-totals", "#month-usd"):
        assert pg.evaluate("document.querySelector('%s').scrollWidth <= document.documentElement.clientWidth" % sel)
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    # длинные подписи переносятся внутри строки и не вылезают за экран
    assert pg.evaluate("""() => Array.from(document.querySelectorAll('.bar-row')).every(
        r => r.getBoundingClientRect().right <= window.innerWidth)""")
    rgb = lambda h: "rgb(%d, %d, %d)" % tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
    css = pg.evaluate("""() => ({
      out: getComputedStyle(document.querySelector('#month-cats-bars .bar-fill')).backgroundColor,
      inc: getComputedStyle(document.querySelector('#month-methods-bars .bar-fill')).backgroundColor,
      tin: getComputedStyle(document.querySelector('#month-trend rect.t-in')).fill,
      tout: getComputedStyle(document.querySelector('#month-trend rect.t-out')).fill,
      text: getComputedStyle(document.querySelector('#month-trend text')).fill,
      body: getComputedStyle(document.body).backgroundColor })""")
    assert css["out"] == css["tout"] == rgb(theme["destructive_text_color"])
    assert css["inc"] == css["tin"] == rgb(theme["accent_text_color"])
    assert css["text"] == rgb(theme["text_color"]) and css["body"] == rgb(theme["bg_color"])
    pg.locator('#tabs [data-tab="today"]').click()
    pg.wait_for_selector("#today-bars .bar-row")
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not [e for e in errors if "Content Security Policy" in e or "Refused" in e], errors
    ctx.close()


def test_chart_texts_are_text_not_html(page, server):
    port, path = server
    seed(path, cents=1000, cur="USD", category="<img src=x onerror=__p=7>", usd=1000)
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-cats-bars .bar-row")
    assert page.evaluate("window.__p") is None and page.locator("#month-cats-bars img").count() == 0
    assert "<img" in page.locator("#month-cats-bars .bar-name").inner_text()


def test_charts_add_no_buttons(page, server):
    port, path = server
    long_data(path)
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-trend svg")
    # только стрелки месяца и одно раскрытие «По валютам»; полосы раскрываются нажатием на саму полосу, не кнопкой
    assert page.locator("#view-month button:visible").all_inner_texts() == ["◀", "▶", "По валютам ▾"]
    tab(page, "today")
    page.wait_for_selector("#today-bars .bar-row")
    assert page.locator("#today-chart button, #today-chart a, #today-chart input").count() == 0


# --- страница месяца: порядок блоков, детали по нажатию, свёрнутые курсы и валюты ---------------------------------
def test_month_page_order_summary_trend_categories_methods_currencies(page, server):
    port, path = server
    seed_month(path, TODAY)
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-trend svg")
    page.wait_for_selector("#month-cats-bars .bar-row")
    ys = page.evaluate("""() => ['#month-usd', '#month-trend-box', '#month-cats-chart', '#month-methods-chart',
                                 '#month-cur-box'].map(s => document.querySelector(s).getBoundingClientRect().top)""")
    assert ys == sorted(ys) and len(set(ys)) == 5
    assert page.locator("#month-cats").count() == 0 and page.locator("#month-methods").count() == 0   # дублей нет
    assert page.locator("#month-totals").is_hidden()                                           # валюты свёрнуты
    assert page.locator("#month-cur-toggle").inner_text() == "По валютам ▾"


def test_card_shows_in_out_net_big_and_closed_note_is_small_with_rates_collapsed(page, server):
    port, path = server
    seed_month(path, TODAY)
    c = db.connect(path)
    c.execute("INSERT INTO closed_months(month, closed_at, rates, source_name, source_sha, expected, rows_in, rows_out) "
              "VALUES (?,?,?,?,?,?,0,0)", (MONTH1, "x", json.dumps({"TRY_per_USD": 41.5, "RUB_per_USD": 80}), "x", "x",
                                           json.dumps({"income_usd_cents": 0, "expense_usd_cents": 0,
                                                       "net_usd_cents": 0, "by_category": {}})))
    c.close()
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-usd .sum-row")
    assert page.locator("#month-usd .sum-row span:first-child").all_inner_texts() == ["Поступления", "Расходы", "Нетто"]
    assert page.locator("#month-closed").is_hidden()                                       # октябрь не закрыт
    big = page.evaluate("parseFloat(getComputedStyle(document.querySelector('#month-usd .sum-row.net .amt')).fontSize)")
    assert big >= 26
    page.locator("#month-prev").click()
    page.wait_for_selector("#month-closed:not([hidden])")
    assert "Месяц закрыт. Итоги совпадают с закрытием." in page.locator("#month-closed").inner_text()
    assert page.locator("#month-rates").is_hidden() and page.locator("#month-rates-toggle").inner_text() == "Курсы ▾"
    page.locator("#month-rates-toggle").click()
    assert "TRY 41,5" in page.locator("#month-rates").inner_text() and "RUB 80" in page.locator("#month-rates").inner_text()
    assert page.locator("#month-rates-toggle").inner_text() == "Курсы ▴"
    page.locator("#month-rates-toggle").click()
    assert page.locator("#month-rates").is_hidden()


def test_closed_month_mismatch_banner_stays_loud(page, server):
    port, path = server
    seed(path, cents=1000, cur="USD", usd=1000, day=MONTH1 + "-05")
    c = db.connect(path)
    c.execute("INSERT INTO closed_months(month, closed_at, source_name, source_sha, expected, rows_in, rows_out) "
              "VALUES (?,?,?,?,?,0,0)", (MONTH1, "x", "x", "x", json.dumps(
                  {"income_usd_cents": 0, "expense_usd_cents": 5, "net_usd_cents": -5, "by_category": {}})))
    c.close()
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-usd > *")
    page.locator("#month-prev").click()
    page.wait_for_selector("#month-closed.bad")
    assert "не совпадают" in page.locator("#month-closed").inner_text()


def test_tap_on_bar_opens_details_with_count_and_currency_sums_and_closes(page, server):
    port, path = server
    seed(path, cents=100000, cur="TRY", category="кафе", usd=2500)
    seed(path, cents=50000, cur="TRY", category="кафе", usd=1250)
    seed(path, cents=1000, cur="USD", category="кафе", usd=1000)
    seed(path, cents=700, cur="USD", category="такси", usd=700)
    seed(path, cents=2000, cur="USD", direction="in", category="доход", method="bybit", usd=2000)
    seed(path, cents=300, cur="USDT", direction="in", category="доход", method="bybit", usd=300)
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-cats-bars .bar-row")
    row = page.locator("#month-cats-bars .bar-row", has_text="кафе")
    assert row.locator(".bar-detail").count() == 0
    row.click()
    assert row.locator(".bar-detail").inner_text() == "3 зап. · %s TRY, %s USD" % (fmt(150000), fmt(1000))
    assert page.locator("#month-cats-bars .bar-detail").count() == 1                      # детали только у нажатой
    assert row.get_attribute("aria-expanded") == "true"
    row.click()
    assert row.locator(".bar-detail").count() == 0 and row.get_attribute("aria-expanded") == "false"
    page.locator("#month-methods-bars .bar-row").first.click()
    assert page.locator("#month-methods-bars .bar-detail").inner_text() == \
        "2 зап. · %s USD, %s USDT" % (fmt(2000), fmt(300))


def test_currency_blocks_are_collapsed_at_the_bottom_and_open_on_tap(page, server):
    port, path = server
    seed(path, cents=1000, cur="USD", usd=1000)
    seed(path, cents=40000, cur="TRY", usd=1000)
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-usd .sum-row")
    assert page.locator("#month-totals").is_hidden()
    page.locator("#month-cur-toggle").click()
    titles = {c.query_selector(".card-title").inner_text() for c in page.query_selector_all("#month-totals .card")}
    assert titles == {"USD", "TRY"}
    assert page.locator("#month-cur-toggle").inner_text() == "По валютам ▴"
    page.locator("#month-cur-toggle").click()
    assert page.locator("#month-totals").is_hidden()
