"""Типизиран вход/изход на engine. Decimal стойностите се сериализират като низове."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .config import Currency, PriceUnit, TaxBasis

Mode = Literal["sheet", "sheet_items", "box"]


def _no_float(v):
    if isinstance(v, float):
        # JSON числата се приемат, но точните стойности трябва да се подават като низове
        return str(v)
    return v


class ManualPrice(BaseModel):
    price: Decimal = Field(gt=0)
    unit: PriceUnit
    currency: Currency
    tax_basis: TaxBasis = "excl_vat"

    _f = field_validator("price", mode="before")(_no_float)


class Dimensions2D(BaseModel):
    length_mm: int = Field(gt=0)
    width_mm: int = Field(gt=0)


class SheetInput(BaseModel):
    length_mm: int = Field(gt=0, description="Ос „дължина“ на листа")
    width_mm: int = Field(gt=0, description="Ос „ширина“ на листа")
    items_per_sheet: Optional[int] = Field(default=None, gt=0)
    flute_direction: Optional[Literal["along_length", "along_width"]] = Field(
        default=None, description="Посока на вълната спрямо именуваната ос; не се извежда от по-дългата страна")
    layout_source: Optional[Literal["manual_count", "approved_scheme"]] = None


class BoxInput(BaseModel):
    length_mm: int = Field(gt=0)
    width_mm: int = Field(gt=0)
    height_mm: int = Field(gt=0)
    dimension_basis: Optional[Literal["internal", "external"]] = None
    fefco: Optional[str] = None
    manual_blank: Optional[Dimensions2D] = Field(default=None, description="Ръчно зададен разгъв")


class WasteInput(BaseModel):
    rate_pct: Decimal = Field(ge=0, lt=100)
    type: Literal["loss_of_input", "addition_on_net"]
    source: str = "manual"

    _f = field_validator("rate_pct", mode="before")(_no_float)


class SalesInput(BaseModel):
    sales_price_per_item: Optional[Decimal] = Field(default=None, gt=0)
    currency: Optional[Currency] = None
    target_margin_pct: Optional[Decimal] = Field(default=None, lt=100)
    markup_pct: Optional[Decimal] = Field(default=None, gt=-100)

    _f = field_validator("sales_price_per_item", "target_margin_pct", "markup_pct", mode="before")(_no_float)


class CalculateRequest(BaseModel):
    mode: Optional[Mode] = None
    quantity: Optional[int] = Field(default=None, gt=0, description="Изделия (или листове в режим sheet)")
    sheet: Optional[SheetInput] = None
    box: Optional[BoxInput] = None
    material_code: Optional[str] = None
    flute: Optional[str] = None
    manual_price: Optional[ManualPrice] = None
    price_list_id: Optional[str] = None
    waste: Optional[WasteInput] = None
    sales: Optional[SalesInput] = None
    load_kg: Optional[Decimal] = Field(default=None, gt=0)
    stacking: Optional[int] = Field(default=None, gt=0)
    environment: Optional[Literal["dry", "humid", "cold", "wet"]] = None
    export: Optional[bool] = None
    rough_handling: Optional[bool] = None

    @model_validator(mode="after")
    def _infer_mode(self):
        if self.mode is None:
            if self.box:
                self.mode = "box"
            elif self.sheet and self.sheet.items_per_sheet:
                self.mode = "sheet_items"
            elif self.sheet:
                self.mode = "sheet"
            else:
                raise ValueError("Липсват размери: задайте box (L/W/H) или sheet (лист)")
        if self.mode == "box" and not self.box:
            raise ValueError("Режим box изисква L/W/H")
        if self.mode in ("sheet", "sheet_items") and not self.sheet:
            raise ValueError("Листовият режим изисква размери на листа")
        if self.mode == "sheet_items" and not self.sheet.items_per_sheet:
            raise ValueError("Режим sheet_items изисква изделия в лист")
        if self.box and self.sheet and not self.sheet.items_per_sheet:
            raise ValueError("Лист към кашон изисква брой изделия в лист")
        return self


# ---------------------------------------------------------------- изход

class Money(BaseModel):
    amount: Decimal
    currency: Currency
    rounded: Decimal


class TraceStep(BaseModel):
    step: str
    formula: Optional[str] = None
    inputs: dict[str, Any] = {}
    result: Any = None
    source: Optional[str] = None


class Warning_(BaseModel):
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "warning"


class BlankResult(BaseModel):
    fefco: str
    formula_version: Optional[str]
    formula_status: str
    source: Literal["formula", "manual"]
    blank_length_mm: int
    blank_width_mm: int
    area_m2: Decimal
    volume_m3: Decimal
    dimension_basis: str
    additives: dict[str, Decimal]


class LayoutResult(BaseModel):
    sheet_length_mm: int
    sheet_width_mm: int
    sheet_area_m2: Decimal
    items_per_sheet: int
    blank_area_m2: Optional[Decimal] = None
    layout_waste_percent: Optional[Decimal] = None
    flute_direction: Optional[str] = None
    flute_direction_status: str
    layout_status: str


class QuantityResult(BaseModel):
    ordered_items: int
    input_items_with_waste: int
    purchased_sheets: int
    produced_items: int
    surplus_items: int
    purchased_area_m2: Decimal
    waste: Optional[WasteInput] = None


class TierInfo(BaseModel):
    code: str
    interval: str
    price: Decimal
    price_unit: PriceUnit


class PriceSource(BaseModel):
    origin: Literal["price_list", "manual"]
    label: str
    price_list_id: Optional[str] = None
    price_list_version: Optional[int] = None
    price_list_status: Optional[str] = None
    supplier: Optional[str] = None
    tax_basis: TaxBasis
    reference_only: bool = False


class MaterialCost(BaseModel):
    material_code: Optional[str]
    status: Literal["OK", "BELOW_MIN_ORDER", "NO_TIER", "NO_PRICE_FOR_TIER", "NO_PRICE_LIST", "NEEDS_QUANTITY",
                    "NEEDS_SHEET"]
    status_message: Optional[str] = None
    source: Optional[PriceSource] = None
    tier: Optional[TierInfo] = None
    price_per_m2: Optional[Decimal] = None
    cost_per_sheet: Optional[Money] = None
    cost_per_item: Optional[Money] = None
    total: Optional[Money] = None
    rounding_note: Optional[str] = None


class OptimizationResult(BaseModel):
    category: Literal["STRONG_RECOMMENDATION", "GOOD_OPTIMIZATION", "OPTIONAL", "TIER_TOO_FAR",
                      "NO_APPLICABLE_NEXT_TIER", "MIN_ORDER_REQUIRED"]
    basis: str
    message: str
    current_quantity: int
    proposed_quantity: Optional[int] = None
    current_area_m2: Decimal
    proposed_area_m2: Optional[Decimal] = None
    current_tier: Optional[str] = None
    proposed_tier: Optional[TierInfo] = None
    extra_items: Optional[int] = None
    increase_pct: Optional[Decimal] = None
    increase_band: Optional[str] = None
    current_unit_cost: Optional[Money] = None
    proposed_unit_cost: Optional[Money] = None
    current_total: Optional[Money] = None
    proposed_total: Optional[Money] = None
    total_difference: Optional[Money] = None
    effective_price_extra_items: Optional[Money] = None
    just_below_threshold: Optional[dict[str, Any]] = None


class SalesResult(BaseModel):
    cost_basis_label: str
    cost_per_item: Money
    sales_price_per_item: Money
    profit_per_item: Money
    margin_percent: Optional[Decimal]
    markup_percent: Optional[Decimal]
    total_sales: Optional[Money] = None
    total_profit: Optional[Money] = None
    exchange_rate: Optional[dict[str, Any]] = None
    business_warnings: list[str] = []


class MaterialRecommendation(BaseModel):
    recommended: Optional[str]
    economy: Optional[str]
    upgrade: Optional[str] = None
    risk: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"]
    technical_validation_status: Literal["APPROVED", "NOT_VALIDATED"]
    size_class: Optional[str]
    missing_inputs: list[str]
    reason_codes: list[str]
    alternatives: list[dict[str, Any]] = []


class CalculationResult(BaseModel):
    engine_version: str
    rule_version: str
    rounding_policy: str
    test_data_only: bool
    mode: Mode
    blank: Optional[BlankResult] = None
    layout: Optional[LayoutResult] = None
    quantities: Optional[QuantityResult] = None
    material: MaterialCost
    optimization: Optional[OptimizationResult] = None
    recommendation: Optional[MaterialRecommendation] = None
    sales: Optional[SalesResult] = None
    included_costs: list[str]
    not_included: list[str]
    production_status: str
    defaults_used: dict[str, str]
    missing_inputs: list[str]
    warnings: list[Warning_]
    trace: list[TraceStep]
