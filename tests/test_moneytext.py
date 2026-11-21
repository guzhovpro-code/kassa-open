"""Этап 4 (бот): разбор денежных фраз и детектор «похоже на деньги» (moneytext.py)."""
import pytest

from kassa_api import moneytext as mt

TODAY = "2026-10-15"


def p(text, **kw):
    return mt.parse(text, TODAY, **kw)


# --- детектор: должны ловиться -------------------------------------------------------------------------------
MONEY = [
    "потратил 20 лир на обед",
    "пришло 3000 рублей",
    "купил кофе за 150",
    "заплатил 1500 за аренду",
    "получил 200 долларов",
    "перевели 50 usdt",
    "+70000₽",
    "потратил пять тысяч лир",
    "оплатил интернет 12,5 евро",
    "отдал 30к за ремонт",
    "кофе 150 лир",
    "потратил 20 на обед",
    "$20 такси",
    "заработал 1 500 usd",
    "потратил две тысячи пятьсот лир на продукты",
    "пришло 3000",
    "заплатил за такси 15",
    "500 лир потратил на ужин",
    # привычные формы без глагола и с аббревиатурами (ревью Рафа, P1-2)
    "кофе 150",
    "итого 500",
    "сумма 500",
    "-500 такси",
    "+3000 зарплата",
    "500 тл",
    "потратил 500 тл",
    "1k",
    "снял 5000",
    "ушло 300 на такси",
    "кофе стоил 150",
    "пришло 3000 за оплату заказа",
    "он вернул мне 500",
    # длинные сообщения: денежный фрагмент ищется в каждой части
    "сегодня " + "очень " * 12 + "потратил 20 лир",
    "Заплатил 1500 лир за аренду, 3000 за еду, 200 за такси, ещё что-то там, и ещё много слов тут",
    "Сегодня был длинный хороший день, утром пробежал пять километров и поработал над проектом. "
    "Потом потратил 500 лир на кофе и книгу, вечером смотрел кино и ложился спать пораньше",
    # диапазоны и множители не угадываем, но перехватываем, чтобы спросить
    "потратил 500-600 лир",
    "потратил от 500 до 600 лир",
    "потратил 2x500 лир",
    "потратил 500 лир или 600",
    "потратил 10 к вечеру",
    "20 лир 50 куруш",
    "5 рублей 50 копеек",
]
# --- не должны ловиться (время, количество, единицы, нет суммы, длинный текст, префикс) -------------------------
NOT_MONEY = [
    "как дела?",
    "встреча в 12:00",
    "потратил 2 часа на отчёт",
    "потратил три часа на отчёт",
    "3 подхода по 10 повторений",
    "сделал 3 подхода",
    "пробежал 5 км",
    "проснулся в 7 утра",
    "купил хлеб",
    "оплатил счёт",
    "получил письмо от Ивана",
    "заработал 3 очка в игре",
    "пришло 5 писем",
    "в 12 часов встреча",
    "отдал 2 часа сну",
    "потратил 15 минут на медитацию",
    "купил 3 яблока",
    "заплатил за 2 недели вперёд",
    "лёг в 23:30",
    "спал 7 часов",
    "вес 72,5",
    "до 30 июня",
    "Дневник: потратил 20 лир на обед",
    "Дневник: потратил три часа на отчёт",
    "/kassa",
    "потратил день на отчёт",
    "перевели на другую работу",
    "получил 2 хороших новости",
    "купил 2 билета",
    # даты, время, числа в быту (P2-6, P1-2)
    "купил билеты на 15.07",
    "встреча 15.07",
    "до 15.07 надо сдать отчёт",
    "вес 72,5",
    "день 3",
    "в 12 встреча",
    "позвонить в 18:30",
    "я заплатил 2 дня назад",
    "сегодня 10к шагов",
    "дом 15 лет назад",
    "у меня 2 встречи, купил хлеб",
    "Сегодня был длинный хороший день, утром пробежал пять километров и поработал над проектом. "
    "Потом гулял два часа, вечером смотрел кино и ложился спать пораньше",
]


@pytest.mark.parametrize("text", MONEY)
def test_money_phrases_are_caught(text):
    assert mt.looks_like_money(text), text


