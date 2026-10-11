"""Оркестратор: един и същ вход/изход за формата, чата и ERP."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .. import ENGINE_VERSION
from .config import Config, get_config
from .construction import compute_blank
from .models import (CalculateRequest, CalculationResult, LayoutResult, QuantityResult, TraceStep, Warning_)
from .numbers import clean, div, mul
from .pricing import Purchase, material_cost, optimize_quantity
from .recommend import candidates_for, pick_primary, recommend
from .sales import compute_sales

NOT_INCLUDED_STAGE1 = ["печат", "щанца", "слотер", "лепене", "ръчна работа", "палетизация", "фолио", "чембер",
                       "транспорт", "матрица", "клише", "setup"]


def calculate(req: CalculateRequest, cfg: Optional[Config] = None) -> CalculationResult:
    cfg = cfg or get_config()
    trace: list[TraceStep] = []
    warnings: list[Warning_] = []
    defaults_used: dict[str, str] = {}
    missing: list[str] = []

    flute = req.flute or (cfg.materials[req.material_code].flute if req.material_code in cfg.materials else None)
    if req.material_code and req.material_code not in cfg.materials and not req.manual_price:
        warnings.append(Warning_(code="UNKNOWN_MATERIAL",
                                 message=f"Материал {req.material_code} не е в регистъра."))

    # --- разгъв
    blank = None
    if req.mode == "box":
        blank = compute_blank(req.box, cfg, flute, trace, defaults_used)
        if blank.formula_status != "confirmed" and blank.source == "formula":
            warnings.append(Warning_(code="FORMULA_NOT_CONFIRMED", severity="info",
                                     message=f"Формула {blank.formula_version}: статус {blank.formula_status}. "
                                             f"Дебелина, биг, слот и trim не са включени."))
        for k in ("environment", "transport", "special_requirements"):
            defaults_used.setdefault(k, cfg.settings.defaults[k])

    # --- лист/разполагане
    layout = None
    if req.sheet:
        s = req.sheet
        sheet_area = div(Decimal(s.length_mm * s.width_mm), Decimal(1_000_000))
        n = s.items_per_sheet or 1
        waste_pct = None
        if blank and s.items_per_sheet:
            used = mul(Decimal(n), blank.area_m2)
            if used > sheet_area:
                raise ValueError(f"{n} разгъва по {blank.area_m2} m² не се събират в лист {clean(sheet_area)} m².")
            waste_pct = clean(div((sheet_area - used) * 100, sheet_area))
            trace.append(TraceStep(step="layout_waste", formula="(S − n × A) / S × 100",
                                   inputs={"S": str(clean(sheet_area)), "n": n, "A": str(blank.area_m2)},
                                   result=str(waste_pct)))
        if s.flute_direction is None:
            fd_status = "NOT_VERIFIED"
            warnings.append(Warning_(code="FLUTE_DIRECTION_NOT_VERIFIED",
                                     message="Посоката на вълната не е проверена; разполагането не е одобрено "
                                             "за производство."))
        else:
            fd_status = "SPECIFIED_NOT_VALIDATED"
        layout_status = ("MANUAL_COUNT_NOT_GEOMETRICALLY_VERIFIED" if s.items_per_sheet and s.layout_source !=
                         "approved_scheme" else ("APPROVED_SCHEME" if s.layout_source else "SHEET_ONLY"))
        layout = LayoutResult(sheet_length_mm=s.length_mm, sheet_width_mm=s.width_mm,
                              sheet_area_m2=clean(sheet_area), items_per_sheet=n,
                              blank_area_m2=blank.area_m2 if blank else None, layout_waste_percent=waste_pct,
                              flute_direction=s.flute_direction, flute_direction_status=fd_status,
                              layout_status=layout_status)
        trace.append(TraceStep(step="sheet_area", formula="S = length_mm × width_mm / 1 000 000",
                               inputs={"length_mm": s.length_mm, "width_mm": s.width_mm},
                               result=str(clean(sheet_area))))
        purchase = Purchase(items_per_sheet=n, sheet_area=sheet_area)
    else:
        # кашон без зададен лист: всеки разгъв е отделен закупен лист
        purchase = Purchase(items_per_sheet=1, sheet_area=blank.area_m2)
        warnings.append(Warning_(code="FLUTE_DIRECTION_NOT_VERIFIED", severity="info",
                                 message="Няма зададен лист и посока на вълната; изчислено по площ на разгъва."))

    if req.waste:
        purchase.waste_rate = div(req.waste.rate_pct, Decimal(100))
        purchase.waste_type = req.waste.type
        trace.append(TraceStep(step="waste", formula=("input = ceil(q / (1 − r))" if req.waste.type ==
                                                      "loss_of_input" else "input = ceil(q × (1 + r))"),
                               inputs={"r_pct": str(req.waste.rate_pct), "source": req.waste.source}))

    # --- количества
    quantities = None
    q = req.quantity
    if q:
        sheets = purchase.sheets(q)
        quantities = QuantityResult(ordered_items=q, input_items_with_waste=purchase.input_items(q),
                                    purchased_sheets=sheets, produced_items=sheets * purchase.items_per_sheet,
                                    purchased_area_m2=clean(purchase.area(q)),
                                    surplus_items=sheets * purchase.items_per_sheet - q, waste=req.waste)
        trace.append(TraceStep(step="purchased", formula="sheets = ceil(input_items / items_per_sheet); "
                                                         "area = sheets × S",
                               result={"sheets": sheets, "area_m2": str(quantities.purchased_area_m2)}))
    else:
        missing.append("quantity")

    # --- материал
    primary = req.material_code
    if not primary and not req.manual_price and flute:
        primary = pick_primary(cfg, flute)
        if primary:
            trace.append(TraceStep(step="material_selection", result=primary,
                                   source="material_tags: RECOMMENDED/STANDARD за вълната"))
    if not primary and not req.manual_price:
        missing.append("material_code или manual_price")

    is_sheet_only = req.mode == "sheet"
    mat = material_cost(cfg, primary, purchase, q, req.manual_price, req.price_list_id, is_sheet_only)
    if mat.source and mat.source.reference_only:
        warnings.append(Warning_(code="REFERENCE_PRICE",
                                 message="Цената е от reference/тестов ценоразпис, не от активен."))
    if mat.status not in ("OK",):
        warnings.append(Warning_(code=mat.status, message=mat.status_message or mat.status))
    if mat.tier:
        trace.append(TraceStep(step="tier", inputs={"purchased_area_m2": str(quantities.purchased_area_m2)},
                               result={"tier": mat.tier.code, "interval": mat.tier.interval,
                                       "price": str(mat.tier.price), "unit": mat.tier.price_unit},
                               source=mat.source.label))

    # --- оптимизация и препоръка
    optimization = None
    if q and primary and not req.manual_price:
        optimization = optimize_quantity(cfg, primary, purchase, q, req.price_list_id)

    recommendation = None
    if req.mode == "box" and not req.manual_price and (primary or flute):
        costs = {}
        for code in dict.fromkeys(([primary] if primary else []) + candidates_for(cfg, flute, primary)):
            costs[code] = mat if code == primary else material_cost(cfg, code, purchase, q, None,
                                                                    req.price_list_id, False)
        recommendation = recommend(cfg, req, primary, blank.volume_m3 if blank else None, costs)
        if "XL_UNKNOWN_LOAD" in recommendation.reason_codes:
            warnings.append(Warning_(code="XL_UNKNOWN_LOAD", message="XL кашон с неизвестен товар."))

    # --- продажна цена
    sales = None
    if req.sales:
        if mat.cost_per_item:
            sales = compute_sales(cfg, req.sales, mat.cost_per_item, q, "material")
        elif mat.cost_per_sheet and is_sheet_only:
            sales = compute_sales(cfg, req.sales, mat.cost_per_sheet, q, "material")
        else:
            warnings.append(Warning_(code="SALES_WITHOUT_COST",
                                     message="Няма изчислена себестойност; марж не може да се определи."))

    return CalculationResult(
        engine_version=ENGINE_VERSION, rule_version=cfg.rule_version,
        rounding_policy=cfg.settings.rounding.policy_version, test_data_only=cfg.test_data_only, mode=req.mode,
        blank=blank, layout=layout, quantities=quantities, material=mat, optimization=optimization,
        recommendation=recommendation, sales=sales, included_costs=["материал"] if mat.status == "OK" else [],
        not_included=NOT_INCLUDED_STAGE1, production_status="NOT_CHECKED", defaults_used=defaults_used,
        missing_inputs=missing, warnings=warnings, trace=trace)
