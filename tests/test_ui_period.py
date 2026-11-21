"""Вкладка «Период» в браузере: произвольные даты, быстрые кнопки «7 дней» и «30 дней», неверные даты."""
from datetime import date, timedelta

import pytest

from test_ui import ONLINE, browser  # noqa: F401
from test_ui3 import TODAY, fmt, open_app, page, seed, server, tab  # noqa: F401

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.skipif(not ONLINE, reason="telegram.org недоступен")


def day(n):
    return (date.fromisoformat(TODAY) - timedelta(days=n)).isoformat()


def set_dates(page, first, last):
    page.fill("#period-from", first)
    page.fill("#period-to", last)
    page.locator("#period-to").dispatch_event("change")


def test_period_tab_default_is_last_7_days_and_looks_like_month(page, server):
    port, path = server
    seed(path, cents=10000, cur="USD", usd=10000, day=day(2), category="кафе")
    seed(path, direction="in", cents=50000, cur="USD", usd=50000, day=day(1), method="cash", category="доход")
    seed(path, cents=999, cur="USD", usd=999, day=day(30), category="старое")        # вне периода
    open_app(page, port)
    tab(page, "period")
    page.wait_for_selector("#period-usd [data-k=out]")
    assert page.locator("#period-from").input_value() == day(6) and page.locator("#period-to").input_value() == TODAY
    assert page.locator("#period-usd [data-k=out]").get_attribute("data-cents") == "10000"
    assert page.locator("#period-usd [data-k=in]").get_attribute("data-cents") == "50000"
    expect(page.locator("#period-cats-bars")).to_contain_text("кафе")
    expect(page.locator("#period-cats-bars")).not_to_contain_text("старое")
    expect(page.locator("#period-methods-bars")).to_contain_text("Наличные")
    page.locator("#period-sums > summary").click()                                  # итоги свёрнуты по умолчанию
    page.locator("#period-cur-toggle").click()
    expect(page.locator("#period-totals")).to_contain_text("USD")


def test_period_quick_buttons_and_single_day(page, server):
    port, path = server
    seed(path, cents=100, cur="USD", usd=100, day=day(20), category="а")
    seed(path, cents=300, cur="USD", usd=300, day=day(10), category="б")
    seed(path, cents=700, cur="USD", usd=700, day=TODAY, category="в")
    open_app(page, port)
    tab(page, "period")
    page.wait_for_selector("#period-usd [data-k=out]")
    assert page.locator("#period-usd [data-k=out]").get_attribute("data-cents") == "700"       # за 7 дней
    page.locator("#period-30").click()
    expect(page.locator("#period-usd [data-k=out]")).to_have_attribute("data-cents", "1100")
    assert page.locator("#period-from").input_value() == day(29)
    page.locator("#period-7").click()
    expect(page.locator("#period-usd [data-k=out]")).to_have_attribute("data-cents", "700")
    set_dates(page, day(10), day(10))                                                          # один день
    expect(page.locator("#period-usd [data-k=out]")).to_have_attribute("data-cents", "300")
    expect(page.locator("#period-title")).not_to_contain_text(" - ")


def test_period_empty_and_bad_dates(page, server):
    port, path = server
    open_app(page, port)
    tab(page, "period")
    page.wait_for_selector("#period-usd:not(:empty)")
    expect(page.locator("#period-usd")).to_contain_text("Записей нет")
    set_dates(page, day(1), day(3))                                                            # с позже по
    expect(page.locator("#period-msg")).to_be_visible()
    expect(page.locator("#period-msg")).to_contain_text("Такого периода нет")
    set_dates(page, day(500), TODAY)                                                           # длиннее 400 дней
    expect(page.locator("#period-msg")).to_contain_text("400")
    set_dates(page, day(3), day(1))                                                            # снова верно
    expect(page.locator("#period-msg")).to_be_hidden()


def test_period_list_rows_by_day_filter_quick_buttons_and_edit(page, server):
    port, path = server
    seed(path, cents=300, cur="USD", usd=300, day=day(1), category="вчера-кафе")
    seed(path, direction="in", cents=900, cur="USD", usd=900, day=day(1), method="cash", category="доход")
    seed(path, cents=100, cur="USD", usd=100, day=day(10), category="давно")
    open_app(page, port)
    tab(page, "period")
    page.wait_for_selector("#period-list .rec")
    assert page.locator("#period-list .rec").count() == 2 and page.locator("#period-list .daysep").count() == 1
    expect(page.locator("#period-list")).not_to_contain_text("давно")
    page.locator("#period-14").click()
    expect(page.locator("#period-list .rec")).to_have_count(3)
    page.locator("#period-1").click()                                   # «Вчера»: один день, обе даты равны
    expect(page.locator("#period-list .rec")).to_have_count(2)
    assert page.locator("#period-from").input_value() == day(1) == page.locator("#period-to").input_value()
    page.locator("#period-dir .chip", has_text="Поступления").click()
    expect(page.locator("#period-list .rec")).to_have_count(1)
    page.locator("#period-dir .chip", has_text="Все").click()
    expect(page.locator("#period-list .rec")).to_have_count(2)
    page.locator("#period-list .rec", has_text="вчера-кафе").click()    # та же правка, что в «Последних»
    page.locator("#period-list .acts button", has_text="Удалить").click()
    page.locator("#period-list .acts button", has_text="Да, удалить").click()
    expect(page.locator("#period-list .rec")).to_have_count(1)
