"""Acceptance тестове от раздел 17 на заданието (тестови данни, не активни цени)."""

from decimal import Decimal as D

import pytest

from calc.engine.calculate import calculate
from calc.engine.config import get_config
from calc.engine.construction import UnsupportedConstruction
from calc.engine.models import CalculateRequest
from calc.engine.pricing import tier_for_area
from calc.engine.sales import compute_sales
from calc.engine.models import Money, SalesInput

BOX = {"length_mm": 1192, "width_mm": 792, "height_mm": 779}


def box(**kw):
    return calculate(CalculateRequest(box=BOX, **kw))


def test_blank_area_volume():
    r = box(material_code="EC3008", quantity=160)
    assert (r.blank.blank_length_mm, r.blank.blank_width_mm) == (4008, 1571)
    assert r.blank.area_m2 == D("6.296568")
    assert r.blank.volume_m3 == D("0.735425856")
    assert r.defaults_used["fefco"] == "0201"


def test_160_first_tier():
    r = box(material_code="EC3008", quantity=160)
    assert r.quantities.purchased_area_m2 == D("1007.450880")
    assert r.material.tier.code == "T1"
    assert r.material.cost_per_item.amount == D("6.67436208")
    assert r.material.total.amount == D("1067.8979328")
    assert r.material.total.rounded == D("1067.90")
    assert r.material.source.reference_only


def test_next_tier_optimization():
    o = box(material_code="EC3008", quantity=160).optimization
    assert o.proposed_quantity == 239
    assert o.extra_items == 79
    assert o.increase_pct == D("49.375")
    assert o.proposed_area_m2 == D("1504.879752")
    assert o.proposed_tier.code == "T2"
    assert o.category == "TIER_TOO_FAR"  # по-висок общ разход, >15%


def test_238_to_239():
    o = box(material_code="EC3008", quantity=238).optimization
    assert o.proposed_quantity == 239
    assert o.current_total.amount == D("1588.49817504")
    assert o.proposed_total.amount == D("1449.199201176")
    assert -o.total_difference.amount == D("139.298973864")
    assert o.category == "STRONG_RECOMMENDATION"
    below = box(material_code="EC3008", quantity=160).optimization.just_below_threshold
    assert below["quantity"] == 238 and below["saving_at_threshold"]["amount"] == "139.298973864"


@pytest.mark.parametrize("area,tier", [
    ("999.999", None), ("1000", "T1"), ("1499.999", "T1"),
    ("1500", "T2"), ("1999.999", "T2"), ("2000", "T2"), ("250000", "T2"),
])
def test_tier_boundaries(area, tier):
    pl = get_config().price_lists[0]
    t = tier_for_area(pl, D(area))
    assert (t.code if t else None) == tier


def test_below_minimum_no_invented_price():
    r = box(material_code="EC3008", quantity=100)
    assert r.material.status == "BELOW_MIN_ORDER"
    assert r.material.total is None
    assert r.optimization.category == "MIN_ORDER_REQUIRED"
    assert r.optimization.proposed_quantity == 159  # 159 × 6.296568 = 1001.15 m²


def test_above_1500_better_price_without_upper_limit():
    r = box(material_code="EC3008", quantity=400)  # 2518.6 m²
    assert r.material.tier.code == "T2" and r.material.tier.interval == "[1500, ∞)"
    assert r.optimization.category == "NO_APPLICABLE_NEXT_TIER"


def _cfg_copy():
    return get_config().model_copy(deep=True)


def test_closed_last_tier_not_assumed():
    cfg = _cfg_copy()
    cfg.price_lists[0].tiers[1].upper_m2 = D("2000")
    r = calculate(CalculateRequest(box=BOX, material_code="EC3008", quantity=400), cfg)
    assert r.material.status == "NO_TIER" and r.material.total is None


def test_material_specific_minimum_2000():
    """Някои материали се поръчват минимум 2000 m²; общият минимум 1000 m² остава за останалите."""
    cfg = _cfg_copy()
    cfg.price_lists[0].order_rules.material_min_order_m2 = {"EC4027": D("2000")}
    r = calculate(CalculateRequest(box=BOX, material_code="EC4027", quantity=160), cfg)  # 1007 m²
    assert r.material.status == "BELOW_MIN_ORDER" and "EC4027" in r.material.status_message
    assert r.material.total is None
    o = r.optimization
    assert o.category == "MIN_ORDER_REQUIRED" and o.proposed_quantity == 318  # 318 × 6.296568 = 2002.31 m²
    assert o.proposed_area_m2 >= D("2000") and D("2000") > 317 * D("6.296568")
    # EC3008 не е засегнат
    assert calculate(CalculateRequest(box=BOX, material_code="EC3008", quantity=160), cfg).material.status == "OK"


def test_missing_tier_price_not_invented():
    r = box(material_code="EC4027", quantity=240)  # T2, няма цена
    assert r.material.status == "NO_PRICE_FOR_TIER"


def test_sheet_only_manual_price():
    r = calculate(CalculateRequest(sheet={"length_mm": 1200, "width_mm": 800},
                                   manual_price={"price": "1060", "unit": "per_1000m2", "currency": "BGN"}))
    assert r.mode == "sheet"
    assert r.layout.sheet_area_m2 == D("0.96")
    assert r.material.cost_per_sheet.amount == D("1.0176")
    assert r.material.source.origin == "manual"
    assert r.blank is None and r.material.material_code is None


