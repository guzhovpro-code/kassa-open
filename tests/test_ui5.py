"""Этап 5 в браузере: строка «≈ USD» во вкладках «Сегодня» и «Месяц» не врёт нулями. Жалоба владельца: запись 120 USD есть,
а «USD: расходы 0, поступления 0, итог +0»."""
import threading
from datetime import date, timedelta

import pytest

from helpers import BOT_ID, OWNER
from kassa_api import rates
from kassa_api.server import Config, make_servers
from test_stage5 import CBR, ECB, Net
from test_ui import ONLINE, browser, chip, press  # noqa: F401
from test_ui3 import PUB, TODAY, TOKEN, fmt, open_app, page, seed, tab  # noqa: F401

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = pytest.mark.skipif(not ONLINE, reason="telegram.org недоступен")


@pytest.fixture()
def net():
    return Net()


@pytest.fixture()
def server(tmp_path, net):
    db_path = str(tmp_path / "kassa-test.db")
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), "I" * 40, PUB, db_path)
    store = rates.RateStore(tmp_path / "rates-cache.json", fetch=net, log=lambda m: None)
    if net.up:
        store.refresh_if_stale()
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0), rate_store=store)
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield pub.server_address[1], db_path
    for s in (pub, internal):
        s.shutdown()
        s.server_close()


def usd_text(page, tab_name):
    tab(page, tab_name)
    box = "#%s-usd" % tab_name
    page.wait_for_selector(box + ":not(:empty)")
    return page.locator(box).inner_text()


def add_via_form(page, amount, currency, note="кофе"):
    page.fill("#amount", amount)
    chip(page, "cur", currency)
    page.fill("#note", note)
    press(page)
    page.wait_for_selector("#ok:not([hidden])")


def test_form_record_120_usd_shows_120_in_usd_line(page, server):
    port, _ = server
    open_app(page, port)
    add_via_form(page, "120", "USD")
    text = usd_text(page, "today")
    assert "расходы " + fmt(12000) in text and "итог " + fmt(-12000, True) in text
    assert "нет курса" not in text
    assert "расходы 0," not in text and "расходы 0 " not in text


def test_form_record_in_try_gets_a_rate(page, server):
    port, _ = server
    open_app(page, port)
    add_via_form(page, "400", "TRY")
    text = usd_text(page, "today")
    assert "расходы " + fmt(1000) in text and "нет курса" not in text          # курс 40 из подмены сети
    usd_text(page, "month")
    assert page.locator("#month-usd [data-k=out]").get_attribute("data-cents") == "1000"


@pytest.mark.parametrize("n,word", [(1, "1 записи"), (2, "2 записей"), (5, "5 записей"), (11, "11 записей"),
                                    (21, "21 записи")])
def test_all_records_without_rate_say_so_instead_of_zero(page, server, n, word):
    port, path = server
    for _ in range(n):
        seed(path, cents=1000, cur="BTC", usd=None)                           # курса для BTC нет
    open_app(page, port)
    text = usd_text(page, "today")
    assert text == "≈ USD: нет курса для " + word
    assert "расходы" not in text and "итог" not in text
    assert usd_text(page, "month") == text
    # суммы по самой валюте видны всегда
    card = page.locator("#today-totals .card").first.inner_text()
    assert "BTC" in card and fmt(1000 * n) in card


def test_partial_rates_show_sums_and_the_note(page, server):
    port, path = server
    seed(path, cents=12000, cur="USD", usd=12000)
    seed(path, cents=1000, cur="BTC", usd=None)
    seed(path, cents=1000, cur="BTC", usd=None)
    open_app(page, port)
    text = usd_text(page, "today")
    assert "расходы " + fmt(12000) in text and "Не вошло: нет курса для 2 записей" in text


def test_offline_record_is_saved_and_line_says_no_rate(page, tmp_path, net):
    net.up = False
    off = tmp_path / "offline"                       # свой каталог: кэш курсов фикстуры server сюда не попадает
    off.mkdir()
    db_path = str(off / "kassa-test.db")
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), "I" * 40, PUB, db_path)
    store = rates.RateStore(off / "rates-cache.json", fetch=net, log=lambda m: None)
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0), rate_store=store)
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        open_app(page, pub.server_address[1])
        add_via_form(page, "400", "TRY")
        text = usd_text(page, "today")
        assert text == "≈ USD: нет курса для 1 записи"
        assert fmt(40000) in page.locator("#today-totals .card").first.inner_text()
    finally:
        for s in (pub, internal):
            s.shutdown()
            s.server_close()


def test_empty_day_shows_no_usd_line(page, server):
    port, _ = server
    open_app(page, port)
    tab(page, "today")
    page.wait_for_selector("#today-totals .note")
    assert page.locator("#today-usd").inner_text() == ""
