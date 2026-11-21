"""Этап 4 в браузере (headless Chromium, 390x844): категория сама, меньше кнопок, валюты, правка и удаление
из «Сегодня». Каждый тест получает свою чистую базу (фикстуры из test_ui3)."""
import re
from datetime import date, timedelta

import pytest

from helpers import OWNER
from kassa_api import db
from test_ui import ONLINE, browser, main_button, pick_method, press  # noqa: F401
from test_ui3 import (TODAY, btn, fetch_row, fmt, main_button_visible, open_app, open_search, page, result_codes,  # noqa: F401
                      seed, server, tab)

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.skipif(not ONLINE, reason="telegram.org недоступен")

VISIBLE_BUTTONS = """() => Array.from(document.querySelectorAll('button')).filter(e => {
  const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; }).map(e => e.textContent.trim())"""


def last_row(path):
    c = db.connect(path, readonly=True)
    r = c.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
    c.close()
    return dict(r)


def total(path):
    c = db.connect(path, readonly=True)
    n = c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    c.close()
    return n


def audit_rows(path, action):
    c = db.connect(path, readonly=True)
    rows = [dict(r) for r in c.execute("SELECT * FROM audit WHERE action=? ORDER BY id", (action,))]
    c.close()
    return rows


# --- категория определяется сама --------------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("продукты", "продукты"), ("еда в кафе", "кафе"), ("оплата ЖКХ", "жильё"), ("подарки", "подарки"),
    ("такси", "транспорт"), ("абракадабра", "прочее"),
])
def test_category_caption_follows_text_and_is_saved(page, server, text, expected):
    port, path = server
    open_app(page, port)
    page.fill("#amount", "100")
    page.fill("#note", text)
    expect(page.locator("#cat-select")).to_have_value(expected)
    assert page.locator("#form .catline").inner_text().startswith("Категория:")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    assert expected in page.locator("#ok-text").inner_text()
    r = last_row(path)
    assert (r["category"], r["note"], r["amount_cents"]) == (expected, text, 10000)


def test_empty_text_is_other_and_does_not_block_saving(page, server):
    port, path = server
    open_app(page, port)
    expect(page.locator("#cat-select")).to_have_value("прочее")
    page.fill("#amount", "55")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    r = last_row(path)
    assert (r["category"], r["note"]) == ("прочее", None)


def test_manual_category_change_wins_over_text(page, server):
    port, path = server
    open_app(page, port)
    page.fill("#amount", "100")
    page.fill("#note", "такси")
    expect(page.locator("#cat-select")).to_have_value("транспорт")
    page.select_option("#cat-select", "бар")
    page.fill("#note", "такси домой")
    page.wait_for_timeout(600)                                    # дольше задержки подсказки
    expect(page.locator("#cat-select")).to_have_value("бар")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    r = last_row(path)
    assert (r["category"], r["note"]) == ("бар", "такси домой")


def test_after_saving_the_caption_returns_to_default(page, server):
    port, _ = server
    open_app(page, port)
    page.fill("#amount", "100")
    page.fill("#note", "такси")
    expect(page.locator("#cat-select")).to_have_value("транспорт")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    expect(page.locator("#cat-select")).to_have_value("прочее")
    assert page.locator("#note").input_value() == ""


def test_category_is_a_dropdown_not_a_grid_and_custom_one_works(page, server):
    port, path = server
    open_app(page, port)
    assert page.locator("#cat").count() == 0                       # сетки кнопок категорий больше нет
    options = page.locator("#cat-select option").all_inner_texts()
    assert options[:3] == ["продукты", "кафе", "бар"] and options[-2:] == ["прочее", "Другая…"]
    assert page.locator("#cat-other").is_hidden()
    page.select_option("#cat-select", "__other")
    page.locator("#cat-other").fill("Кипр платёж")
    page.fill("#amount", "10")
    page.fill("#note", "такси")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    assert last_row(path)["category"] == "Кипр платёж"


def test_income_caption_defaults_and_return(page, server):
    port, path = server
    open_app(page, port)
    btn(page, "direction", "Поступление").click()
    expect(page.locator("#cat-select")).to_have_value("доход")
    page.fill("#note", "возврат долга")
    expect(page.locator("#cat-select")).to_have_value("возврат")
    page.fill("#amount", "10")
    pick_method(page, "Наличные")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    assert last_row(path)["category"] == "возврат"