@pytest.mark.parametrize("text", NOT_MONEY)
def test_non_money_phrases_are_not_caught(text):
    assert not mt.looks_like_money(text), text


def test_false_positive_set_is_large_enough():
    assert len(NOT_MONEY) >= 20 and len(MONEY) >= 10


def test_diary_prefix_is_stripped():
    assert mt.strip_diary_prefix("Дневник: потратил 20 лир") == "потратил 20 лир"
    assert mt.strip_diary_prefix("дневник - заплатил 5") == "заплатил 5"
    assert mt.strip_diary_prefix("потратил 20 лир") is None


# --- разбор ------------------------------------------------------------------------------------------------------
def test_parse_basic_digits():
    r = p("потратил 20 лир на обед")
    assert (r["direction"], r["amount"], r["currency"], r["currency_guess"]) == ("out", "20", "TRY", False)
    assert r["date"] == TODAY and r["method"] is None


def test_parse_income_with_verb_and_currency_word():
    r = p("пришло 3000 рублей")
    assert (r["direction"], r["amount"], r["currency"]) == ("in", "3000", "RUB")


def test_parse_decimal_and_thousands_variants():
    assert p("кофе 8,50 евро")["amount"] == "8.5"
    assert p("оплатил 1 500 лир")["amount"] == "1500"
    assert p("потратил 1,5к лир")["amount"] == "1500"
    assert p("потратил 30к рублей")["amount"] == "30000"
    assert p("получил 2 тысячи долларов")["amount"] == "2000"
    assert p("перевели 12.50 usd")["amount"] == "12.5"


def test_parse_word_numbers():
    cases = {
        "потратил пятьсот лир": "500",
        "потратил двадцать пять лир": "25",
        "заплатил сто двадцать три евро": "123",
        "потратил две тысячи пятьсот лир": "2500",
        "потратил тысячу рублей": "1000",
        "потратил тысяча двести лир": "1200",
        "потратил полторы тысячи лир": "1500",
        "потратил пятнадцать тысяч рублей": "15000",
        "потратил один миллион рублей": "1000000",
        "получил триста usdt": "300",
    }
    for text, want in cases.items():
        assert p(text)["amount"] == want, text


def test_parse_refuses_when_unsure():
    for text in ["потратил несколько тысяч лир", "потратил три с половиной тысячи",
                 "купил 3 кофе по 50 лир", "потратил 20 и ещё 30", "потратил 10 лир и 20 долларов",
                 "потратил двадцать тысяч двести тысяч", "потратил 12,345 лир", "потратил 0 лир"]:
        with pytest.raises(mt.Reject) as ei:
            p(text)
        assert ei.value.code == "unsure_amount", text


def test_parse_no_amount():
    for text in ["", "потратил на обед", "как дела", "в 12:00 встреча", "сегодня 2 часа работал"]:
        with pytest.raises(mt.Reject) as ei:
            p(text)
        assert ei.value.code == "no_amount", text


def test_parse_two_numbers_with_one_currency_picks_the_currency_one():
    r = p("обед 2 человека, потратил 300 лир")
    assert (r["amount"], r["currency"]) == ("300", "TRY")


def test_parse_time_is_not_an_amount():
    r = p("в 12:00 потратил 40 лир на кофе")
    assert r["amount"] == "40"
    r = p("потратил 40 лир в 12 на кофе")
    assert r["amount"] == "40"


def test_parse_dates_words():
    assert p("вчера потратил 20 лир")["date"] == "2026-10-14"
    assert p("позавчера потратил 20 лир")["date"] == "2026-10-13"
    assert p("сегодня потратил 20 лир")["date"] == TODAY


def test_parse_direction_signs_and_defaults():
    assert p("+70000 ₽")["direction"] == "in"
    assert p("-2000 лир")["direction"] == "out"
    assert p("500 лир кофе", default_direction="in")["direction"] == "in"       # глагола нет: умолчание формы
    assert p("потратил 500 лир", default_direction="in")["direction"] == "out"   # глагол сильнее умолчания
    assert p("30к руб от Ивана")["direction"] == "in"


def test_parse_currency_default_and_symbol():
    r = p("потратил 20 на обед", default_currency="USDT")
    assert (r["currency"], r["currency_guess"]) == ("USDT", True)
    r = p("$20 такси")
    assert (r["currency"], r["currency_guess"]) == ("USD", False)
    r = p("такси 15 в лирах")
    assert r["currency"] == "TRY"


