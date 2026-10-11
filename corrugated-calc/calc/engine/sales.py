"""Продажна цена, марж и markup спрямо изрично обозначената включена себестойност."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .config import Config, Currency
from .models import Money, SalesInput, SalesResult
from .numbers import clean, div, mul
from .pricing import convert, money

COST_BASIS_LABELS = {
    "material": "Марж спрямо материалната себестойност",
    "full": "Марж спрямо пълната включена себестойност",
}


def compute_sales(cfg: Config, sales: SalesInput, cost_per_item: Money, quantity: Optional[int],
                  cost_basis: str = "material") -> SalesResult:
    currency: Currency = sales.currency or cost_per_item.currency
    cost = cost_per_item.amount
    rate_info = None
    if currency != cost_per_item.currency:
        cost, rate_info = convert(cost, cost_per_item.currency, currency, cfg)

    if sales.sales_price_per_item is not None:
        price = sales.sales_price_per_item
    elif sales.target_margin_pct is not None:
        # sales_price = cost / (1 − target_margin)
        price = div(cost, Decimal(1) - div(sales.target_margin_pct, Decimal(100)))
    elif sales.markup_pct is not None:
        price = mul(cost, Decimal(1) + div(sales.markup_pct, Decimal(100)))
    else:
        raise ValueError("Задайте продажна цена, целеви марж или markup")

    profit = price - cost
    margin = clean(div(profit * 100, price)) if price != 0 else None
    markup = clean(div(profit * 100, cost)) if cost != 0 else None

    warnings = []
    rules = cfg.settings.business_rules
    if rules.min_margin_pct is not None and margin is not None and margin < rules.min_margin_pct:
        warnings.append(f"Маржът {margin}% е под минималния {rules.min_margin_pct}%.")
    if profit < 0:
        warnings.append("Продажната цена е под включената себестойност.")

    res = SalesResult(cost_basis_label=COST_BASIS_LABELS[cost_basis], cost_per_item=money(cost, currency, cfg),
                      sales_price_per_item=money(price, currency, cfg), profit_per_item=money(profit, currency, cfg),
                      margin_percent=margin, markup_percent=markup, exchange_rate=rate_info,
                      business_warnings=warnings)
    if quantity:
        res.total_sales = money(mul(price, Decimal(quantity)), currency, cfg)
        res.total_profit = money(mul(profit, Decimal(quantity)), currency, cfg)
    return res