def test_text_is_not_rendered_as_html_in_caption_area(page, server):
    port, _ = server
    open_app(page, port)
    page.fill("#note", "<img src=x onerror=__p=7>")
    page.wait_for_timeout(600)
    assert page.evaluate("window.__p") is None
    assert page.locator("#form img").count() == 0


# --- дата и способ на главном экране ----------------------------------------------------------------------------
DATE_BUTTONS = ["Сегодня", "Вчера", "Другая дата"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]


def dayword(iso):
    d = date.fromisoformat(iso)
    return "%d %s" % (d.day, MONTHS_GEN[d.month - 1])


def test_expense_screen_shows_date_and_method_without_expanding(page, server):
    port, _ = server
    open_app(page, port, method=None)
    labels = page.evaluate(VISIBLE_BUTTONS)
    assert labels == ["Запись", "Входящие", "Сегодня", "Месяц", "Период", "Последние", "Обмены", "Расход", "Поступление", "Обмен", "🎙 Надиктовать",
                      "TRY", "USDT", "USD", "RUB", "Ещё ▾"] + DATE_BUTTONS + \
        ["Наличные", "Redot", "Тинькофф", "Ещё ▾"]
    assert page.locator("#date").is_visible() and page.locator("#method").is_visible()     # ничего не раскрывать
    assert page.locator("#date button.on").inner_text() == "Сегодня"                      # по умолчанию сегодня
    assert page.locator("#date-label").inner_text() == "Дата: " + dayword(TODAY)
    assert page.locator("#method button.on").count() == 0                               # способ ещё не выбран
    assert page.locator("#method-hint").is_visible()
    assert main_button(page)["is_active"] is False
    assert page.locator("#amount").is_visible() and page.locator("#note").is_visible()


def test_dates_are_exactly_three_and_other_date_opens_the_picker(page, server):
    port, path = server
    open_app(page, port, method=None)
    assert page.locator("#date button").all_inner_texts() == ["Сегодня", "Вчера", "Другая дата"]
    assert page.locator("#date-pick").is_hidden()
    btn(page, "date", "Другая дата").click()
    assert page.locator("#date-pick").is_visible() and page.locator("#date button.on").inner_text() == "Другая дата"
    page.fill("#date-pick", (date.fromisoformat(TODAY) - timedelta(days=5)).isoformat())
    page.locator("#date-pick").dispatch_event("change")
    btn(page, "method", "Redot").click()
    page.fill("#amount", "9")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    assert last_row(path)["occurred_on"] == (date.fromisoformat(TODAY) - timedelta(days=5)).isoformat()


def test_three_method_chips_plus_more_and_the_rest_under_more(page, server):
    port, _ = server
    open_app(page, port, method=None)
    assert page.locator("#method button").all_inner_texts() == ["Наличные", "Redot", "Тинькофф", "Ещё ▾"]
    btn(page, "method", "Ещё ▾").click()
    opts = page.locator("#method-select option").all_inner_texts()
    assert {"Kaspi", "Bybit", "Сбер", "Другой…"} <= set(opts)                     # остальные и «Другой» под «Ещё»
    page.select_option("#method-select", "kaspi")
    assert page.locator("#method button.on").inner_text() == "Kaspi"            # выбранный из «Ещё» остаётся видимым
    assert main_button(page)["is_active"] is True


def test_record_button_is_inactive_until_a_method_is_chosen_for_expense(page, server):
    port, path = server
    open_app(page, port, method=None)
    before = total(path)
    page.fill("#amount", "12")
    page.fill("#note", "кофе")
    assert main_button(page)["is_active"] is False
    press(page)                          # нажатие неактивной кнопки библиотека Telegram не пропускает: записи нет
    page.wait_for_timeout(400)
    assert page.locator("#ok").is_hidden() and total(path) == before
    btn(page, "method", "Redot").click()
    assert main_button(page)["is_active"] is True and page.locator("#method-hint").is_hidden()
    assert page.locator("#method button.on").inner_text() == "Redot"
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    assert last_row(path)["method"] == "redot" and total(path) == before + 1


def test_date_and_method_change_with_one_tap_and_method_is_remembered(page, server):
    port, path = server
    open_app(page, port, method=None)
    btn(page, "date", "Вчера").click()
    yesterday = (date.fromisoformat(TODAY) - timedelta(days=1)).isoformat()
    assert page.locator("#date-label").inner_text() == "Дата: " + dayword(yesterday)
    btn(page, "method", "Redot").click()
    page.fill("#amount", "12")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    r = last_row(path)
    assert r["method"] == "redot" and r["occurred_on"] == yesterday
    assert page.locator("#date button.on").inner_text() == "Сегодня"                      # дата вернулась на сегодня
    assert page.locator("#method button.on").inner_text() == "Redot"                      # способ остался
    # новое открытие формы: последний выбранный способ подставляет сервер
    open_app(page, port, method=None)
    assert page.locator("#method button.on").inner_text() == "Redot"
    assert main_button(page)["is_active"] is True and page.locator("#method-hint").is_hidden()


