"""Страховка парсера обмена (exchangetext.py): строгая грамматика всего сообщения проверяется корпусом, а не отдельными фразами.

Три списка (новое ложное срабатывание - одна строка в ADVERSARIAL, положительная фраза - одна строка в POSITIVE):
1. ORDINARY - все денежные фразы существующих тестов кассы, собираются из исходников тестов автоматически (строковые
   константы с цифрой и кириллицей); ни одна не должна стать обменом или отказом «не уверен в обмене»;
2. ADVERSARIAL - ловушки с двумя валютами и глаголами обмена/покупки/перевода: обычные траты, переводы, возвраты долга;
3. POSITIVE - настоящие обмены с точным разбором (пример владельца 10.11 первым).
История: три ложных срабатывания подряд («купил кофе на 3 доллара за 120 лир», «Перевёл 5 000 рублей на карту и оплатил 20
долларов за подписку»), поэтому разбор переписан на грамматику с привязкой к началу и концу сообщения.
"""
import ast
import re
from pathlib import Path

import pytest

from helpers import METHODS
from kassa_api import exchangetext

TESTS = Path(__file__).resolve().parent
TODAY = "2026-11-10"
# файлы с самими обменами сюда не попадают: их фразы - положительные
SKIP = {"test_exchange.py", "test_exchange_corpus.py", "test_ui_exchange.py"}
_CYR, _DIGIT = re.compile(r"[А-Яа-яЁё]"), re.compile(r"\d")


def collect_ordinary() -> list:
    found = set()
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name in SKIP:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = " ".join(node.value.split())
                if 3 <= len(text) <= 300 and _CYR.search(text) and _DIGIT.search(text):
                    found.add(text)
    return sorted(found)


ORDINARY = collect_ordinary()

ADVERSARIAL = [
    # переводы
    "Перевёл 5 000 рублей на карту и оплатил 20 долларов за подписку.",
    "перевёл 5000 рублей на карту, комиссия 20 долларов",
    "перевел 100 долларов маме, это 9000 рублей",
    "перевёл 100 долларов в банк на 4000 лир",
    "перевёл 10000 рублей в 3500 лир",
    "перевёл 500 лир на карту, это 15 долларов",
    # покупки товаров
    "купил кофе на 3 доллара за 120 лир",
    "купил такси за 150 лир, это 4 доллара",
    "купил билет на концерт за 1200 лир, это 30 долларов",
    "купил на 50 долларов продуктов за 1600 лир",
    "купила продукты на 20 евро за 800 лир",
    "купил подарок на 100 долларов за 3500 лир",
    "купил себе кофе на 3 евро за 120 лир",
    "купил сим-карту за 200 лир, 5 долларов",
    "купил проездной за 400 лир на 10 евро",
    "купил 2 билета за 500 лир и 5 долларов",
    "купил ноутбук за 800 долларов, это 34000 лир",
    # «оплатил ... и ...», долги, возвраты, сравнения
    "оплатил 20 долларов за подписку и 500 рублей за связь",
    "оплатил квартиру 500 долларов и 3000 лир за свет",
    "вернул долг 100 долларов, это 9000 рублей",
    "вернули 200 лир, это 6 долларов",
    "на 3 доллара дороже, чем 100 лир",
    "заплатил 120 лир, это 3 доллара",
    "потратил 50 долларов и 2000 рублей на поездку",
    "обед стоил 300 лир, примерно 9 долларов",
    # обмен-подобное без обмена
    "поменял масло в машине, заплатил 800 лир и 5 долларов за мойку",
    "поменял лампочку за 300 лир, потом купил кран за 20 долларов",
    "продал телефон за 5000 рублей и 20 долларов",
    "поменял 5000 рублей на карту и купил кофе за 20 долларов",
    "обменял 100 долларов, потом потратил 2000 лир на еду",
    "сегодня поменял 100 долларов на лиры и оплатил такси 150 лир",
    "купил доллары и оплатил такси 150 лир",
    "обменял 100 долларов на 3500 лир и купил билет за 500 лир",
]

