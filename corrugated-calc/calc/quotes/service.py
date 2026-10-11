"""Оферти: версии, позиции, неизменими snapshots, палети, търсене и история.

- Всяка продуктова позиция се смята от engine; резултатът и използваните правила се пазят в позицията.
- Финализирането замразява версията (и в базата чрез тригери). Редакция = нова версия.
- Преизчисляване по текущи цени е отделно действие върху чернова.
- Палетите са отделни позиции с отделен резултат; не се добавят към себестойността на кашоните.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import ENGINE_VERSION
from ..db.audit import audit
from ..db.models import Customer, PalletType, Quotation, QuotationLine, QuotationVersion, User
from ..db.repository import build_config
from ..engine.calculate import calculate
from ..engine.config import Config
from ..engine.models import CalculateRequest, CalculationResult
from ..engine.numbers import clean, div, mul, quantize
from ..engine.pricing import convert

COST_BASIS_LABEL = {"material": "Марж спрямо материалната себестойност"}
NEAR_TOLERANCE_PCT = Decimal("5")   # „подобен“: всяко измерение в ±5% (минимум ±10 mm)
NEAR_TOLERANCE_MIN_MM = 10


class QuoteError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _d(v) -> Optional[Decimal]:
    return Decimal(str(v)) if v is not None else None


# ---------------------------------------------------------------- създаване и заглавие

def create_quotation(s: Session, actor: User, customer_id: Optional[int], currency: str,
                     valid_until: Optional[date] = None, delivery_terms: Optional[str] = None,
                     payment_terms: Optional[str] = None, client_notes: Optional[str] = None,
                     internal_notes: Optional[str] = None) -> QuotationVersion:
    if customer_id is not None and s.get(Customer, customer_id) is None:
        raise QuoteError("Няма такъв клиент")
    q = Quotation(number="pending", customer_id=customer_id, created_by=actor.id)
    s.add(q)
    s.flush()
    q.number = f"Q{date.today().year}-{q.id:05d}"
    v = QuotationVersion(quotation_id=q.id, version=1, status="draft", currency=currency, valid_until=valid_until,
                         delivery_terms=delivery_terms, payment_terms=payment_terms, client_notes=client_notes,
                         internal_notes=internal_notes, created_by=actor.id)
    s.add(v)
    s.flush()
    audit(s, actor.id, "quotation.create", "quotation", q.id, new={"number": q.number, "customer_id": customer_id,
                                                                    "currency": currency})
    return v


def get_version(s: Session, quotation_id: int, version: int) -> QuotationVersion:
    v = s.scalar(select(QuotationVersion).where(QuotationVersion.quotation_id == quotation_id,
                                                QuotationVersion.version == version))
    if v is None:
        raise QuoteError("Няма такава версия на оферта")
    return v


def _require_draft(v: QuotationVersion) -> None:
    if v.status != "draft":
        raise QuoteError(f"Версия {v.version} е {v.status} и не се редактира; създайте нова версия.")


HEADER_FIELDS = ("valid_until", "delivery_terms", "payment_terms", "client_notes", "internal_notes", "currency")


def update_header(s: Session, actor: User, v: QuotationVersion, data: dict[str, Any]) -> QuotationVersion:
    _require_draft(v)
    if "currency" in data and data["currency"] != v.currency and v.lines:
        raise QuoteError("Валутата се сменя само на празна оферта; използвайте преизчисляване в нова версия.")
    old = {k: getattr(v, k) for k in HEADER_FIELDS}
    for k, val in data.items():
        if k in HEADER_FIELDS:
            setattr(v, k, val)
    audit(s, actor.id, "quotation.update", "quotation_version", v.id, old=old,
          new={k: getattr(v, k) for k in HEADER_FIELDS})
    return v


# ---------------------------------------------------------------- позиции

def line_snapshot(cfg: Config, req: CalculateRequest, res: CalculationResult) -> dict[str, Any]:
    """Всичко, от което зависи сметката: формула, материал, ценоразпис/праг, курсове, rounding, версии."""
    snap: dict[str, Any] = {
        "engine_version": ENGINE_VERSION, "rule_version": cfg.rule_version,
        "rounding": cfg.settings.rounding.model_dump(mode="json"),
        "settings": cfg.settings.model_dump(mode="json"),
        "formula": res.blank.model_dump(mode="json", include={"fefco", "formula_version", "formula_status", "source",
                                                              "additives", "dimension_basis"}) if res.blank else None,
        "material": None, "price_list": None, "exchange_rates": [r.model_dump(mode="json", by_alias=True)
                                                                   for r in cfg.exchange_rates],
        "machine_rules": None, "route": None, "operation_rates": None,
        "taken_at": _now().isoformat(),
    }
    code = res.material.material_code
    if code and code in cfg.materials:
        snap["material"] = {"code": code, **cfg.materials[code].model_dump(mode="json")}
    src = res.material.source
    if src and src.origin == "price_list":
        pl = next((p for p in cfg.price_lists if p.id == src.price_list_id and p.version == src.price_list_version),
                  None)
        if pl is not None:
            snap["price_list"] = {"id": pl.id, "version": pl.version, "status": pl.status, "supplier": pl.supplier,
                                  "currency": pl.currency, "price_unit": pl.price_unit, "tax_basis": pl.tax_basis,
                                  "valid_from": pl.valid_from, "valid_to": pl.valid_to,
                                  "order_rules": pl.order_rules.model_dump(mode="json"),
                                  "tiers": [t.model_dump(mode="json") for t in pl.tiers],
                                  "prices": {k: {t: str(v) for t, v in p.items()} for k, p in pl.prices.items()
                                             if k == code},
                                  "applied_tier": res.material.tier.model_dump(mode="json") if res.material.tier
                                  else None}
    elif src:
        snap["price_list"] = {"origin": "manual", "label": src.label, "tax_basis": src.tax_basis}
    return snap


def _describe(req: CalculateRequest, res: CalculationResult) -> str:
    code = res.material.material_code or (req.flute or "")
    if res.blank:
        b = req.box
        return (f"Кашон FEFCO {res.blank.fefco}, {b.length_mm}×{b.width_mm}×{b.height_mm} mm "
                f"({'вътрешни' if res.blank.dimension_basis == 'internal' else 'външни'}), {code}").strip(", ")
    sh = req.sheet
    return f"Лист {sh.length_mm}×{sh.width_mm} mm, {code}".strip(", ")


def _calc_line(cfg: Config, v: QuotationVersion, request: dict[str, Any], pricing: dict[str, Any]):
    data = dict(request)
    sales = {k: pricing.get(k) for k in ("sales_price_per_item", "target_margin_pct", "markup_pct")
             if pricing.get(k) is not None}
    if len(sales) != 1:
        raise QuoteError("Задайте точно едно от: продажна цена, целеви марж или markup.")
    data["sales"] = {**sales, "currency": v.currency}
    req = CalculateRequest.model_validate(data)
    if not req.quantity:
        raise QuoteError("Позицията изисква количество.")
    res = calculate(req, cfg)
    if res.material.status != "OK" or res.sales is None:
        raise QuoteError(f"Позицията няма изчислена себестойност ({res.material.status}: "
                         f"{res.material.status_message or ''}). Цена не се измисля.")
    if res.material.source and res.material.source.tax_basis != "excl_vat":
        raise QuoteError("Себестойността е с ДДС; офертата е без ДДС. Не се смесват суми с и без ДДС.")
    return req, res


def _apply_result(line: QuotationLine, cfg: Config, req: CalculateRequest, res: CalculationResult) -> None:
    line.quantity = req.quantity
    exact = res.sales.sales_price_per_item.amount
    if req.sales.sales_price_per_item is None:
        # цена от марж/markup: закръгля се веднъж по rounding policy, за да е общо = ед. цена × количество
        r = cfg.settings.rounding
        line.unit_sales_price = quantize(exact, r.quote_unit_price_places, r.mode)
    else:
        line.unit_sales_price = exact  # въведена от потребителя цена се пази точно
    pricing_note = {"exact_from_pricing": str(clean(exact)), "quoted": str(clean(line.unit_sales_price)),
                    "places": cfg.settings.rounding.quote_unit_price_places}
    line.unit_cost = res.sales.cost_per_item.amount
    line.cost_basis = "material"
    line.request = req.model_dump(mode="json", exclude_none=True)
    line.result = res.model_dump(mode="json")
    line.snapshot = {**line_snapshot(cfg, req, res), "unit_price_rounding": pricing_note}
    if res.blank:
        line.dimension_basis = res.blank.dimension_basis
        line.fefco = res.blank.fefco
        line.length_mm, line.width_mm, line.height_mm = req.box.length_mm, req.box.width_mm, req.box.height_mm
    line.material_code = res.material.material_code
    line.parts = 1


def add_product_line(s: Session, actor: User, v: QuotationVersion, request: dict[str, Any], pricing: dict[str, Any],
                     description: Optional[str] = None) -> QuotationLine:
    _require_draft(v)
    cfg = build_config(s)
    req, res = _calc_line(cfg, v, request, pricing)
    line = QuotationLine(version_id=v.id, position=_next_pos(v), kind="product",
                         description=(description or _describe(req, res))[:500], quantity=req.quantity)
    _apply_result(line, cfg, req, res)
    v.lines.append(line)
    s.flush()
    audit(s, actor.id, "quotation.line_add", "quotation_version", v.id,
          new={"line_id": line.id, "description": line.description, "quantity": line.quantity,
               "unit_sales_price": str(clean(line.unit_sales_price)), "unit_cost": str(clean(line.unit_cost))})
    return line


def add_pallet_line(s: Session, actor: User, v: QuotationVersion, pallet_code: str, count: int,
                    unit_sales_price: Optional[Decimal] = None) -> QuotationLine:
    from ..api.registry import current_pallet_price
    _require_draft(v)
    if count <= 0:
        raise QuoteError("Броят палети трябва да е положителен.")
    pt = s.scalar(select(PalletType).where(PalletType.code == pallet_code, PalletType.active))
    if pt is None:
        raise QuoteError("Няма такъв активен тип палет")
    price = current_pallet_price(pt)
    if price is None:
        raise QuoteError(f"Палет {pallet_code} няма въведена цена; цена не се измисля.")
    if price.tax_basis != "excl_vat":
        raise QuoteError("Палетната цена е с ДДС; офертата е без ДДС.")
    cfg = build_config(s)
    rate = None
    purchase, sales = price.purchase_price, price.sales_price
    if price.currency != v.currency:
        purchase, rate = convert(purchase, price.currency, v.currency, cfg)
        sales, _ = convert(sales, price.currency, v.currency, cfg)
    if unit_sales_price is not None:
        sales = unit_sales_price
    line = QuotationLine(version_id=v.id, position=_next_pos(v), kind="pallet",
                         description=f"Палет {pt.code} {pt.length_mm}×{pt.width_mm} mm — {pt.description}"[:500],
                         quantity=count, unit_sales_price=sales, unit_cost=purchase, cost_basis="pallet_purchase",
                         pallet_type_id=pt.id,
                         snapshot={"pallet_type": {"code": pt.code, "length_mm": pt.length_mm, "width_mm": pt.width_mm,
                                                   "description": pt.description},
                                   "price_record": {"id": price.id, "purchase_price": str(clean(price.purchase_price)),
                                                    "sales_price": str(clean(price.sales_price)),
                                                    "currency": price.currency, "valid_from": price.valid_from.isoformat()},
                                   "sales_price_overridden": unit_sales_price is not None,
                                   "exchange_rate": rate, "taken_at": _now().isoformat()})
    v.lines.append(line)
    s.flush()
    audit(s, actor.id, "quotation.pallet_add", "quotation_version", v.id,
          new={"line_id": line.id, "pallet": pt.code, "count": count, "unit_sales_price": str(clean(sales))})
    return line


def _next_pos(v: QuotationVersion) -> int:
    return max([l.position for l in v.lines] + [0]) + 1


def remove_line(s: Session, actor: User, v: QuotationVersion, line_id: int) -> None:
    _require_draft(v)
    line = next((l for l in v.lines if l.id == line_id), None)
    if line is None:
        raise QuoteError("Няма такава позиция")
    v.lines.remove(line)
    s.flush()
    audit(s, actor.id, "quotation.line_remove", "quotation_version", v.id,
          old={"line_id": line_id, "description": line.description, "quantity": line.quantity})


# ---------------------------------------------------------------- суми

def compute_totals(cfg: Config, v: QuotationVersion) -> dict[str, Any]:
    r = cfg.settings.rounding

    def m(x: Decimal) -> dict[str, str]:
        return {"amount": str(clean(x)), "rounded": str(quantize(x, r.money_places, r.mode))}

    prod_sales = prod_cost = pal_sales = pal_cost = Decimal(0)
    warnings = []
    lines = []
    min_margin = cfg.settings.business_rules.min_margin_pct
    for l in v.lines:
        sales = mul(l.unit_sales_price, Decimal(l.quantity))
        cost = mul(l.unit_cost, Decimal(l.quantity)) if l.unit_cost is not None else Decimal(0)
        if l.kind == "product":
            prod_sales += sales
            prod_cost += cost
        else:
            pal_sales += sales
            pal_cost += cost
        profit = sales - cost
        margin = clean(div(profit * 100, sales)) if sales else None
        if l.kind == "product" and profit < 0:
            warnings.append(f"Позиция {l.position}: продажната цена е под себестойността.")
        elif l.kind == "product" and min_margin is not None and margin is not None and margin < min_margin:
            warnings.append(f"Позиция {l.position}: марж {margin}% под минималния {min_margin}%.")
        lines.append({"line_id": l.id, "position": l.position, "kind": l.kind, "sales_total": m(sales),
                      "cost_total": m(cost), "profit": m(profit), "margin_percent": str(margin) if margin is not None
                      else None})

    def result(sales, cost):
        profit = sales - cost
        return {"sales": m(sales), "cost": m(cost), "profit": m(profit),
                "margin_percent": str(clean(div(profit * 100, sales))) if sales else None,
                "markup_percent": str(clean(div(profit * 100, cost))) if cost else None}

    return {"currency": v.currency, "tax_basis": "excl_vat",
            "products": {**result(prod_sales, prod_cost), "cost_basis_label": COST_BASIS_LABEL["material"]},
            "pallets": result(pal_sales, pal_cost),
            "deal_total": m(prod_sales + pal_sales), "lines": lines, "business_warnings": warnings,
            "rounding_policy": r.policy_version}


# ---------------------------------------------------------------- финализиране, версии, статус

def finalize(s: Session, actor: User, v: QuotationVersion) -> QuotationVersion:
    _require_draft(v)
    if not v.lines:
        raise QuoteError("Офертата няма позиции.")
    if not any(l.kind == "product" for l in v.lines):
        raise QuoteError("Офертата няма продуктова позиция.")
    cfg = build_config(s)
    v.totals = compute_totals(cfg, v)
    v.snapshot = {"engine_version": ENGINE_VERSION, "rule_version": cfg.rule_version,
                  "rounding": cfg.settings.rounding.model_dump(mode="json"), "finalized_at": _now().isoformat(),
                  "lines": [{"line_id": l.id, "kind": l.kind, "has_snapshot": bool(l.snapshot)} for l in v.lines]}
    v.finalized_by = actor.id
    v.finalized_at = _now()
    v.status = "final"
    v.status_changed_at = v.finalized_at
    s.flush()
    audit(s, actor.id, "quotation.finalize", "quotation_version", v.id,
          new={"version": v.version, "deal_total": v.totals["deal_total"]})
    return v


def new_version(s: Session, actor: User, quotation_id: int, from_version: int) -> QuotationVersion:
    """Копие за редакция. Позициите запазват своите изчисления и snapshots (без автоматично преизчисляване)."""
    src = get_version(s, quotation_id, from_version)
    last = s.scalar(select(func.max(QuotationVersion.version)).where(QuotationVersion.quotation_id == quotation_id))
    v = QuotationVersion(quotation_id=quotation_id, version=last + 1, status="draft", currency=src.currency,
                         valid_until=src.valid_until, delivery_terms=src.delivery_terms,
                         payment_terms=src.payment_terms, client_notes=src.client_notes,
                         internal_notes=src.internal_notes, based_on_version=src.version, created_by=actor.id)
    s.add(v)
    s.flush()
    for l in src.lines:
        v.lines.append(QuotationLine(
            version_id=v.id, position=l.position, kind=l.kind, description=l.description, quantity=l.quantity,
            unit_sales_price=l.unit_sales_price, unit_cost=l.unit_cost, cost_basis=l.cost_basis, request=l.request,
            result=l.result, snapshot=l.snapshot, pallet_type_id=l.pallet_type_id, dimension_basis=l.dimension_basis,
            fefco=l.fefco, length_mm=l.length_mm, width_mm=l.width_mm, height_mm=l.height_mm,
            material_code=l.material_code, parts=l.parts))
    s.flush()
    audit(s, actor.id, "quotation.new_version", "quotation", quotation_id,
          new={"version": v.version, "based_on": src.version})
    return v


def recalculate(s: Session, actor: User, v: QuotationVersion) -> list[dict[str, Any]]:
    """Изрично преизчисляване на продуктовите позиции по текущите ценоразписи/правила (само чернова)."""
    _require_draft(v)
    cfg = build_config(s)
    changes = []
    for l in v.lines:
        if l.kind != "product":
            continue
        request = {k: val for k, val in (l.request or {}).items() if k != "sales"}
        pricing = (l.request or {}).get("sales", {})
        old = {"unit_cost": str(clean(l.unit_cost)), "unit_sales_price": str(clean(l.unit_sales_price)),
               "price_list": (l.snapshot or {}).get("price_list", {}) and
               {k: l.snapshot["price_list"].get(k) for k in ("id", "version")}}
        req, res = _calc_line(cfg, v, request, pricing)
        _apply_result(l, cfg, req, res)
        new = {"unit_cost": str(clean(l.unit_cost)), "unit_sales_price": str(clean(l.unit_sales_price)),
               "price_list": l.snapshot.get("price_list") and
               {k: l.snapshot["price_list"].get(k) for k in ("id", "version")}}
        if old != new:
            changes.append({"line_id": l.id, "position": l.position, "old": old, "new": new})
    s.flush()
    audit(s, actor.id, "quotation.recalculate", "quotation_version", v.id, details={"changes": changes})
    return changes


TRANSITIONS = {"draft": {"cancelled"}, "final": {"sent", "accepted", "rejected", "cancelled"},
               "sent": {"accepted", "rejected", "cancelled"}}


def set_status(s: Session, actor: User, v: QuotationVersion, status: str) -> QuotationVersion:
    if status not in TRANSITIONS.get(v.status, set()):
        raise QuoteError(f"Недопустим преход {v.status} → {status}")
    old = v.status
    v.status = status
    v.status_changed_at = _now()
    s.flush()
    audit(s, actor.id, f"quotation.{status}", "quotation_version", v.id, old={"status": old}, new={"status": status})
    return v


# ---------------------------------------------------------------- търсене и история

def _tol(x: int) -> int:
    return max(NEAR_TOLERANCE_MIN_MM, int(x * NEAR_TOLERANCE_PCT / 100))


def search(s: Session, length_mm: int, width_mm: int, height_mm: int, fefco: str = "0201",
           dimension_basis: str = "internal", material_code: Optional[str] = None,
           customer_id: Optional[int] = None, limit: int = 50) -> dict[str, Any]:
    q = (select(QuotationLine, QuotationVersion, Quotation)
         .join(QuotationVersion, QuotationLine.version_id == QuotationVersion.id)
         .join(Quotation, QuotationVersion.quotation_id == Quotation.id)
         .where(QuotationLine.kind == "product", QuotationLine.fefco == fefco,
                QuotationVersion.status != "cancelled",
                QuotationLine.length_mm.between(length_mm - _tol(length_mm), length_mm + _tol(length_mm)),
                QuotationLine.width_mm.between(width_mm - _tol(width_mm), width_mm + _tol(width_mm)),
                QuotationLine.height_mm.between(height_mm - _tol(height_mm), height_mm + _tol(height_mm)))
         .order_by(QuotationVersion.created_at.desc()).limit(limit * 4))
    if customer_id is not None:
        q = q.where(Quotation.customer_id == customer_id)
    exact, near = [], []
    for line, v, quo in s.execute(q):
        diffs = []
        if (line.length_mm, line.width_mm, line.height_mm) != (length_mm, width_mm, height_mm):
            diffs.append(f"размери {line.length_mm}×{line.width_mm}×{line.height_mm}")
        if line.dimension_basis != dimension_basis:
            diffs.append(f"база на размерите: {line.dimension_basis}")
        if material_code and line.material_code != material_code:
            diffs.append(f"материал {line.material_code}")
        if line.parts != 1:
            diffs.append(f"части: {line.parts}")
        item = {"quotation_id": quo.id, "number": quo.number, "version": v.version, "status": v.status,
                "date": (v.finalized_at or v.created_at).isoformat(), "customer_id": quo.customer_id,
                "customer": quo.customer.name if quo.customer else None, "line_id": line.id,
                "description": line.description, "material_code": line.material_code, "quantity": line.quantity,
                "unit_sales_price": str(clean(line.unit_sales_price)), "currency": v.currency,
                "dimensions": [line.length_mm, line.width_mm, line.height_mm], "fefco": line.fefco,
                "dimension_basis": line.dimension_basis, "differences": diffs,
                "note": "Печат и маршрут не се съхраняват в този етап и не са сравнени."}
        if diffs:
            item["match"] = "near"
            item["label"] = "Подобен, не идентичен"
            near.append(item)
        else:
            item["match"] = "exact"
            exact.append(item)
    return {"exact": exact[:limit], "near": near[:limit],
            "tolerance": f"±{NEAR_TOLERANCE_PCT}% (мин. ±{NEAR_TOLERANCE_MIN_MM} mm) за всяко измерение"}


def customer_history(s: Session, customer_id: int, limit: int = 50) -> list[dict[str, Any]]:
    q = (select(QuotationLine, QuotationVersion, Quotation)
         .join(QuotationVersion, QuotationLine.version_id == QuotationVersion.id)
         .join(Quotation, QuotationVersion.quotation_id == Quotation.id)
         .where(Quotation.customer_id == customer_id, QuotationVersion.status.in_(("final", "sent", "accepted")))
         .order_by(QuotationVersion.finalized_at.desc(), QuotationLine.position).limit(limit))
    return [{"number": quo.number, "version": v.version, "status": v.status,
             "date": v.finalized_at.isoformat() if v.finalized_at else None, "kind": l.kind,
             "description": l.description, "material_code": l.material_code, "quantity": l.quantity,
             "unit_sales_price": str(clean(l.unit_sales_price)), "currency": v.currency}
            for l, v, quo in s.execute(q)]
