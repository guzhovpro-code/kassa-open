"""Обмен валют в браузере (headless Chromium, как test_ui8): режим «Обмен» формы, живой расчёт курса, черновик из чата,
вкладка «Обмены» (список с курсом, удаление, возврат, правка), строка «Обменов: N» в отчётах. Запись идёт нативной
кнопкой «Записать» (имитация main_button_pressed) и только с подписанным initData."""
import pytest

from kassa_api import cards, db
import urllib.parse

from helpers import OWNER, make_init_data
from test_ui import ONLINE, PRESS, PRIV, TOKEN, browser, page, server  # noqa: F401

pytest.importorskip("playwright.sync_api")


def open_url(page, port, query=""):
    init = make_init_data(PRIV, user_id=OWNER)
    hash_ = urllib.parse.urlencode({"tgWebAppData": init, "tgWebAppVersion": "8.0", "tgWebAppPlatform": "ios"})
    page.goto("http://127.0.0.1:%d/%s/%s#%s" % (port, TOKEN, query, hash_))
    page.wait_for_selector("#form:not([hidden]), #stub:not([hidden]), #view-inbox:not([hidden])")


def q(path, sql, args=()):
    c = db.connect(path, readonly=True)
    try:
        return [tuple(r) for r in c.execute(sql, args)]
    finally:
        c.close()
pytestmark = pytest.mark.skipif(not ONLINE, reason="telegram.org недоступен")

EXAMPLE = "поменял 201 230 рублей на 75 000 лир и 700 долларов, комиссия 758,65"


def to_exchange_mode(page):
    page.locator("#direction button", has_text="Обмен").click()
    page.wait_for_selector("#xform:not([hidden])")


def fill_example(page, method="Тинькофф"):
    page.fill("#x-give-amount", "201230")
    page.select_option("#x-give-cur", "RUB")
    if method:
        page.select_option("#x-give-method", label=method)
    leg = page.locator("#x-legs .xleg").nth(0)
    leg.locator(".x-leg-amount").fill("75000")
    leg.locator(".x-leg-cur").select_option("TRY")
    page.locator("#x-add-leg").click()
    leg2 = page.locator("#x-legs .xleg").nth(1)
    leg2.locator(".x-leg-amount").fill("700")
    leg2.locator(".x-leg-cur").select_option("USD")
    page.fill("#x-fee-amount", "758,65")


def count(path, table="exchanges"):
    return q(path, "SELECT COUNT(*) FROM %s" % table)[0][0]


def test_exchange_mode_replaces_the_record_fields_and_shows_live_rate(page, server):
    port, path = server
    open_url(page, port)
    assert page.locator("#rec-fields").is_visible() and not page.locator("#xform").is_visible()
    to_exchange_mode(page)
    assert page.locator("#xform").is_visible() and not page.locator("#rec-fields").is_visible()
    assert page.locator("#direction button.on").inner_text() == "Обмен"
    page.fill("#x-give-amount", "5000")
    page.locator("#x-legs .xleg .x-leg-amount").fill("100")
    page.locator("#x-legs .xleg .x-leg-cur").select_option("USD")
    page.wait_for_function("() => document.getElementById('x-rate').textContent.includes('1 USD = 50 RUB')")
    assert page.locator("#x-legs .xleg .x-leg-part").count() == 0          # у одной валюты доли нет
    page.locator("#x-add-leg").click()
    assert page.locator("#x-legs .xleg .x-leg-part").count() == 2
    assert "оценит" in page.locator("#x-rate").inner_text()               # доли не указаны: курс оценит касса
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")   # шесть вкладок влезают в 390 px
    page.locator("#direction button", has_text="Расход").click()          # назад: поля записи на месте
    assert page.locator("#rec-fields").is_visible() and not page.locator("#xform").is_visible()
    assert page.csp_errors == []