POSITIVE = [
    ("поменял 201 230 рублей на 75 000 лир и 700 долларов, комиссия 758,65",
     ("201230", "RUB", None, [("75000", "TRY", None, None), ("700", "USD", None, None)], ("758.65", "RUB"), TODAY)),
    ("Поменял 5000 рублей на 100 долларов", ("5000", "RUB", None, [("100", "USD", None, None)], None, TODAY)),
    ("Поменял 5000 рублей на 100 долларов.", ("5000", "RUB", None, [("100", "USD", None, None)], None, TODAY)),
    ("обменял 100 usd на 3 500 лир", ("100", "USD", None, [("3500", "TRY", None, None)], None, TODAY)),
    ("сменяла 200 евро на 8000 лир", ("200", "EUR", None, [("8000", "TRY", None, None)], None, TODAY)),
    ("разменял 100 долларов на 3500 лир", ("100", "USD", None, [("3500", "TRY", None, None)], None, TODAY)),
    ("конвертировал 100 долларов в 4000 лир вчера", ("100", "USD", None, [("4000", "TRY", None, None)], None, "2026-11-09")),
    ("вчера конвертировал 100 долларов в 4000 лир", ("100", "USD", None, [("4000", "TRY", None, None)], None, "2026-11-09")),
    ("поменяли 2000 долларов на 80 000 лир позавчера", ("2000", "USD", None, [("80000", "TRY", None, None)], None, "2026-11-08")),
    ("купил 700 долларов за 30000 рублей", ("30000", "RUB", None, [("700", "USD", None, None)], None, TODAY)),
    ("купил себе 300 долларов за 12000 лир", ("12000", "TRY", None, [("300", "USD", None, None)], None, TODAY)),
    ("купила 100 евро за 4500 лир, комиссия 50 лир", ("4500", "TRY", None, [("100", "EUR", None, None)], ("50", "TRY"), TODAY)),
    ("продал 500 usd за 20 000 лир", ("500", "USD", None, [("20000", "TRY", None, None)], None, TODAY)),
    ("продал 300 евро за 12000 лир, комиссия 100 лир", ("300", "EUR", None, [("12000", "TRY", None, None)], ("100", "TRY"), TODAY)),
    ("обменял 5000 рублей на 100 долларов с revolut", ("5000", "RUB", "revolut", [("100", "USD", None, None)], None, TODAY)),
    ("обменял 5000 рублей на 100 долларов со wise", ("5000", "RUB", "wise", [("100", "USD", None, None)], None, TODAY)),
    ("поменял с revolut 100 долларов на 3500 лир", ("100", "USD", "revolut", [("3500", "TRY", None, None)], None, TODAY)),
    ("поменял 5000 рублей на 100 долларов наличными", ("5000", "RUB", None, [("100", "USD", "cash", None)], None, TODAY)),
    ("купил 700 долларов за 30000 рублей налом", ("30000", "RUB", "cash", [("700", "USD", None, None)], None, TODAY)),
    ("поменял 201230 рублей на 75000 лир и 700 долларов, из них 150 000 на лиры",
     ("201230", "RUB", None, [("75000", "TRY", None, "150000"), ("700", "USD", None, None)], None, TODAY)),
    ("обменял 100 евро на 4000 лир и 50 долларов, из них 2000 на лиры",
     ("100", "EUR", None, [("4000", "TRY", None, "2000"), ("50", "USD", None, None)], None, TODAY)),
    ("поменял 300 евро на 5000 лир, 100 долларов и 2000 рублей",
     ("300", "EUR", None, [("5000", "TRY", None, None), ("100", "USD", None, None), ("2000", "RUB", None, None)], None, TODAY)),
    ("поменял 100 долларов на 3 500,50 лир, комиссия банка 15 рублей",
     ("100", "USD", None, [("3500.5", "TRY", None, None)], ("15", "RUB"), TODAY)),
    ("обменял 100 usd на 3000 лир, комиссии нет", ("100", "USD", None, [("3000", "TRY", None, None)], None, TODAY)),
    ("обменял 100 usd на 3000 лир, комиссия 0", ("100", "USD", None, [("3000", "TRY", None, None)], None, TODAY)),
    ("обменял 100 евро на 4000 лир и 50 долларов налом",
     ("100", "EUR", None, [("4000", "TRY", None, None), ("50", "USD", "cash", None)], None, TODAY)),
    ("купил 700 usdt за 60000 рублей", ("60000", "RUB", None, [("700", "USDT", None, None)], None, TODAY)),
]


def shape(text):
    r = exchangetext.parse(text, TODAY, METHODS)
    assert r is not None, text
    return (r["give"]["amount"], r["give"]["currency"], r["give"]["method"],
            [(g["amount"], g["currency"], g["method"], g["part"]) for g in r["get"]],
            None if r["fee"] is None else (r["fee"]["amount"], r["fee"]["currency"]), r["date"])


def test_corpus_sizes_guard_against_a_silent_collection_failure():
    assert len(ORDINARY) >= 100, len(ORDINARY)             # собранные из тестов денежные фразы
    assert len(ADVERSARIAL) >= 30 and len(POSITIVE) >= 20
    assert "потратил 20 лир на обед" in ORDINARY or any("лир" in t for t in ORDINARY)


def test_no_ordinary_money_phrase_of_the_existing_tests_becomes_an_exchange():
    bad = []
    for text in ORDINARY:
        try:
            if exchangetext.parse(text, TODAY, METHODS) is not None:
                bad.append(text)
        except exchangetext.ExchangeReject:
            bad.append("REJECT: " + text)
    assert bad == []


@pytest.mark.parametrize("text", ADVERSARIAL)
def test_adversarial_two_currency_phrases_are_never_exchanges(text):
    assert exchangetext.parse(text, TODAY, METHODS) is None


@pytest.mark.parametrize("text,expected", POSITIVE)
def test_positive_exchange_phrases_parse_exactly(text, expected):
    assert shape(text) == expected


@pytest.mark.parametrize("text", [t for t, _ in POSITIVE])
def test_a_trailing_extra_clause_turns_any_positive_phrase_into_a_non_exchange(text):
    """Строгая грамматика: любой лишний хвост («и оплатил ...», «на карту», второе предложение) - не обмен."""
    for tail in (" и оплатил 20 долларов за подписку", " на карту", ". Потом купил кофе за 120 лир", " и купил хлеб"):
        assert exchangetext.parse(text.rstrip(".") + tail, TODAY, METHODS) is None, text + tail


@pytest.mark.parametrize("text", [t for t, _ in POSITIVE])
def test_a_leading_extra_clause_turns_any_positive_phrase_into_a_non_exchange(text):
    for head in ("оплатил 20 долларов за подписку и ", "купил хлеб, ", "Потом "):
        assert exchangetext.parse(head + text, TODAY, METHODS) is None, head + text
