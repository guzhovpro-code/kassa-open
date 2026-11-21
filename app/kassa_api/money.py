"""Деньги: только целые центы. Никаких float в расчётах и записи.

Сумма из формы приходит строкой и разбирается через Decimal; из finance.db (REAL) - через repr
числа (кратчайшая строка, которая однозначно задаёт float), округление половина-вверх.
"""
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_UNITS = 1_000_000_000          # потолок суммы в основных единицах (защита от опечатки в нулях)
# только ASCII-цифры: \d в Python принимает и арабо-индийские, и другие Unicode-цифры
_AMOUNT_RE = re.compile(r"[0-9]{1,12}(?:[.,][0-9]{1,2})?")
_TOO_MANY_DECIMALS_RE = re.compile(r"[0-9]{1,12}[.,][0-9]{3,}")


class MoneyError(ValueError):
    """Код причины в args[0]: bad_amount, amount_not_positive, amount_too_large, too_many_decimals."""


def parse_amount_to_cents(raw) -> int:
    """'1 500,5' -> 150050. Только строка; не больше двух знаков после запятой; строго > 0."""
    if not isinstance(raw, str):
        raise MoneyError("bad_amount")
    text = raw.strip().replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
    if _TOO_MANY_DECIMALS_RE.fullmatch(text):
        raise MoneyError("too_many_decimals")
    if not _AMOUNT_RE.fullmatch(text):
        raise MoneyError("bad_amount")
    value = Decimal(text.replace(",", "."))
    cents = int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))
    if cents <= 0:
        raise MoneyError("amount_not_positive")
    if cents > MAX_UNITS * 100:
        raise MoneyError("amount_too_large")
    return cents


def float_to_cents(value: float) -> int:
    """REAL из finance.db -> центы, половина-вверх по модулю; знак сохраняется."""
    try:
        dec = Decimal(repr(float(value)))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MoneyError("bad_amount") from exc
    sign = -1 if dec < 0 else 1
    return sign * int((abs(dec) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def has_extra_decimals(value: float) -> bool:
    """True, если у числа больше двух знаков после запятой (по кратчайшему repr)."""
    dec = Decimal(repr(float(value)))
    return dec != dec.quantize(Decimal("0.01"))


def orig_text(value: float) -> str:
    """Исходная сумма строкой без экспоненты (для amount_orig)."""
    return format(Decimal(repr(float(value))), "f")
