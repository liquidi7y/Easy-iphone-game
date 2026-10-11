"""Конфигурация и регистри: формули, материали, ценоразписи, курсове, правила.

Цени, прагове, добавки и материални правила идват само от данни, не от source code.
В тази версия регистрите се зареждат от JSON; следващата стъпка е PostgreSQL.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

PriceUnit = Literal["per_m2", "per_1000m2", "per_sheet"]
Currency = Literal["BGN", "EUR"]
TaxBasis = Literal["excl_vat", "incl_vat"]
PriceListStatus = Literal["draft", "approved", "active", "reference", "archived"]

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "data" / "seed_test.json"


class Interval(BaseModel):
    """Интервал с изрично зададени включени граници. upper=None означава без горна граница."""

    lower: Decimal
    lower_inclusive: bool = True
    upper: Optional[Decimal] = None
    upper_inclusive: bool = False

    def contains(self, x: Decimal) -> bool:
        if x < self.lower or (x == self.lower and not self.lower_inclusive):
            return False
        if self.upper is None:
            return True
        return x < self.upper or (x == self.upper and self.upper_inclusive)

    def describe(self) -> str:
        left = "[" if self.lower_inclusive else "("
        right = "]" if self.upper_inclusive else ")"
        upper = "∞" if self.upper is None else str(self.upper)
        return f"{left}{self.lower}, {upper}{right}"


class SizeClass(BaseModel):
    code: str
    lower_m3: Decimal
    lower_inclusive: bool = True
    upper_m3: Optional[Decimal] = None
    upper_inclusive: bool = False

    @property
    def interval(self) -> Interval:
        return Interval(lower=self.lower_m3, lower_inclusive=self.lower_inclusive,
                        upper=self.upper_m3, upper_inclusive=self.upper_inclusive)


class QuantityOptimizationSettings(BaseModel):
    increase_bands_pct: list[Decimal]
    max_increase_pct: Decimal
    good_effective_price_ratio: Decimal


class RoundingSettings(BaseModel):
    policy_version: str
    mode: str = "ROUND_HALF_UP"
    money_places: int = 2
    unit_cost_min_places: int = 6


class BusinessRules(BaseModel):
    min_margin_pct: Optional[Decimal] = None
    min_sales_price: Optional[Decimal] = None


class Settings(BaseModel):
    defaults: dict[str, str]
    rounding: RoundingSettings
    allow_reference_prices: bool = False
    size_classes: list[SizeClass]
    quantity_optimization: QuantityOptimizationSettings
    material_upgrade_threshold_pct: Decimal
    business_rules: BusinessRules = BusinessRules()


class ExchangeRate(BaseModel):
    from_: Currency = Field(alias="from")
    to: Currency
    rate: Decimal
    date: str
    source: str


class AdditiveOverride(BaseModel):
    flute: Optional[str] = None
    machine_mode: Optional[str] = None
    additives: dict[str, Decimal]


class BoxStyle(BaseModel):
    name: str
    formula: str
    formula_version: str
    status: str
    additives: dict[str, Decimal]
    additive_overrides: list[AdditiveOverride] = []


class TechnicalValue(BaseModel):
    name: str  # ECT, BCT, DIN ...
    value: Decimal
    unit: str
    source: str
    conditions: Optional[str] = None


class Alternative(BaseModel):
    code: str
    type: Literal["ECONOMY", "UPGRADE", "HEAVY_DUTY", "SIMILAR"]
    priority: int = 1
    technical_equivalence: bool = False
    equivalence_conditions: Optional[str] = None


class Material(BaseModel):
    display_code: str
    factory_code: str
    flute: str
    layers: Optional[int] = None
    paper_combination: Optional[str] = None
    kraft_outer: Optional[bool] = None
    kraft_inner: Optional[bool] = None
    grammage_gsm: Optional[Decimal] = None
    technical: list[TechnicalValue] = []
    tags: list[str] = []
    alternatives: list[Alternative] = []


class Tier(BaseModel):
    code: str
    lower_m2: Decimal
    lower_inclusive: bool = True
    upper_m2: Optional[Decimal] = None
    upper_inclusive: bool = False

    @property
    def interval(self) -> Interval:
        return Interval(lower=self.lower_m2, lower_inclusive=self.lower_inclusive,
                        upper=self.upper_m2, upper_inclusive=self.upper_inclusive)


class OrderRules(BaseModel):
    min_order_m2: Optional[Decimal] = None
    min_invoiced_m2: Optional[Decimal] = None


class PriceList(BaseModel):
    id: str
    version: int
    supplier: str
    company_contract: Optional[str] = None
    currency: Currency
    price_unit: PriceUnit
    tax_basis: TaxBasis
    status: PriceListStatus
    valid_from: str
    valid_to: Optional[str] = None
    tier_basis: str
    order_rules: OrderRules = OrderRules()
    tiers: list[Tier]
    prices: dict[str, dict[str, Decimal]]


class Config(BaseModel):
    test_data_only: bool = False
    rule_version: str
    settings: Settings
    exchange_rates: list[ExchangeRate]
    box_styles: dict[str, BoxStyle]
    materials: dict[str, Material]
    price_lists: list[PriceList]

    def usable_price_lists(self) -> list[PriceList]:
        allowed = {"active", "approved"}
        if self.settings.allow_reference_prices:
            allowed.add("reference")
        return [p for p in self.price_lists if p.status in allowed]

    def price_list_for(self, material_code: str, price_list_id: Optional[str] = None) -> Optional[PriceList]:
        for pl in self.usable_price_lists():
            if price_list_id and pl.id != price_list_id:
                continue
            if material_code in pl.prices:
                return pl
        return None


def load_config(path: Optional[str | Path] = None) -> Config:
    path = Path(path or os.environ.get("CALC_CONFIG") or DEFAULT_CONFIG)
    # parse_float=str: числата от JSON никога не минават през float
    data = json.loads(path.read_text(encoding="utf-8"), parse_float=str)
    return Config.model_validate(data)


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()
