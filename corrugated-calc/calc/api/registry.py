"""Регистри: материали (технолог), палетни типове и цени, клиенти. Всяка промяна е в audit log."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db.audit import audit
from ..db.models import (Customer, Material, MaterialAlternative, MaterialTag, PalletPrice, PalletType, User)
from ..engine.numbers import clean
from .deps import ALL_ROLES, MATERIAL_EDITORS, PRICE_EDITORS, QUOTERS, current_user, db, require

router = APIRouter(prefix="/api/v1")

TAGS = ("ECONOMY", "STANDARD", "RECOMMENDED", "KRAFT", "HEAVY_DUTY", "SPECIAL")


def _s(v: Optional[Decimal]) -> Optional[str]:
    return str(clean(v)) if v is not None else None


def _no_float(v):
    return str(v) if isinstance(v, float) else v


# ---------------------------------------------------------------- материали

def material_out(m: Material) -> dict[str, Any]:
    return {"code": m.code, "display_code": m.display_code, "factory_code": m.factory_code, "flute": m.flute,
            "layers": m.layers, "paper_combination": m.paper_combination, "kraft_outer": m.kraft_outer,
            "kraft_inner": m.kraft_inner, "grammage_gsm": _s(m.grammage_gsm), "technical": m.technical or [],
            "tags": sorted(t.tag for t in m.tags),
            "alternatives": [{"code": a.alternative_code, "type": a.type, "priority": a.priority,
                              "technical_equivalence": a.technical_equivalence,
                              "equivalence_conditions": a.equivalence_conditions, "approved_by": a.approved_by}
                             for a in sorted(m.alternatives, key=lambda a: a.priority)]}


class TechnicalValue(BaseModel):
    name: str = Field(max_length=32)  # ECT, BCT, DIN ...
    value: Decimal
    unit: str = Field(max_length=32)
    source: str = Field(max_length=200)
    conditions: Optional[str] = Field(default=None, max_length=300)
    _f = field_validator("value", mode="before")(_no_float)


class MaterialIn(BaseModel):
    code: str = Field(min_length=2, max_length=32, pattern=r"^[A-Z0-9]+$")
    display_code: Optional[str] = Field(default=None, max_length=32)
    factory_code: Optional[str] = Field(default=None, max_length=64)
    flute: str = Field(min_length=1, max_length=8, pattern=r"^[A-Z]+$")
    layers: Optional[int] = Field(default=None, gt=0, lt=10)
    paper_combination: Optional[str] = Field(default=None, max_length=200)
    kraft_outer: Optional[bool] = None
    kraft_inner: Optional[bool] = None
    grammage_gsm: Optional[Decimal] = Field(default=None, gt=0)
    technical: list[TechnicalValue] = []
    tags: list[str] = []
    _f = field_validator("grammage_gsm", mode="before")(_no_float)


class MaterialPatch(BaseModel):
    display_code: Optional[str] = Field(default=None, max_length=32)
    factory_code: Optional[str] = Field(default=None, max_length=64)
    flute: Optional[str] = Field(default=None, max_length=8, pattern=r"^[A-Z]+$")
    layers: Optional[int] = Field(default=None, gt=0, lt=10)
    paper_combination: Optional[str] = Field(default=None, max_length=200)
    kraft_outer: Optional[bool] = None
    kraft_inner: Optional[bool] = None
    grammage_gsm: Optional[Decimal] = Field(default=None, gt=0)
    technical: Optional[list[TechnicalValue]] = None
    _f = field_validator("grammage_gsm", mode="before")(_no_float)


def _material(s: Session, code: str) -> Material:
    m = s.get(Material, code)
    if m is None:
        raise HTTPException(404, f"Няма материал {code}")
    return m


def _check_tags(tags: list[str]) -> list[str]:
    bad = [t for t in tags if t not in TAGS]
    if bad:
        raise HTTPException(422, f"Непознати категории {bad}; разрешени: {', '.join(TAGS)}")
    return sorted(set(tags))


def _check_display_unique(s: Session, display: str, code: str) -> None:
    clash = s.scalar(select(Material).where(Material.display_code == display, Material.code != code))
    if clash is not None or (display != code and s.get(Material, display) is not None):
        raise HTTPException(409, f"Display кодът {display} не е еднозначен (вече се използва)")


@router.get("/materials")
def list_materials(s: Session = Depends(db), _: User = Depends(require(*ALL_ROLES))):
    return [material_out(m) for m in s.scalars(select(Material).order_by(Material.code))]


@router.get("/materials/{code}")
def get_material(code: str, s: Session = Depends(db), _: User = Depends(require(*ALL_ROLES))):
    return material_out(_material(s, code))


@router.post("/materials")
def create_material(body: MaterialIn, s: Session = Depends(db), actor: User = Depends(require(*MATERIAL_EDITORS))):
    if s.get(Material, body.code):
        raise HTTPException(409, f"Материал {body.code} вече съществува")
    display = body.display_code or body.code
    _check_display_unique(s, display, body.code)
    m = Material(code=body.code, display_code=display, factory_code=body.factory_code or body.code, flute=body.flute,
                 layers=body.layers, paper_combination=body.paper_combination, kraft_outer=body.kraft_outer,
                 kraft_inner=body.kraft_inner, grammage_gsm=body.grammage_gsm,
                 technical=[t.model_dump(mode="json") for t in body.technical],
                 tags=[MaterialTag(tag=t) for t in _check_tags(body.tags)])
    s.add(m)
    s.flush()
    audit(s, actor.id, "material.create", "material", m.code, new=material_out(m))
    return material_out(m)


@router.patch("/materials/{code}")
def update_material(code: str, body: MaterialPatch, s: Session = Depends(db),
                    actor: User = Depends(require(*MATERIAL_EDITORS))):
    m = _material(s, code)
    old = material_out(m)
    data = body.model_dump(exclude_unset=True)
    if "display_code" in data and data["display_code"]:
        _check_display_unique(s, data["display_code"], code)
    if "technical" in data and data["technical"] is not None:
        data["technical"] = [TechnicalValue(**t).model_dump(mode="json") for t in data["technical"]]
    for k, v in data.items():
        setattr(m, k, v)
    s.flush()
    audit(s, actor.id, "material.update", "material", code, old=old, new=material_out(m))
    return material_out(m)


class TagsIn(BaseModel):
    tags: list[str]


@router.put("/materials/{code}/tags")
def set_tags(code: str, body: TagsIn, s: Session = Depends(db), actor: User = Depends(require(*MATERIAL_EDITORS))):
    m = _material(s, code)
    old = sorted(t.tag for t in m.tags)
    m.tags = [MaterialTag(tag=t) for t in _check_tags(body.tags)]
    s.flush()
    audit(s, actor.id, "material.tags", "material", code, old=old, new=sorted(t.tag for t in m.tags))
    return material_out(m)


class AlternativeIn(BaseModel):
    code: str
    type: Literal["ECONOMY", "UPGRADE", "HEAVY_DUTY", "SIMILAR"]
    priority: int = Field(default=1, ge=1, le=99)
    technical_equivalence: bool = False
    equivalence_conditions: Optional[str] = Field(default=None, max_length=500)


@router.put("/materials/{code}/alternatives")
def set_alternative(code: str, body: AlternativeIn, s: Session = Depends(db),
                    actor: User = Depends(require(*MATERIAL_EDITORS))):
    """Добавя/променя алтернатива. Техническата еквивалентност се одобрява само от технолог."""
    m = _material(s, code)
    _material(s, body.code)
    if body.code == code:
        raise HTTPException(422, "Материалът не може да е алтернатива на себе си")
    if body.technical_equivalence and actor.role != "technologist":
        raise HTTPException(403, "Техническата еквивалентност се одобрява изрично от технолог")
    if body.technical_equivalence and not body.equivalence_conditions:
        raise HTTPException(422, "Посочете условията на техническата еквивалентност")
    alt = next((a for a in m.alternatives if a.alternative_code == body.code), None)
    old = None
    if alt is None:
        alt = MaterialAlternative(alternative_code=body.code)
        m.alternatives.append(alt)
    else:
        old = {"type": alt.type, "priority": alt.priority, "technical_equivalence": alt.technical_equivalence,
               "equivalence_conditions": alt.equivalence_conditions}
    alt.type, alt.priority = body.type, body.priority
    alt.technical_equivalence = body.technical_equivalence
    alt.equivalence_conditions = body.equivalence_conditions
    alt.approved_by = actor.id if body.technical_equivalence else None
    s.flush()
    audit(s, actor.id, "material.alternative", "material", code, old=old, new=body.model_dump())
    return material_out(m)


@router.delete("/materials/{code}/alternatives/{alt_code}")
def delete_alternative(code: str, alt_code: str, s: Session = Depends(db),
                       actor: User = Depends(require(*MATERIAL_EDITORS))):
    m = _material(s, code)
    alt = next((a for a in m.alternatives if a.alternative_code == alt_code), None)
    if alt is None:
        raise HTTPException(404, "Няма такава алтернатива")
    m.alternatives.remove(alt)
    s.flush()
    audit(s, actor.id, "material.alternative_remove", "material", code, old={"code": alt_code, "type": alt.type})
    return material_out(m)


# ---------------------------------------------------------------- палети

def current_pallet_price(pt: PalletType, on: Optional[date] = None) -> Optional[PalletPrice]:
    on = on or date.today()
    valid = [p for p in pt.prices if p.valid_from <= on]
    return max(valid, key=lambda p: (p.valid_from, p.id)) if valid else None


def pallet_out(pt: PalletType, internal: bool = True) -> dict[str, Any]:
    cur = current_pallet_price(pt)
    out = {"id": pt.id, "code": pt.code, "length_mm": pt.length_mm, "width_mm": pt.width_mm,
           "description": pt.description, "active": pt.active,
           "sales_price": _s(cur.sales_price) if cur else None, "currency": cur.currency if cur else None}
    if internal:
        out["purchase_price"] = _s(cur.purchase_price) if cur else None
        out["price_history"] = [{"purchase_price": _s(p.purchase_price), "sales_price": _s(p.sales_price),
                                 "currency": p.currency, "tax_basis": p.tax_basis, "valid_from": p.valid_from,
                                 "created_by": p.created_by} for p in pt.prices]
    return out


class PalletTypeIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    length_mm: int = Field(gt=0)
    width_mm: int = Field(gt=0)
    description: str = Field(min_length=1, max_length=300)


class PalletPriceIn(BaseModel):
    purchase_price: Decimal = Field(ge=0)
    sales_price: Decimal = Field(ge=0)
    currency: Literal["BGN", "EUR"]
    tax_basis: Literal["excl_vat", "incl_vat"] = "excl_vat"
    valid_from: date
    _f = field_validator("purchase_price", "sales_price", mode="before")(_no_float)


@router.get("/pallet-types")
def pallet_types(s: Session = Depends(db), u: User = Depends(current_user)):
    return [pallet_out(p, internal=u.role != "read_only") for p in s.scalars(select(PalletType).order_by(PalletType.code))]


@router.post("/pallet-types")
def create_pallet_type(body: PalletTypeIn, s: Session = Depends(db), actor: User = Depends(require(*PRICE_EDITORS))):
    if s.scalar(select(PalletType).where(PalletType.code == body.code)):
        raise HTTPException(409, "Кодът е зает")
    pt = PalletType(**body.model_dump())
    s.add(pt)
    s.flush()
    audit(s, actor.id, "pallet_type.create", "pallet_type", pt.id, new=body.model_dump())
    return pallet_out(pt)


@router.post("/pallet-types/{code}/prices")
def add_pallet_price(code: str, body: PalletPriceIn, s: Session = Depends(db),
                     actor: User = Depends(require(*PRICE_EDITORS))):
    """Нов запис в историята; старите цени не се презаписват."""
    pt = s.scalar(select(PalletType).where(PalletType.code == code))
    if pt is None:
        raise HTTPException(404, "Няма такъв палет")
    old = pallet_out(pt)
    pt.prices.append(PalletPrice(created_by=actor.id, **body.model_dump()))
    s.flush()
    audit(s, actor.id, "pallet_type.price", "pallet_type", pt.id,
          old={"purchase_price": old["purchase_price"], "sales_price": old["sales_price"]},
          new=body.model_dump(mode="json"))
    return pallet_out(pt)


# ---------------------------------------------------------------- клиенти

def customer_out(c: Customer) -> dict[str, Any]:
    return {"id": c.id, "name": c.name, "vat_number": c.vat_number, "erp_id": c.erp_id,
            "internal_notes": c.internal_notes}


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    vat_number: Optional[str] = Field(default=None, max_length=32)
    erp_id: Optional[str] = Field(default=None, max_length=64)
    internal_notes: Optional[str] = Field(default=None, max_length=2000)


class CustomerPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=300)
    vat_number: Optional[str] = Field(default=None, max_length=32)
    internal_notes: Optional[str] = Field(default=None, max_length=2000)


@router.get("/customers")
def customers(q: Optional[str] = None, s: Session = Depends(db), _: User = Depends(require(*ALL_ROLES))):
    stmt = select(Customer).order_by(Customer.name).limit(100)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Customer.name.ilike(like), Customer.vat_number.ilike(like)))
    return [customer_out(c) for c in s.scalars(stmt)]


@router.post("/customers")
def create_customer(body: CustomerIn, s: Session = Depends(db), actor: User = Depends(require(*QUOTERS))):
    if body.erp_id and s.scalar(select(Customer).where(Customer.erp_id == body.erp_id)):
        raise HTTPException(409, "Клиент с този ERP код съществува")
    if s.scalar(select(func.count()).select_from(Customer).where(func.lower(Customer.name) == body.name.lower())):
        raise HTTPException(409, "Клиент с това име съществува")
    c = Customer(**body.model_dump())
    s.add(c)
    s.flush()
    audit(s, actor.id, "customer.create", "customer", c.id, new=customer_out(c))
    return customer_out(c)


@router.patch("/customers/{customer_id}")
def update_customer(customer_id: int, body: CustomerPatch, s: Session = Depends(db),
                    actor: User = Depends(require(*QUOTERS))):
    c = s.get(Customer, customer_id)
    if c is None:
        raise HTTPException(404, "Няма такъв клиент")
    old = customer_out(c)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    audit(s, actor.id, "customer.update", "customer", c.id, old=old, new=customer_out(c))
    return customer_out(c)
