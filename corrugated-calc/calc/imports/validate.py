"""Проверка на предложението преди одобрение.

Грешките блокират одобрението. Предупрежденията (напр. необичайна промяна) изискват изрично потвърждение.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from .proposal import NEEDS_REVIEW, PField, Proposal

CURRENCIES = {"BGN", "EUR"}
UNITS = {"per_m2", "per_1000m2", "per_sheet"}
TAX = {"excl_vat", "incl_vat"}
OUTLIER_FACTOR = 3


def _issue(code: str, message: str, ref: Optional[dict] = None) -> dict[str, Any]:
    return {"code": code, "message": message, "ref": ref or {}}


def _dec(v: Optional[str]) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def validate_proposal(p: Proposal, known_materials: set[str],
                      previous: Optional[dict[str, Any]] = None,
                      unusual_change_pct: Decimal = Decimal("15")) -> dict[str, Any]:
    """previous: {"version": n, "currency", "price_unit", "tax_basis", "prices": {(material, lower, upper): price}}"""
    errors: list[dict] = []
    warnings: list[dict] = []

    def need(field: str, f: PField, allowed: Optional[set] = None, label: str = ""):
        if f.status in NEEDS_REVIEW:
            errors.append(_issue("NEEDS_REVIEW", f"{label}: стойността е за уточнение ({f.note or f.status}).",
                                 {"field": field}))
        elif f.value is None:
            errors.append(_issue("MISSING", f"{label}: липсва.", {"field": field}))
        elif allowed and f.value not in allowed:
            errors.append(_issue("INVALID", f"{label}: недопустима стойност „{f.value}“.", {"field": field}))

    need("price_list_code", p.price_list_code, label="Код на ценоразписа")
    need("supplier", p.supplier, label="Доставчик")
    need("currency", p.currency, CURRENCIES, "Валута")
    need("price_unit", p.price_unit, UNITS, "Ценова база")
    need("tax_basis", p.tax_basis, TAX, "Данъчна база")
    need("valid_from", p.valid_from, label="Валиден от")

    vf = vt = None
    try:
        vf = date.fromisoformat(p.valid_from.value) if p.valid_from.value else None
        vt = date.fromisoformat(p.valid_to.value) if p.valid_to.value else None
    except ValueError:
        errors.append(_issue("INVALID_DATE", "Невалидна дата на валидност.", {"field": "valid_from"}))
    if vf and vt and vt < vf:
        errors.append(_issue("VALIDITY", "„Валиден до“ е преди „Валиден от“.", {"field": "valid_to"}))
    for fname in ("valid_to", "min_order_m2", "min_invoiced_m2"):
        f = getattr(p, fname)
        if f.status in NEEDS_REVIEW:
            errors.append(_issue("NEEDS_REVIEW", f"{fname}: стойността е за уточнение.", {"field": fname}))
    for fname in ("min_order_m2", "min_invoiced_m2"):
        v = getattr(p, fname).value
        if v is not None and (_dec(v) is None or _dec(v) < 0):
            errors.append(_issue("INVALID", f"{fname}: невалидно число.", {"field": fname}))

    # --- прагове
    if not p.tiers:
        errors.append(_issue("NO_TIERS", "Няма ценови прагове."))
    codes = [t.code for t in p.tiers]
    if len(codes) != len(set(codes)):
        errors.append(_issue("DUPLICATE_TIER", "Дублирани кодове на прагове."))
    parsed = []
    for t in p.tiers:
        lo, up = _dec(t.lower.value), _dec(t.upper.value)
        if lo is None or lo < 0 or t.lower.status in NEEDS_REVIEW:
            errors.append(_issue("TIER_BOUND", f"{t.code}: невалидна долна граница.", {"tier": t.code}))
            continue
        if t.upper.value is not None and (up is None or up <= lo):
            errors.append(_issue("TIER_BOUND", f"{t.code}: горната граница трябва да е над долната.",
                                 {"tier": t.code}))
            continue
        if not t.bounds_confirmed:
            errors.append(_issue("TIER_BOUNDS_UNCONFIRMED",
                                 f"{t.code}: потвърдете включването на границите по договора "
                                 f"({'[' if t.lower_inclusive else '('}{lo}, {up if up is not None else '∞'}"
                                 f"{']' if t.upper_inclusive else ')'}).", {"tier": t.code}))
        parsed.append((t, lo, up))
    parsed.sort(key=lambda x: x[1])
    for (a, alo, aup), (b, blo, bup) in zip(parsed, parsed[1:]):
        if aup is None or blo < aup or (blo == aup and a.upper_inclusive and b.lower_inclusive):
            errors.append(_issue("TIER_OVERLAP", f"{a.code} и {b.code} се припокриват.", {"tier": b.code}))
        elif blo > aup or (blo == aup and not a.upper_inclusive and not b.lower_inclusive):
            warnings.append(_issue("TIER_GAP", f"Между {a.code} и {b.code} има непокрит интервал; "
                                               f"за него няма да има цена.", {"tier": b.code}))
    min_order = _dec(p.min_order_m2.value)
    if parsed and min_order is not None and parsed[0][1] > min_order:
        warnings.append(_issue("MIN_ORDER_BELOW_FIRST_TIER",
                               f"Минималната заявка {min_order} m² е под първия праг {parsed[0][1]} m²."))

    # --- редове
    seen: dict[str, str] = {}
    tier_codes = set(codes)
    all_vals: list[tuple[str, str, Decimal]] = []
    for r in p.rows:
        m = r.material
        ref = {"row_id": r.id}
        if m.status in NEEDS_REVIEW or m.value is None:
            errors.append(_issue("MATERIAL_REVIEW", f"Ред {r.id}: кодът на материала е за уточнение "
                                                    f"(„{r.material_raw or ''}“).", ref))
        elif m.value not in known_materials:
            errors.append(_issue("UNKNOWN_MATERIAL", f"Ред {r.id}: {m.value} не е в регистъра на материалите; "
                                                     f"добавете го (технолог) или коригирайте кода.", ref))
        elif m.value in seen:
            errors.append(_issue("DUPLICATE_MATERIAL", f"{m.value} се среща в {seen[m.value]} и {r.id}.", ref))
        else:
            seen[m.value] = r.id
        has_price = False
        for tcode, f in r.prices.items():
            pref = {"row_id": r.id, "tier": tcode}
            if tcode not in tier_codes:
                errors.append(_issue("UNKNOWN_TIER", f"Ред {r.id}: цена за несъществуващ праг {tcode}.", pref))
                continue
            if f.status in NEEDS_REVIEW:
                errors.append(_issue("PRICE_REVIEW", f"Ред {r.id}/{tcode}: стойността е за уточнение "
                                                     f"(„{f.raw}“{'; ' + f.note if f.note else ''}).", pref))
                continue
            if f.value is None:
                continue
            v = _dec(f.value)
            if v is None or v <= 0:
                errors.append(_issue("PRICE_INVALID", f"Ред {r.id}/{tcode}: цената трябва да е положително число.",
                                     pref))
                continue
            has_price = True
        if not has_price:
            warnings.append(_issue("ROW_WITHOUT_PRICES", f"Ред {r.id} ({m.value}) няма нито една цена.", ref))
        # правдоподобност в рамките на документа (не блокира, изисква потвърждение)
        row_vals = [(t.code, _dec(r.prices[t.code].value)) for t, _, _ in parsed if t.code in r.prices]
        row_vals = [(t, v) for t, v in row_vals if v is not None and v > 0]
        for (ta, va), (tb, vb) in zip(row_vals, row_vals[1:]):
            if vb > va:
                warnings.append(_issue("TIER_PRICE_INCREASES", f"{m.value}: цената за {tb} ({vb}) е по-висока "
                                                               f"от {ta} ({va}).", {"row_id": r.id, "tier": tb}))
        all_vals.extend((r.id, t, v) for t, v in row_vals)
    if len(all_vals) >= 2:
        ordered = sorted(v for _, _, v in all_vals)
        median = ordered[len(ordered) // 2]
        for row_id, t, v in all_vals:
            if v * OUTLIER_FACTOR < median or v > median * OUTLIER_FACTOR:
                warnings.append(_issue("PRICE_OUTLIER", f"Ред {row_id}/{t}: {v} се отличава повече от "
                                                        f"{OUTLIER_FACTOR}× от медианата {median} в документа.",
                                       {"row_id": row_id, "tier": t}))
        missing = [t for t in codes if t not in r.prices or r.prices[t].value is None]
        if has_price and missing:
            warnings.append(_issue("MISSING_TIER_PRICES", f"{m.value}: няма цена за {', '.join(missing)}; "
                                                          f"за тези прагове няма да се смята цена.", ref))
    if not p.rows:
        errors.append(_issue("NO_ROWS", "Няма редове с цени."))

    # --- спрямо предходната версия
    if previous:
        same_basis = (previous["currency"] == p.currency.value and previous["price_unit"] == p.price_unit.value
                      and previous["tax_basis"] == p.tax_basis.value)
        if not same_basis:
            warnings.append(_issue("BASIS_CHANGED",
                                   f"Валутата/базата/ДДС се различават от v{previous['version']}; "
                                   f"цените не са сравнени автоматично."))
        else:
            tiers_by_code = {t.code: (t.lower.value, t.upper.value) for t in p.tiers}
            for r in p.rows:
                for tcode, f in r.prices.items():
                    key = (r.material.value, *tiers_by_code.get(tcode, (None, None)))
                    old = previous["prices"].get(key)
                    new = _dec(f.value)
                    if old is None or new is None or old == 0:
                        continue
                    pct = (new - old) * 100 / old
                    if abs(pct) > unusual_change_pct:
                        warnings.append(_issue("UNUSUAL_CHANGE",
                                               f"{r.material.value}/{tcode}: {old} → {new} "
                                               f"({pct.quantize(Decimal('0.1'))}%) спрямо v{previous['version']}.",
                                               {"row_id": r.id, "tier": tcode, "old": str(old), "new": str(new),
                                                "pct": str(pct.quantize(Decimal('0.01')))}))

    return {"ok": not errors, "errors": errors, "warnings": warnings, "digest": p.digest()}
