"""Ценоразписи, прагове, валути, материална цена и количествена оптимизация."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .config import Config, Currency, PriceList, PriceUnit, Tier
from .models import ManualPrice, MaterialCost, Money, OptimizationResult, PriceSource, TierInfo
from .numbers import ceil_int, clean, div, mul, quantize


# ---------------------------------------------------------------- пари и валути

def money(amount: Decimal, currency: Currency, cfg: Config) -> Money:
    r = cfg.settings.rounding
    return Money(amount=clean(amount), currency=currency, rounded=quantize(amount, r.money_places, r.mode))


def find_rate(cfg: Config, src: Currency, dst: Currency) -> Optional[tuple[Decimal, dict]]:
    if src == dst:
        return Decimal(1), {}
    for r in cfg.exchange_rates:
        if r.from_ == src and r.to == dst:
            return r.rate, {"from": src, "to": dst, "rate": str(r.rate), "date": r.date, "source": r.source}
        if r.from_ == dst and r.to == src:
            return div(Decimal(1), r.rate), {"from": src, "to": dst, "rate": f"1/{r.rate}", "date": r.date,
                                             "source": r.source}
    return None


def convert(amount: Decimal, src: Currency, dst: Currency, cfg: Config) -> tuple[Decimal, dict]:
    found = find_rate(cfg, src, dst)
    if found is None:
        raise ValueError(f"Няма конфигуриран курс {src}→{dst}")
    rate, info = found
    return mul(amount, rate), info


# ---------------------------------------------------------------- прагове

def tier_for_area(pl: PriceList, area: Decimal) -> Optional[Tier]:
    for t in pl.tiers:
        if t.interval.contains(area):
            return t
    return None


def price_per_m2(price: Decimal, unit: PriceUnit, sheet_area: Optional[Decimal]) -> Optional[Decimal]:
    if unit == "per_m2":
        return price
    if unit == "per_1000m2":
        return div(price, Decimal(1000))
    if sheet_area:
        return div(price, sheet_area)
    return None


def cost_for(price: Decimal, unit: PriceUnit, area: Decimal, sheets: int) -> Decimal:
    if unit == "per_sheet":
        return mul(price, Decimal(sheets))
    return mul(area, price_per_m2(price, unit, None))


# ---------------------------------------------------------------- количества

@dataclass
class Purchase:
    """Функция за закупуване: готови изделия -> входни изделия, листове, площ."""

    items_per_sheet: int
    sheet_area: Decimal
    waste_rate: Optional[Decimal] = None  # дроб, напр. 0.03
    waste_type: Optional[str] = None

    def input_items(self, q: int) -> int:
        if not self.waste_rate:
            return q
        if self.waste_type == "loss_of_input":
            return ceil_int(div(Decimal(q), Decimal(1) - self.waste_rate))
        return ceil_int(mul(Decimal(q), Decimal(1) + self.waste_rate))

    def sheets(self, q: int) -> int:
        return ceil_int(div(Decimal(self.input_items(q)), Decimal(self.items_per_sheet)))

    def area(self, q: int) -> Decimal:
        return mul(Decimal(self.sheets(q)), self.sheet_area)


def min_quantity_reaching(purchase: Purchase, tier: Tier, start: int) -> int:
    """Най-малкото цяло количество изделия >= start, чиято закупувана площ достига долната граница на прага."""

    def reaches(q: int) -> bool:
        a = purchase.area(q)
        return a > tier.lower_m2 or (a == tier.lower_m2 and tier.lower_inclusive)

    lo, hi = start, max(start, 1)
    while not reaches(hi):
        lo, hi = hi + 1, hi * 2
    while lo < hi:
        mid = (lo + hi) // 2
        if reaches(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


# ---------------------------------------------------------------- материална цена

@dataclass
class PricedPoint:
    status: str
    message: Optional[str]
    tier: Optional[Tier]
    price: Optional[Decimal]
    total: Optional[Decimal]


def price_point(pl: PriceList, code: str, area: Decimal, sheets: int) -> PricedPoint:
    min_order = pl.order_rules.min_order_m2
    if min_order is not None and area < min_order:
        return PricedPoint("BELOW_MIN_ORDER",
                           f"Закупуваната площ {clean(area)} m² е под минималната заявка {min_order} m²; "
                           f"цена под минималния праг не се измисля.", None, None, None)
    tier = tier_for_area(pl, area)
    if tier is None:
        return PricedPoint("NO_TIER", f"Няма конфигуриран праг за {clean(area)} m² в {pl.id} v{pl.version}; "
                                      f"задайте поведението по договора.", None, None, None)
    price = pl.prices.get(code, {}).get(tier.code)
    if price is None:
        return PricedPoint("NO_PRICE_FOR_TIER", f"{code} няма цена за праг {tier.code} {tier.interval.describe()}.",
                           tier, None, None)
    return PricedPoint("OK", None, tier, price, cost_for(price, pl.price_unit, area, sheets))


def tier_info(tier: Tier, price: Decimal, unit: PriceUnit) -> TierInfo:
    return TierInfo(code=tier.code, interval=tier.interval.describe(), price=price, price_unit=unit)


def source_for_list(pl: PriceList) -> PriceSource:
    return PriceSource(origin="price_list", label=f"{pl.supplier} · {pl.id} v{pl.version} ({pl.status})",
                       price_list_id=pl.id, price_list_version=pl.version, price_list_status=pl.status,
                       supplier=pl.supplier, tax_basis=pl.tax_basis, reference_only=pl.status == "reference")


def manual_source(mp: ManualPrice) -> PriceSource:
    return PriceSource(origin="manual", label="Ръчно въведена цена", tax_basis=mp.tax_basis)


def material_cost(cfg: Config, code: Optional[str], purchase: Purchase, quantity: Optional[int],
                  manual: Optional[ManualPrice], price_list_id: Optional[str],
                  is_sheet_only: bool) -> MaterialCost:
    """Материална себестойност. quantity е брой готови изделия (листове в sheet режим)."""
    sheet_area = purchase.sheet_area

    if manual:
        p_m2 = price_per_m2(manual.price, manual.unit, sheet_area)
        per_sheet = mul(sheet_area, p_m2) if manual.unit != "per_sheet" else manual.price
        res = MaterialCost(material_code=code, status="OK", source=manual_source(manual), price_per_m2=clean(p_m2),
                           cost_per_sheet=money(per_sheet, manual.currency, cfg))
        if quantity:
            sheets = purchase.sheets(quantity)
            total = cost_for(manual.price, manual.unit, purchase.area(quantity), sheets)
            _fill_totals(res, total, quantity, manual.currency, cfg)
        elif not is_sheet_only:
            res.status = "NEEDS_QUANTITY"
            res.status_message = "Задайте количество за обща стойност."
        return res

    if not code:
        return MaterialCost(material_code=None, status="NO_PRICE_LIST",
                            status_message="Няма избран материал или ръчна цена.")
    pl = cfg.price_list_for(code, price_list_id)
    if pl is None:
        return MaterialCost(material_code=code, status="NO_PRICE_LIST",
                            status_message=f"Няма използваем ценоразпис за {code}; цена не се измисля.")
    if not quantity:
        return MaterialCost(material_code=code, status="NEEDS_QUANTITY", source=source_for_list(pl),
                            status_message="Прагът зависи от закупуваната площ; задайте количество.")
    area, sheets = purchase.area(quantity), purchase.sheets(quantity)
    pt = price_point(pl, code, area, sheets)
    res = MaterialCost(material_code=code, status=pt.status, status_message=pt.message, source=source_for_list(pl))
    if pt.status == "OK":
        res.tier = tier_info(pt.tier, pt.price, pl.price_unit)
        p_m2 = price_per_m2(pt.price, pl.price_unit, sheet_area)
        res.price_per_m2 = clean(p_m2)
        res.cost_per_sheet = money(mul(sheet_area, p_m2), pl.currency, cfg)
        _fill_totals(res, pt.total, quantity, pl.currency, cfg)
    return res


def _fill_totals(res: MaterialCost, total: Decimal, quantity: int, currency: Currency, cfg: Config) -> None:
    unit = div(total, Decimal(quantity))
    res.total = money(total, currency, cfg)
    res.cost_per_item = money(unit, currency, cfg)
    shown = res.cost_per_item.rounded * quantity
    if shown != res.total.rounded:
        res.rounding_note = (f"Общата стойност е от незакръглени стойности: {res.total.rounded}; "
                             f"закръглена единична × количество = {shown}.")


# ---------------------------------------------------------------- количествена оптимизация

def _band(pct: Decimal, bands: list[Decimal]) -> str:
    prev = Decimal(0)
    for b in bands:
        if pct <= b:
            return f"{prev}–{b}%"
        prev = b
    return f">{prev}%"


def optimize_quantity(cfg: Config, code: str, purchase: Purchase, quantity: int,
                      price_list_id: Optional[str]) -> Optional[OptimizationResult]:
    pl = cfg.price_list_for(code, price_list_id)
    if pl is None:
        return None
    s = cfg.settings.quantity_optimization
    cur_area = purchase.area(quantity)
    cur = price_point(pl, code, cur_area, purchase.sheets(quantity))
    tiers = sorted(pl.tiers, key=lambda t: t.lower_m2)
    basis = "Материал с материал (закупувана площ)"

    if cur.status == "BELOW_MIN_ORDER":
        target = next((t for t in tiers if pl.prices.get(code, {}).get(t.code) is not None), None)
        if target is None:
            return None
        q = min_quantity_reaching(purchase, target, quantity)
        nxt = price_point(pl, code, purchase.area(q), purchase.sheets(q))
        return OptimizationResult(
            category="MIN_ORDER_REQUIRED", basis=basis,
            message=f"Минималната заявка се достига при {q} бр. ({clean(purchase.area(q))} m²).",
            current_quantity=quantity, proposed_quantity=q, current_area_m2=clean(cur_area),
            proposed_area_m2=clean(purchase.area(q)),
            proposed_tier=tier_info(nxt.tier, nxt.price, pl.price_unit) if nxt.status == "OK" else None,
            extra_items=q - quantity,
            proposed_total=money(nxt.total, pl.currency, cfg) if nxt.total is not None else None)

    if cur.status != "OK":
        return None

    later = [t for t in tiers if t.lower_m2 >= (cur.tier.upper_m2 or cur.tier.lower_m2) and t.code != cur.tier.code]
    target = None
    for t in later:
        p = pl.prices.get(code, {}).get(t.code)
        if p is not None and p < cur.price:
            target = t
            break
    if target is None:
        return OptimizationResult(category="NO_APPLICABLE_NEXT_TIER", basis=basis,
                                  message="Няма конфигуриран следващ по-нисък ценови праг.",
                                  current_quantity=quantity, current_area_m2=clean(cur_area),
                                  current_tier=cur.tier.code)

    q = min_quantity_reaching(purchase, target, quantity + 1)
    nxt_area = purchase.area(q)
    nxt = price_point(pl, code, nxt_area, purchase.sheets(q))
    if nxt.status != "OK" or nxt.tier.code != target.code:
        return OptimizationResult(category="NO_APPLICABLE_NEXT_TIER", basis=basis,
                                  message="Следващият праг не може да бъде достигнат с цели изделия/листове.",
                                  current_quantity=quantity, current_area_m2=clean(cur_area),
                                  current_tier=cur.tier.code)

    extra = q - quantity
    inc_pct = div(Decimal(extra) * 100, Decimal(quantity))
    diff = nxt.total - cur.total
    eff = div(diff, Decimal(extra))
    cur_unit = div(cur.total, Decimal(quantity))
    nxt_unit = div(nxt.total, Decimal(q))

    if nxt.total < cur.total:
        cat, msg = "STRONG_RECOMMENDATION", "По-ниска обща стойност на материала при повече бройки."
    elif inc_pct > s.max_increase_pct:
        cat, msg = "TIER_TOO_FAR", f"Нужно е увеличение {clean(inc_pct)}% (> {s.max_increase_pct}%)."
    elif eff <= mul(cur_unit, s.good_effective_price_ratio):
        cat, msg = "GOOD_OPTIMIZATION", "Ефективната цена на допълнителните бройки е в приемливия праг."
    else:
        cat, msg = "OPTIONAL", "Възможно, но без ясна икономия."

    below = None
    if q - 1 >= quantity and q - 1 > 0:
        b_area = purchase.area(q - 1)
        b = price_point(pl, code, b_area, purchase.sheets(q - 1))
        if b.status == "OK":
            below = {"quantity": q - 1, "area_m2": str(clean(b_area)), "tier": b.tier.code,
                     "total": money(b.total, pl.currency, cfg).model_dump(mode="json"),
                     "saving_at_threshold": money(b.total - nxt.total, pl.currency, cfg).model_dump(mode="json")}

    return OptimizationResult(
        category=cat, basis=basis, message=msg, current_quantity=quantity, proposed_quantity=q,
        current_area_m2=clean(cur_area), proposed_area_m2=clean(nxt_area), current_tier=cur.tier.code,
        proposed_tier=tier_info(nxt.tier, nxt.price, pl.price_unit), extra_items=extra,
        increase_pct=clean(inc_pct), increase_band=_band(inc_pct, s.increase_bands_pct),
        current_unit_cost=money(cur_unit, pl.currency, cfg), proposed_unit_cost=money(nxt_unit, pl.currency, cfg),
        current_total=money(cur.total, pl.currency, cfg), proposed_total=money(nxt.total, pl.currency, cfg),
        total_difference=money(diff, pl.currency, cfg), effective_price_extra_items=money(eff, pl.currency, cfg),
        just_below_threshold=below)
