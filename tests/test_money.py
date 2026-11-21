import pytest

from kassa_api import money


@pytest.mark.parametrize("raw,cents", [
    ("500", 50000), ("12,5", 1250), ("12.50", 1250), ("0,01", 1), (" 1 500 ", 150000), ("1\u00a0500,25", 150025),
    ("999999999", 99999999900), ("1000000000", 100000000000),
])
def test_parse_ok(raw, cents):
    assert money.parse_amount_to_cents(raw) == cents


@pytest.mark.parametrize("raw,code", [
    ("0", "amount_not_positive"), ("0,00", "amount_not_positive"), ("-5", "bad_amount"), ("+5", "bad_amount"),
    ("abc", "bad_amount"), ("", "bad_amount"), ("1e3", "bad_amount"), ("1,", "bad_amount"), (",5", "bad_amount"),
    ("1.234", "too_many_decimals"), ("5,999", "too_many_decimals"), ("1000000000,01", "amount_too_large"),
    ("1,2,3", "bad_amount"), (500, "bad_amount"), (5.5, "bad_amount"), (None, "bad_amount"), (True, "bad_amount"),
    ("NaN", "bad_amount"), ("Infinity", "bad_amount"), ("1" * 13, "bad_amount"),
])
def test_parse_rejects(raw, code):
    with pytest.raises(money.MoneyError) as e:
        money.parse_amount_to_cents(raw)
    assert e.value.args[0] == code


@pytest.mark.parametrize("value,cents", [
    (1500.0, 150000), (12.5, 1250), (0.285, 29), (0.1, 10), (0.2, 20), (-300.0, -30000), (-0.285, -29),
    (1.005, 101), (2.675, 268), (100.0, 10000), (0.30000000000000004, 30),
])
def test_float_to_cents_half_up_on_repr(value, cents):
    assert money.float_to_cents(value) == cents


def test_extra_decimals_and_orig():
    assert money.has_extra_decimals(0.285) and not money.has_extra_decimals(12.5) and not money.has_extra_decimals(3.0)
    assert money.orig_text(0.285) == "0.285"
    assert money.orig_text(1e-05) == "0.00001"
