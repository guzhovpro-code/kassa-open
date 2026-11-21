"""Регрессия детектора и разбора по набору фраз из ревью Рафа (~110 фраз: tests/raph_probe*.py, 15.10.2026).

Ожидание каждой строки: (фраза, ловит ли детектор, результат разбора). Разбор проверяется, только если детектор
ловит фразу (иначе она уходит модели, и разбор не используется). Строки, помеченные «принято», - известные ложные
срабатывания (спецификация разд. 8: цена одного тапа ниже цены пропущенной суммы)."""
import pytest

from kassa_api import moneytext as mt

TODAY = "2026-10-15"

CASES = [
    ('потратил 10 к вечеру', True, 'unsure_amount'),
    ('отдал 300 к концу месяца', True, 'unsure_amount'),
    ('заплатил 5 к 7', True, 'unsure_amount'),
    ('1.200,00 лир кофе', True, ('out', '1200', 'TRY', None)),
    ('купил кофе 1 200,50 лир', True, ('out', '1200.5', 'TRY', None)),
    ('пришло 3000 за оплату заказа', True, ('in', '3000', 'TRY', None)),
    ('он вернул мне 500', True, ('in', '500', 'TRY', None)),
    ('купил билеты на 15.07', False, None),
    ('потратил 500 лир, курс 40', True, ('out', '500', 'TRY', None)),
    ('Курс 40 рублей за доллар', True, ('out', '40', 'RUB', None)),
    ('я заплатил 2 дня назад', False, None),
    ('оплатил 1500', True, ('out', '1500', 'TRY', None)),
    ('получил 5 писем', False, None),
    ('купил 3 яблока за 150', True, ('out', '150', 'TRY', None)),
    ('перевел 20', True, ('out', '20', 'TRY', None)),
    ('кофе 5 tl', True, ('out', '5', 'TRY', None)),
    ('потратил 12,5 лир', True, ('out', '12.5', 'TRY', None)),
    ('потратил 1,5к лир', True, ('out', '1500', 'TRY', None)),
    ('пятьсот лир кофе', True, ('out', '500', 'TRY', None)),
    ('полторы тысячи рублей', True, ('out', '1500', 'RUB', None)),
    ('потратил 100 usdt', True, ('out', '100', 'USDT', None)),
    ('-500 такси', True, ('out', '500', 'TRY', None)),
    ('+3000 зарплата', True, ('in', '3000', 'TRY', None)),
    ('купил 1 кг за 200 лир', True, ('out', '200', 'TRY', None)),
    ('купил за 200', True, ('out', '200', 'TRY', None)),
    ('потратил 20 на обед', True, ('out', '20', 'TRY', None)),
    ('заплатил 8.5', True, ('out', '8.5', 'TRY', None)),
    ('у меня 2 встречи, купил хлеб', False, None),
    ('потратил 5 р', True, ('out', '5', 'RUB', None)),
    ('дом 15 лет назад, потратил 100', True, ('out', '100', 'TRY', None)),
    ('потратил 1 000 000 лир', True, ('out', '1000000', 'TRY', None)),
    ('заплатил 99999999999', True, 'unsure_amount'),
    ('купил 2 за 500 лир', True, ('out', '500', 'TRY', None)),
    ('оплатил 500 лир за 2 дня', True, ('out', '500', 'TRY', None)),
    ('телефон 89991234567 оплатил', True, 'unsure_amount'),
    ('я съел 3 яблока и купил 500 лир', True, ('out', '500', 'TRY', None)),
    ('потратил $20', True, ('out', '20', 'USD', None)),
    ('потратил 20$', True, ('out', '20', 'USD', None)),
    ('потратил 20 долларов 5 центов', True, ('out', '20.05', 'USD', None)),
    ('ровно 5000 лир', True, ('out', '5000', 'TRY', None)),
    ('Мне 5000 лир не хватает', True, ('out', '5000', 'TRY', None)),
    ('сколько стоит 100 лир в долларах', True, ('out', '100', 'TRY', None)),
    ('Заплатил 1500 лир за аренду, 3000 за еду, 200 за такси, ещё что-то там, и ещё много слов тут', True, 'unsure_amount'),
    ('заплатил 200 руб', True, ('out', '200', 'RUB', None)),
    ('потратил 200 руб. на такси', True, ('out', '200', 'RUB', None)),
    ('10 лир', True, ('out', '10', 'TRY', None)),
    ('лир 10', True, ('out', '10', 'TRY', None)),
    ('потратил десять лир', True, ('out', '10', 'TRY', None)),
    ('сегодня потратил 1.5к', True, ('out', '1500', 'TRY', None)),
    ('1k', True, ('out', '1000', 'TRY', None)),
    ('Дневник: потратил 500 лир', False, None),
    ('пять тысяч двести лир', True, ('out', '5200', 'TRY', None)),
    ('двадцать пять лир', True, ('out', '25', 'TRY', None)),
    ('три тысячи пятьсот лир', True, ('out', '3500', 'TRY', None)),
    ('сто двадцать пять тысяч рублей', True, ('out', '125000', 'RUB', None)),
    ('две с половиной тысячи лир', True, 'unsure_amount'),
    ('тысяча двести', False, None),
    ('пятьсот пятьдесят лир', True, ('out', '550', 'TRY', None)),
    ('пять тысяч', False, None),
    ('двадцать тысяч лир кофе', True, ('out', '20000', 'TRY', None)),
    ('миллион рублей', True, ('out', '1000000', 'RUB', None)),
    ('два миллиона рублей', True, ('out', '2000000', 'RUB', None)),
    ('сто', False, None),
    ('сто лир', True, ('out', '100', 'TRY', None)),
    ('двадцать один лир', True, ('out', '21', 'TRY', None)),
    ('двадцать лир пятьдесят курушей', True, ('out', '20.5', 'TRY', None)),
    ('20 лир 50 куруш', True, ('out', '20.5', 'TRY', None)),
    ('5 рублей 50 копеек', True, ('out', '5.5', 'RUB', None)),
    ('3 евро 20 центов', True, ('out', '3.2', 'EUR', None)),
    ('потратил 1 500 лир', True, ('out', '1500', 'TRY', None)),
    ('потратил 1\xa0500 лир', True, ('out', '1500', 'TRY', None)),
    ('потратил 1.500 лир', True, 'unsure_amount'),
    ('потратил 1,500 лир', True, 'unsure_amount'),
    ('потратил 1500.50 лир', True, ('out', '1500.5', 'TRY', None)),
    ('потратил 1 500.5 лир', True, ('out', '1500.5', 'TRY', None)),
    ('потратил 0,5 лир', True, ('out', '0.5', 'TRY', None)),
    ('потратил 0.005 лир', True, 'unsure_amount'),
    ('потратил 12,345 лир', True, 'unsure_amount'),
    ('потратил 100.1 лир', True, ('out', '100.1', 'TRY', None)),
    ('купил за 1к', True, ('out', '1000', 'TRY', None)),
    ('пришло 2 тыс долларов', True, ('in', '2000', 'USD', None)),
    ('пришло 2 тыс. долларов', True, ('in', '2000', 'USD', None)),
    ('пришло 2.5 млн рублей', True, ('in', '2500000', 'RUB', None)),
    ('кофе 150', True, ('out', '150', 'TRY', None)),
    ('обед 450 лир', True, ('out', '450', 'TRY', None)),
    ('в 2026 году потратил 500', True, ('out', '500', 'TRY', None)),
    ('15 октября потратил 500 лир', True, ('out', '500', 'TRY', None)),
    ('потратил 500 лир 15.10', True, ('out', '500', 'TRY', None)),
    ('потратил 500 лир 15.10.2026', True, ('out', '500', 'TRY', None)),
    ('заплатил 1/2 от 500', True, 'unsure_amount'),
    ('потратил 500-600 лир', True, 'unsure_amount'),
    ('потратил от 500 до 600 лир', True, 'unsure_amount'),
    ('потратил 3 раза по 500 лир', True, 'unsure_amount'),
    ('потратил 2x500 лир', True, 'unsure_amount'),
    ('потратил 500 лир или 600', True, 'unsure_amount'),
    ('потратил 500лир', True, ('out', '500', 'TRY', None)),
    ('потратил 500tl', True, ('out', '500', 'TRY', None)),
    ('потратил 500₺', True, ('out', '500', 'TRY', None)),
    ('потратил ₺500', True, ('out', '500', 'TRY', None)),
    ('потратил 500 тл', True, ('out', '500', 'TRY', None)),
    ('потратил 500 TL', True, ('out', '500', 'TRY', None)),
    ('заплатил 5 евро', True, ('out', '5', 'EUR', None)),
    ('купил за 5 EUR', True, ('out', '5', 'EUR', None)),
    ('перевёл 1000 в долларах', True, ('out', '1000', 'USD', None)),
    ('потратил 50 usd и 20 usdt', True, 'unsure_amount'),
    ('купил 50 акций за 3000 лир', True, ('out', '3000', 'TRY', None)),
    ('я потратил 5', True, ('out', '5', 'TRY', None)),
    ('мы потратили 3 часа и 500 лир', True, ('out', '500', 'TRY', None)),
    ('сумма 500', True, ('out', '500', 'TRY', None)),
    ('итого 500', True, ('out', '500', 'TRY', None)),
    ('потратил около 500 лир', True, ('out', '500', 'TRY', None)),
    ('потратил примерно 500 лир', True, ('out', '500', 'TRY', None)),
    ('потратил 500 лир наличными', True, ('out', '500', 'TRY', 'cash')),
    ('потратил 500 лир картой тинькофф', True, ('out', '500', 'TRY', 'tinkoff')),
    ('получил 100 долларов на bybit', True, ('in', '100', 'USD', 'bybit')),
    ('пришло 100 usdt на холодный кошелек', True, ('in', '100', 'USDT', 'cold_wallet')),
]


def _parse(text):
    try:
        r = mt.parse(text, TODAY, "out", "TRY")
        return (r["direction"], r["amount"], r["currency"], r["method"])
    except mt.Reject as exc:
        return exc.code


@pytest.mark.parametrize("text,detected,parsed", CASES, ids=[str(i) for i in range(len(CASES))])
def test_regression_phrase(text, detected, parsed):
    assert mt.looks_like_money(text) is detected, text
    if detected:
        assert _parse(text) == parsed, text


def test_regression_set_size():
    assert len(CASES) >= 105