def test_sheet_per_sheet_price_base():
    r = calculate(CalculateRequest(sheet={"length_mm": 1000, "width_mm": 500}, quantity=10,
                                   manual_price={"price": "2", "unit": "per_sheet", "currency": "EUR"}))
    assert r.material.price_per_m2 == D("4")
    assert r.material.total.amount == D("20")


def test_items_per_sheet_ceil_and_surplus():
    r = calculate(CalculateRequest(sheet={"length_mm": 1200, "width_mm": 800, "items_per_sheet": 4},
                                   quantity=10,
                                   manual_price={"price": "1060", "unit": "per_1000m2", "currency": "BGN"}))
    q = r.quantities
    assert (q.purchased_sheets, q.produced_items, q.surplus_items) == (3, 12, 2)
    assert q.purchased_area_m2 == D("2.88")
    assert r.material.total.amount == D("3.0528")


def test_items_per_sheet_tiers_use_purchased_area():
    # 3 разгъва в лист 4100×6000 (24.6 m²); прагът се определя по закупените листове
    r = calculate(CalculateRequest(box=BOX, sheet={"length_mm": 6000, "width_mm": 4100, "items_per_sheet": 3},
                                   material_code="EC3008", quantity=160))
    assert r.quantities.purchased_sheets == 54
    assert r.quantities.purchased_area_m2 == D("1328.4")
    assert r.material.tier.code == "T1"
    # layout waste = (S − n × A) / S × 100
    assert r.layout.layout_waste_percent == (D("24.6") - 3 * D("6.296568")) * 100 / D("24.6")
    o = r.optimization
    # първото количество с площ >= 1500: 61 листа (1500.6 m²) -> 181..183 изделия; минимумът е 181
    assert o.proposed_quantity == 181 and o.proposed_area_m2 == D("1500.6")


def test_waste_types():
    base = dict(sheet={"length_mm": 1000, "width_mm": 1000}, quantity=100,
                manual_price={"price": "1", "unit": "per_m2", "currency": "BGN"})
    loss = calculate(CalculateRequest(**base, waste={"rate_pct": "10", "type": "loss_of_input"}))
    add = calculate(CalculateRequest(**base, waste={"rate_pct": "10", "type": "addition_on_net"}))
    assert loss.quantities.input_items_with_waste == 112  # 100 / 0.9 = 111.1 -> 112
    assert add.quantities.input_items_with_waste == 110
    with pytest.raises(ValueError):
        CalculateRequest(**base, waste={"rate_pct": "100", "type": "loss_of_input"})


def test_unknown_flute_direction_still_calculates():
    r = calculate(CalculateRequest(sheet={"length_mm": 1200, "width_mm": 800, "items_per_sheet": 2}, quantity=5,
                                   manual_price={"price": "1", "unit": "per_m2", "currency": "BGN"}))
    assert r.material.total is not None
    assert r.layout.flute_direction_status == "NOT_VERIFIED"
    assert r.layout.layout_status != "APPROVED_SCHEME"
    assert any(w.code == "FLUTE_DIRECTION_NOT_VERIFIED" for w in r.warnings)


def test_unsupported_fefco_does_not_fall_back():
    with pytest.raises(UnsupportedConstruction):
        calculate(CalculateRequest(box={**BOX, "fefco": "0427"}, material_code="EC3008", quantity=160))
    r = calculate(CalculateRequest(box={**BOX, "fefco": "0427", "manual_blank": {"length_mm": 1000,
                                                                                  "width_mm": 1000}},
                                   material_code="EC3008", quantity=1000))
    assert r.blank.source == "manual" and r.blank.area_m2 == D("1")


def test_margin_and_markup_differ():
    cfg = get_config()
    cost = Money(amount=D("100"), currency="BGN", rounded=D("100"))
    s = compute_sales(cfg, SalesInput(target_margin_pct="20"), cost, 10)
    assert s.sales_price_per_item.amount == D("125")
    assert s.margin_percent == D("20") and s.markup_percent == D("25")
    assert s.cost_basis_label == "Марж спрямо материалната себестойност"
    with pytest.raises(ValueError):
        SalesInput(target_margin_pct="100")


def test_sales_in_other_currency():
    r = box(material_code="EC3008", quantity=160, sales={"sales_price_per_item": "4.50", "currency": "EUR"})
    cost_eur = D("6.67436208") / D("1.95583")
    assert abs(r.sales.cost_per_item.amount - cost_eur) < D("1e-20")
    assert r.sales.exchange_rate["source"]
    assert r.sales.profit_per_item.amount == D("4.50") - r.sales.cost_per_item.amount
    assert r.sales.margin_percent is not None and r.sales.markup_percent is not None


def test_flute_only_uses_recommended_not_cheapest():
    r = box(flute="EC", quantity=160)
    assert r.material.material_code == "EC3008"
    assert r.recommendation.recommended == "EC3008"
    assert r.recommendation.economy == "EC4027"
    assert r.recommendation.technical_validation_status == "NOT_VALIDATED"
    assert "XL_UNKNOWN_LOAD" in r.recommendation.reason_codes


def test_no_float_in_engine():
    with pytest.raises(TypeError):
        from calc.engine.numbers import dec
        dec(1.06)


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        CalculateRequest(box={"length_mm": 0, "width_mm": 1, "height_mm": 1})
    with pytest.raises(ValueError):
        CalculateRequest(box=BOX, quantity=-5)
