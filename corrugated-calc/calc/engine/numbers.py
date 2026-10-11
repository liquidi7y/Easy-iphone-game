"""Decimal помощни функции. Финансовото ядро не използва float."""

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, localcontext

PRECISION = 50
ROUNDING_MODES = {"ROUND_HALF_UP": ROUND_HALF_UP}


def dec(value) -> Decimal:
    """Строго преобразуване към Decimal; float е забранен."""
    if isinstance(value, bool):
        raise TypeError("bool не е число")
    if isinstance(value, float):
        raise TypeError("float не се допуска във финансовото ядро; подайте низ")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value).strip().replace(",", "."))


def div(a: Decimal, b: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return a / b


def mul(*values: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        result = Decimal(1)
        for v in values:
            result *= v
        return result


def ceil_int(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def quantize(value: Decimal, places: int, mode: str = "ROUND_HALF_UP") -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUNDING_MODES[mode])


def clean(value: Decimal) -> Decimal:
    """Премахва излишни нули без загуба на точност (6.2965680 -> 6.296568)."""
    if value == value.to_integral_value():
        return value.quantize(Decimal(1))
    return value.normalize()