def test_more_opens_full_method_list_with_other_for_expense_too(page, server):
    port, path = server
    open_app(page, port, method=None)
    btn(page, "method", "Ещё ▾").click()
    assert page.locator("#method-select option").all_inner_texts() == \
        ["Выберите способ", "Наличные", "Redot", "Тинькофф", "Kaspi", "Bybit", "Сбер", "Другой…"]
    page.select_option("#method-select", "sber")                                        # из списка: появляется чипсом
    assert page.locator("#method button.on").inner_text() == "Сбер" and page.locator("#method-panel").is_hidden()
    btn(page, "method", "Ещё ▾").click()
    page.select_option("#method-select", "__other")
    page.fill("#method-other", "Карта друга")
    page.fill("#amount", "7")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    c = db.connect(path, readonly=True)
    m = c.execute("SELECT * FROM methods WHERE title='Карта друга'").fetchone()
    assert m is not None and m["scope"] == "out" and last_row(path)["method"] == m["code"]
    c.close()
    page.wait_for_selector("#method button.on")                                       # CSP forbids eval, so no wait_for_function
    assert page.locator("#method button.on").inner_text() == "Карта друга"


def test_method_is_remembered_per_direction(page, server):
    port, _ = server
    open_app(page, port, method=None)
    btn(page, "method", "Redot").click()
    btn(page, "direction", "Поступление").click()
    assert page.locator("#method button.on").count() == 0 and main_button(page)["is_active"] is False
    btn(page, "method", "Тинькофф").click()
    btn(page, "direction", "Расход").click()
    assert page.locator("#method button.on").inner_text() == "Redot"
    btn(page, "direction", "Поступление").click()
    assert page.locator("#method button.on").inner_text() == "Тинькофф"


def test_closed_month_date_blocks_the_record_button(page, server):
    port, path = server
    c = db.connect(path)
    month = (date.fromisoformat(TODAY).replace(day=1) - timedelta(days=1)).isoformat()[:7]
    c.execute("INSERT INTO closed_months(month, closed_at, source_name, source_sha, expected, rows_in, rows_out) "
              "VALUES (?,?,?,?,?,0,0)", (month, "x", "x", "x", "{}"))
    c.close()
    open_app(page, port)
    page.fill("#amount", "5")
    assert main_button(page)["is_active"] is True
    btn(page, "date", "Другая дата").click()
    page.fill("#date-pick", month + "-15")
    page.locator("#date-pick").dispatch_event("change")
    assert page.locator("#date-hint").is_visible() and main_button(page)["is_active"] is False
    n = total(path)
    press(page)
    page.wait_for_timeout(400)
    assert page.locator("#ok").is_hidden() and total(path) == n
    assert "закрыт" in page.locator("#date-hint").inner_text()


def test_income_without_method_shows_chips_and_hint(page, server):
    port, _ = server
    open_app(page, port)
    btn(page, "direction", "Поступление").click()
    assert page.locator("#date").is_visible() and page.locator("#method").is_visible()
    labels = page.evaluate(VISIBLE_BUTTONS)
    assert labels == ["Запись", "Входящие", "Сегодня", "Месяц", "Период", "Последние", "Обмены", "Расход", "Поступление", "Обмен", "🎙 Надиктовать",
                      "TRY", "USDT", "USD", "RUB", "Ещё ▾"] + DATE_BUTTONS + ["Bybit", "Тинькофф", "Сбер", "Ещё ▾"] + ["B2B", "B2C"]
    assert page.locator("#method-hint").is_visible()


def test_main_path_amount_currency_text_record(page, server):
    """Главный путь расхода: сумма -> валюта -> «на что» -> Записать (способ запомнен, дата сегодня)."""
    port, path = server
    open_app(page, port)
    page.fill("#amount", "1 250,5")
    btn(page, "cur", "USDT").click()
    page.fill("#note", "еда в кафе")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    r = last_row(path)
    assert (r["amount_cents"], r["currency"], r["category"], r["note"], r["method"]) == \
        (125050, "USDT", "кафе", "еда в кафе", "cash")
    assert r["occurred_on"] == TODAY