def test_exchange_from_the_form_is_written_with_signature_and_listed(page, server):
    port, path = server
    before = count(path)
    open_url(page, port)
    to_exchange_mode(page)
    fill_example(page)
    page.evaluate(PRESS)
    page.wait_for_selector("#ok:not([hidden])")
    assert "Обмен записан №" in page.locator("#ok-text").inner_text()
    assert count(path) == before + 1 and count(path, "exchange_legs") >= 2
    row = q(path, "SELECT give_cents, give_currency, give_method, via FROM exchanges ORDER BY id DESC LIMIT 1")[0]
    assert row == (20123000, "RUB", "tinkoff", "webapp")
    assert q(path, "SELECT amount_cents, category, method FROM transactions WHERE exchange_id IS NOT NULL "
                   "ORDER BY id DESC LIMIT 1") == [(75865, "Комиссия обмена", "tinkoff")]
    assert page.locator("#x-give-amount").input_value() == ""              # форма очищена
    # «↩ Отменить» отменяет именно обмен
    page.locator("#undo").click()
    page.wait_for_function("() => document.getElementById('ok-text').textContent.includes('Обмен №')")
    assert q(path, "SELECT status FROM exchanges ORDER BY id DESC LIMIT 1") == [("cancelled",)]
    assert q(path, "SELECT status FROM transactions WHERE exchange_id IS NOT NULL ORDER BY id DESC LIMIT 1") == [("cancelled",)]
    assert page.csp_errors == []


def test_bad_exchange_input_is_refused_in_the_form_without_a_request(page, server):
    port, path = server
    before = count(path)
    open_url(page, port)
    to_exchange_mode(page)
    page.fill("#x-give-amount", "5000")
    page.select_option("#x-give-cur", "USD")
    page.locator("#x-legs .xleg .x-leg-amount").fill("100")
    page.locator("#x-legs .xleg .x-leg-cur").select_option("USD")
    page.evaluate(PRESS)
    page.wait_for_function("() => document.getElementById('err').textContent.includes('одну валюту')")
    page.fill("#x-give-amount", "")
    page.evaluate(PRESS)
    page.wait_for_function("() => document.getElementById('err').textContent.includes('отданного')")
    page.fill("#x-give-amount", "5000")
    page.select_option("#x-give-cur", "RUB")
    page.fill("#x-fee-amount", "10")
    page.wait_for_selector("#x-fee-hint:not([hidden])")                      # комиссия без способа отдачи
    page.evaluate(PRESS)
    page.wait_for_function("() => document.getElementById('err').textContent.includes('способ')")
    assert count(path) == before


def test_draft_from_chat_opens_the_exchange_form_and_saving_closes_the_card(page, server):
    port, path = server
    c = db.connect(path)
    cid = cards.create_from_text(c, 1001, 1001, "phrase", EXAMPLE, "uie-1")["card_id"]
    c.close()
    before = count(path)
    open_url(page, port, "?draft=%d" % cid)
    page.wait_for_function("() => document.getElementById('x-give-amount').value === '201230'")
    assert page.locator("#xform").is_visible() and page.locator("#x-banner").is_visible()
    assert page.locator("#x-give-cur").input_value() == "RUB"
    assert page.locator("#x-legs .xleg").count() == 2
    assert page.locator("#x-legs .xleg").nth(0).locator(".x-leg-cur").input_value() == "TRY"
    assert page.locator("#x-fee-amount").input_value() == "758.65"
    page.select_option("#x-give-method", label="Тинькофф")
    page.evaluate(PRESS)
    page.wait_for_selector("#ok:not([hidden])")
    assert q(path, "SELECT status FROM cards WHERE id=?", (cid,)) == [("confirmed",)]
    assert count(path) == before + 1
    assert q(path, "SELECT idem_key FROM exchanges ORDER BY id DESC LIMIT 1") == [("card:%d" % cid,)]
    assert page.csp_errors == []


