"""Форма показывает ровно способы из базы (начальный набор «Наличные» плюс добавленные командой methods)."""
import threading

import pytest

from conftest import REAL_METHODS_SEED
from helpers import BOT_ID, OWNER
from kassa_api import db, methods
from kassa_api.server import Config, make_servers
from test_ui import (ONLINE, PRIV, PUB, STUB, TOKEN, browser, chip, open_form, page, playwright_sync)  # noqa: F401

pytestmark = pytest.mark.skipif(not ONLINE, reason="telegram.org недоступен")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setattr(db, "METHODS_SEED", REAL_METHODS_SEED)
    db_path = str(tmp_path_factory.mktemp("ui-methods") / "kassa-test.db")
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), "I" * 40, PUB, db_path)
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0))
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield pub.server_address[1], db_path
    for s in (pub, internal):
        s.shutdown()
        s.server_close()
    mp.undo()


def test_form_shows_exactly_configured_methods(page, server):
    port, path = server
    open_form(page, port, method=None)
    assert page.locator("#method button").all_inner_texts() == ["Наличные", "Ещё ▾"]      # «Ещё» ведёт к «Другой…»
    conn = db.connect(path)
    methods.add_method(conn, "Карта Revolut", "card")
    methods.add_method(conn, "Биржа OKX", "exchange")
    methods.add_method(conn, "Кошелёк Ledger", "wallet")
    conn.close()
    page.goto("about:blank")                                  # иначе тот же адрес не перезагрузится
    open_form(page, port, method=None)
    assert page.locator("#method button").all_inner_texts() == ["Наличные", "Карта Revolut", "Биржа OKX", "Ещё ▾"]
    chip(page, "method", "Ещё ▾")
    assert page.locator("#method-select option").all_inner_texts() == [
        "Выберите способ", "Наличные", "Карта Revolut", "Биржа OKX", "Кошелёк Ledger", "Другой…"]
    conn = db.connect(path)
    methods.remove_method(conn, "Биржа OKX")
    conn.close()
    page.goto("about:blank")                                  # иначе тот же адрес не перезагрузится
    open_form(page, port, method=None)
    assert page.locator("#method button").all_inner_texts() == ["Наличные", "Карта Revolut", "Кошелёк Ledger", "Ещё ▾"]