# --- валюты -----------------------------------------------------------------------------------------------------
def test_eur_is_under_more_not_among_main(page, server):
    port, path = server
    open_app(page, port)
    assert "EUR" not in page.locator("#cur button").all_inner_texts()
    open_search(page)
    page.fill("#cur-search", "")
    assert result_codes(page)[:5] == ["TRY", "USDT", "USD", "RUB", "EUR"]
    page.locator('#cur-results [data-code="EUR"]').click()
    assert page.locator("#cur button.on").inner_text() == "EUR"
    page.fill("#amount", "7")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    assert last_row(path)["currency"] == "EUR"


# --- «Сегодня»: действия с записью -------------------------------------------------------------------------------
def open_today(page, port):
    open_app(page, port)
    tab(page, "today")
    page.wait_for_selector("#today-list li")


def test_today_rows_have_no_buttons_until_tapped(page, server):
    port, path = server
    seed(path, cents=10000, cur="TRY", usd=300)
    open_today(page, port)
    assert page.locator("#today-list button").count() == 0
    page.locator("#today-list li").first.click()
    assert [b.strip() for b in page.locator("#today-list li").first.locator("button").all_inner_texts()] == \
        ["Изменить", "Удалить"]
    page.locator("#today-list li").first.locator(".rec-top").click()               # повторное нажатие закрывает
    assert page.locator("#today-list button").count() == 0


def test_today_edit_amount_and_currency_with_audit_and_totals(page, server):
    port, path = server
    rid = seed(path, cents=10000, cur="TRY", usd=300, category="кафе", note="кофе")
    seed(path, cents=5000, cur="TRY", usd=150, category="бар")
    open_today(page, port)
    li = page.locator("#today-list li", has_text="кофе")
    li.click()
    li.locator("button", has_text="Изменить").click()
    ed = li.locator(".editor")
    assert ed.locator("input").first.input_value() == "100"
    assert "Сумму и валюту менять нельзя" not in page.inner_text("body")
    ed.locator("input").first.fill("250,5")
    ed.locator("select").nth(0).select_option("USD")
    ed.locator("button", has_text="Сохранить").click()
    expect(page.locator("#today-msg")).to_contain_text("изменена")
    r = fetch_row(path, rid)
    assert (r["amount_cents"], r["currency"], r["usd_cents"], r["category"]) == (25050, "USD", 25050, "кафе")   # этап 5: USD всегда 1:1
    expect(page.locator(".card-title", has_text="USD")).to_have_count(1)   # дождаться перерисовки итогов
    cards = {c.query_selector(".card-title").inner_text(): c.inner_text()
             for c in page.query_selector_all("#today-totals .card")}
    assert set(cards) == {"TRY", "USD"}
    assert fmt(25050) in cards["USD"] and fmt(5000) in cards["TRY"] and fmt(10000) not in cards["TRY"]
    (a,) = audit_rows(path, "update")
    assert (a["actor_id"], a["entity_id"]) == (str(OWNER), str(rid))
    import json
    d = json.loads(a["detail"])["changed"]
    assert d["amount_cents"] == [10000, 25050] and d["currency"] == ["TRY", "USD"] and d["usd_cents"] == [300, 25050]


def test_today_delete_then_undo_from_message(page, server):
    port, path = server
    rid = seed(path, cents=22200, cur="TRY", category="бар")
    open_today(page, port)
    li = page.locator("#today-list li").first
    li.click()
    li.locator("button", has_text="Удалить").click()
    li.locator("button", has_text="Да, удалить").click()
    expect(page.locator("#today-msg")).to_contain_text("удалена")
    expect(page.locator("#today-list li")).to_have_count(0)                  # из «Сегодня» исчезла
    assert fetch_row(path, rid)["status"] == "cancelled"
    btn(page, "today-msg", "↩ Вернуть").click()
    expect(page.locator("#today-list li")).to_have_count(1)
    row = fetch_row(path, rid)
    assert row["status"] == "active" and row["cancelled_at"] is None and row["amount_cents"] == 22200


def test_editor_rejects_bad_amount_and_changes_nothing(page, server):
    port, path = server
    rid = seed(path, cents=10000, cur="TRY")
    open_today(page, port)
    li = page.locator("#today-list li").first
    li.click()
    li.locator("button", has_text="Изменить").click()
    ed = li.locator(".editor")
    for bad in ("abc", "0", "1,234"):
        ed.locator("input").first.fill(bad)
        ed.locator("button", has_text="Сохранить").click()
        expect(ed.locator(".err")).to_be_visible()
        assert fetch_row(path, rid)["amount_cents"] == 10000
    assert audit_rows(path, "update") == []