def test_parse_method_words():
    assert p("потратил 20 лир наличными")["method"] == "cash"
    assert p("оплатил 300 лир с тинькофф")["method"] == "tinkoff"
    assert p("потратил 20 лир редот")["method"] == "redot"
    assert p("получил 100 usdt на bybit", default_direction="in")["method"] == "bybit"
    assert p("потратил 20 лир на холодный кошелёк")["method"] is None            # такой способ только у поступлений


def test_parse_never_returns_float_or_negative():
    r = p("потратил 20 лир")
    assert isinstance(r["amount"], str)
    with pytest.raises(mt.Reject):
        p("потратил 99999999999 лир")




# --- ревью Рафа 15.10: копейки, диапазоны, «к», направление, даты ------------------------------------------------
def test_parse_minor_units_are_added():
    cases = {"20 лир 50 куруш": ("20.5", "TRY"), "5 рублей 50 копеек": ("5.5", "RUB"),
             "3 евро 20 центов": ("3.2", "EUR"), "потратил 20 долларов 5 центов": ("20.05", "USD"),
             "двадцать лир пятьдесят курушей": ("20.5", "TRY"), "потратил 50 куруш": ("0.5", "TRY"),
             "1 фунт 99 пенсов": ("1.99", "GBP")}
    for text, (amount, cur) in cases.items():
        r = p(text)
        assert (r["amount"], r["currency"]) == (amount, cur), text


def test_parse_minor_units_refuse_when_unsure():
    for text in ["20 лир 50 центов", "20 лир 150 куруш", "потратил 50 центов"]:
        with pytest.raises(mt.Reject) as ei:
            p(text)
        assert ei.value.code == "unsure_amount", text


def test_parse_ranges_and_multipliers_are_not_guessed():
    for text in ["потратил 500-600 лир", "потратил от 500 до 600 лир", "потратил 2x500 лир", "потратил 2х500 лир",
                 "потратил 500 лир или 600", "потратил 3 раза по 500 лир", "заплатил 1/2 от 500", "500+300 лир"]:
        with pytest.raises(mt.Reject) as ei:
            p(text)
        assert ei.value.code == "unsure_amount", text


def test_k_is_thousand_only_when_attached():
    assert p("потратил 5к")["amount"] == "5000"
    assert p("потратил 1,5к лир")["amount"] == "1500"
    for text in ["потратил 10 к вечеру", "отдал 300 к концу месяца", "заплатил 5 к 7"]:
        with pytest.raises(mt.Reject):
            p(text)


def test_direction_first_verb_wins_and_returned_money():
    assert p("пришло 3000 за оплату заказа")["direction"] == "in"
    assert p("он вернул мне 500")["direction"] == "in"
    assert p("мне вернул 500 Иван")["direction"] == "in"
    assert p("я вернул долг 500")["direction"] == "out"
    assert p("оплатил заказ, пришло 500", default_direction="in")["direction"] == "out"


def test_date_is_not_an_amount():
    for text in ["купил билеты на 15.07", "кофе 15.07"]:
        with pytest.raises(mt.Reject):
            p(text)
    assert p("заплатил 8.5")["amount"] == "8.5"
    assert p("потратил 15.07 лир")["amount"] == "15.07"       # дробная сумма с валютой - не дата


def test_amount_with_thousand_separators():
    assert p("1.200,00 лир кофе")["amount"] == "1200"
    assert p("потратил 1,200.50 usd")["amount"] == "1200.5"


def test_detector_never_raises_on_noise():
    import random
    rnd = random.Random(7)
    alphabet = "0123456789 .,:-+/xх×*к kтыслирдоллаваткопеекнапотратилза$€₽₺\n"
    for _ in range(3000):
        s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 60)))
        mt.looks_like_money(s)
        try:
            mt.parse(s, TODAY, "out", "TRY")
        except mt.Reject:
            pass


def test_direction_of_first_verb_wins_when_both_present():
    assert mt.direction_of("пришло 500, потратил на это 20") == "in"
    assert mt.direction_of("потратил 20, пришло 500") == "out"