def test_inbox_shows_an_exchange_draft_and_opens_it(page, server):
    port, path = server
    c = db.connect(path)
    cid = cards.create_from_text(c, 1001, 1001, "phrase", "обменял 100 долларов на 3500 лир", "uie-2")["card_id"]
    c.close()
    open_url(page, port, "?tab=inbox")
    page.wait_for_selector("#view-inbox:not([hidden]) #inbox-list .rec")
    text = page.locator("#inbox-list").inner_text()
    assert "Обмен: отдал 100 $ → получил 3 500 ₺" in text
    page.locator("#inbox-list .rec", has_text="3 500").locator("button", has_text="Проверить").click()
    page.wait_for_function("() => document.getElementById('x-give-amount').value === '100'")
    assert page.locator("#direction button.on").inner_text() == "Обмен"
    assert q(path, "SELECT status FROM cards WHERE id=?", (cid,)) == [("pending",)]


def test_exchanges_tab_lists_rates_and_cancel_restore_edit_work(page, server):
    port, path = server
    open_url(page, port)
    to_exchange_mode(page)
    fill_example(page)
    page.fill("#x-note", "обменник")
    page.evaluate(PRESS)
    page.wait_for_selector("#ok:not([hidden])")
    ex_id = q(path, "SELECT id FROM exchanges ORDER BY id DESC LIMIT 1")[0][0]

    page.locator("#tabs button[data-tab=exchanges]").click()
    page.wait_for_selector("#view-exchanges:not([hidden]) #exchanges-list .rec:not(.empty)")
    row = page.locator("#exchanges-list .rec", has_text="обменник").first
    text = row.inner_text().replace(" ", " ")           # fmtCents ставит узкий неразрывный пробел
    assert "201 230 RUB → 75 000 TRY, 700 USD" in text and "комиссия 758,65 RUB" in text
    assert "курс не посчитан" in text or "≈ 1 TRY =" in text                 # курсов в тесте нет или оценка
    assert "отдал: Тинькофф" in text and "обменник" in text

    row.click()                                                              # удалить
    row.locator("button", has_text="Удалить").click()
    row.locator("button", has_text="Да, удалить").click()
    page.wait_for_function("() => document.getElementById('exchanges-msg').textContent.includes('удалён')")
    assert q(path, "SELECT status FROM exchanges WHERE id=?", (ex_id,)) == [("cancelled",)]
    row = page.locator("#exchanges-list .rec.gone", has_text="обменник").first
    row.click()                                                              # вернуть
    row.locator("button", has_text="Вернуть").click()
    page.wait_for_function("() => document.getElementById('exchanges-msg').textContent.includes('возвращён')")
    assert q(path, "SELECT status FROM exchanges WHERE id=?", (ex_id,)) == [("active",)]

    row = page.locator("#exchanges-list .rec", has_text="обменник").first
    row.click()                                                              # изменить
    row.locator("button", has_text="Изменить").click()
    page.wait_for_selector("#xform:not([hidden])")
    assert "Правка обмена №%d" % ex_id in page.locator("#x-banner").inner_text()
    assert page.locator("#x-give-amount").input_value() == "201230" and page.locator("#x-fee-amount").input_value() == "758,65"
    page.fill("#x-note", "поправил")
    page.fill("#x-fee-amount", "800")
    page.evaluate(PRESS)
    page.wait_for_function("() => document.getElementById('exchanges-msg').textContent.includes('изменён')")
    assert q(path, "SELECT note FROM exchanges WHERE id=?", (ex_id,)) == [("поправил",)]
    assert q(path, "SELECT amount_cents FROM transactions WHERE exchange_id=?", (ex_id,)) == [(80000,)]

    page.locator("#tabs button[data-tab=today]").click()                    # отчёты: только число, без сумм
    page.wait_for_function("() => document.getElementById('today-ex').textContent.includes('Обменов:')")
    assert "Обменов: " in page.locator("#today-ex").inner_text()
    page.locator("#tabs button[data-tab=recent]").click()                   # комиссия в «Последних» - без кнопок правки
    page.wait_for_selector("#recent-list .rec")
    fee = page.locator("#recent-list .rec", has_text="комиссия обмена №%d" % ex_id).first
    fee.click()
    assert fee.locator("button").count() == 0
    assert page.csp_errors == []