def test_editor_without_changes_just_closes(page, server):
    port, path = server
    seed(path, cents=10000, cur="TRY")
    open_today(page, port)
    li = page.locator("#today-list li").first
    li.click()
    li.locator("button", has_text="Изменить").click()
    li.locator(".editor button", has_text="Сохранить").click()
    expect(page.locator("#today-list .editor")).to_have_count(0)
    assert audit_rows(path, "update") == []


def test_editor_cancel_keeps_record_untouched(page, server):
    port, path = server
    rid = seed(path, cents=10000, cur="TRY")
    open_today(page, port)
    li = page.locator("#today-list li").first
    li.click()
    li.locator("button", has_text="Изменить").click()
    li.locator(".editor input").first.fill("999")
    li.locator(".editor button", has_text="Отмена").click()
    assert li.locator(".editor").count() == 0 and fetch_row(path, rid)["amount_cents"] == 10000


def test_editor_outlier_needs_second_press(page, server):
    port, path = server
    for _ in range(10):
        seed(path, cents=10000, cur="KZT")
    rid = seed(path, cents=10000, cur="KZT", note="цель")
    open_today(page, port)
    li = page.locator("#today-list li", has_text="цель")
    li.click()
    li.locator("button", has_text="Изменить").click()
    ed = li.locator(".editor")
    ed.locator("input").first.fill("99999")
    ed.locator("button", has_text="Сохранить").click()
    expect(ed.locator(".warn")).to_be_visible()
    assert fetch_row(path, rid)["amount_cents"] == 10000
    ed.locator("button", has_text="Да, столько").click()
    expect(page.locator("#today-msg")).to_contain_text("изменена")
    assert fetch_row(path, rid)["amount_cents"] == 9999900


def test_editor_changes_text_category_and_method_for_income(page, server):
    port, path = server
    rid = seed(path, direction="in", cents=12345, cur="USD", usd=12345, category="доход", method=None)
    open_today(page, port)
    li = page.locator("#today-list li").first
    li.click()
    li.locator("button", has_text="Изменить").click()
    ed = li.locator(".editor")
    ed.locator("select").nth(2).select_option("__other")
    ed.locator("input[placeholder='Название способа']").fill("Мой банк")
    ed.locator("input").nth(1).fill("за урок")
    ed.locator("button", has_text="Сохранить").click()
    expect(page.locator("#today-msg")).to_contain_text("изменена")
    r = fetch_row(path, rid)
    assert (r["note"], r["amount_cents"], r["currency"], r["usd_cents"]) == ("за урок", 12345, "USD", 12345)
    c = db.connect(path, readonly=True)
    assert c.execute("SELECT title FROM methods WHERE code=?", (r["method"],)).fetchone()[0] == "Мой банк"


def test_recent_list_rows_open_the_same_actions(page, server):
    port, path = server
    rid = seed(path, cents=10000, cur="TRY")
    open_app(page, port)
    tab(page, "recent")
    page.wait_for_selector("#recent-list li")
    assert page.locator("#recent-list button").count() == 0
    li = page.locator("#recent-list li").first
    li.click()
    li.locator("button", has_text="Изменить").click()
    li.locator(".editor input").first.fill("400")
    li.locator(".editor button", has_text="Сохранить").click()
    expect(page.locator("#recent-msg")).to_contain_text("изменена")
    assert fetch_row(path, rid)["amount_cents"] == 40000


def test_month_view_has_only_aggregates_no_record_rows(page, server):
    port, path = server
    seed(path, cents=10000, cur="TRY", category="жильё")
    open_app(page, port)
    tab(page, "month")
    page.wait_for_selector("#month-cats-bars .bar-row")
    assert page.locator("#view-month .tappable").count() == 0                          # строк записей нет, только итоги


def test_manual_choice_does_not_leak_into_the_next_record(page, server):
    port, path = server
    open_app(page, port)
    page.fill("#amount", "100")
    page.fill("#note", "такси")
    page.select_option("#cat-select", "бар")
    press(page)
    page.wait_for_selector("#ok:not([hidden])")
    assert last_row(path)["category"] == "бар"
    expect(page.locator("#cat-select")).to_have_value("прочее")
    page.fill("#amount", "101")
    page.fill("#note", "такси")
    expect(page.locator("#cat-select")).to_have_value("транспорт")
    press(page)
    page.wait_for_selector("#ok-text:has-text('транспорт')")
    assert last_row(path)["category"] == "транспорт"
