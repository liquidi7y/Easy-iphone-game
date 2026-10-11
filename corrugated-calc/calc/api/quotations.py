"""API за оферти: версии, позиции, финализиране, търсене, клиентски изход."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Material, Quotation, QuotationVersion, User
from ..db.repository import build_config
from ..engine.numbers import clean
from ..quotes import service as qs
from ..quotes.client import build_client_quotation, render_html
from .deps import ALL_ROLES, QUOTERS, db, require

router = APIRouter()


def _wf(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except qs.QuoteError as e:
        raise HTTPException(422, str(e))
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False))
    except ValueError as e:
        raise HTTPException(422, str(e))


def _quotation(s: Session, qid: int) -> Quotation:
    q = s.get(Quotation, qid)
    if q is None:
        raise HTTPException(404, "Няма такава оферта")
    return q


def _version(s: Session, qid: int, ver: int) -> QuotationVersion:
    return _wf(qs.get_version, s, qid, ver)


def _s(x: Optional[Decimal]) -> Optional[str]:
    return str(clean(x)) if x is not None else None


def version_out(s: Session, q: Quotation, v: QuotationVersion) -> dict[str, Any]:
    """Вътрешен изглед: включва себестойност, марж и вътрешни бележки."""
    totals = v.totals if v.status != "draft" else qs.compute_totals(build_config(s), v)
    return {
        "quotation_id": q.id, "number": q.number, "customer": {"id": q.customer.id, "name": q.customer.name,
                                                               "internal_notes": q.customer.internal_notes}
        if q.customer else None,
        "version": v.version, "status": v.status, "currency": v.currency, "valid_until": v.valid_until,
        "delivery_terms": v.delivery_terms, "payment_terms": v.payment_terms, "client_notes": v.client_notes,
        "internal_notes": v.internal_notes, "based_on_version": v.based_on_version, "created_by": v.created_by,
        "created_at": v.created_at, "finalized_by": v.finalized_by, "finalized_at": v.finalized_at,
        "versions": [{"version": x.version, "status": x.status} for x in q.versions],
        "totals": totals, "snapshot": v.snapshot,
        "lines": [{"id": l.id, "position": l.position, "kind": l.kind, "description": l.description,
                   "quantity": l.quantity, "unit_sales_price": _s(l.unit_sales_price), "unit_cost": _s(l.unit_cost),
                   "cost_basis": l.cost_basis, "material_code": l.material_code, "request": l.request,
                   "result": l.result, "snapshot": l.snapshot} for l in v.lines],
    }


# ---------------------------------------------------------------- оферти

class NewQuotation(BaseModel):
    customer_id: Optional[int] = None
    currency: Literal["BGN", "EUR"] = "BGN"
    valid_until: Optional[date] = None
    delivery_terms: Optional[str] = Field(default=None, max_length=2000)
    payment_terms: Optional[str] = Field(default=None, max_length=2000)
    client_notes: Optional[str] = Field(default=None, max_length=4000)
    internal_notes: Optional[str] = Field(default=None, max_length=4000)


@router.post("/api/v1/quotations")
def create(body: NewQuotation, s: Session = Depends(db), u: User = Depends(require(*QUOTERS))):
    v = _wf(qs.create_quotation, s, u, **body.model_dump())
    return version_out(s, v.quotation, v)


@router.get("/api/v1/quotations")
def list_quotations(customer_id: Optional[int] = None, s: Session = Depends(db),
                    _: User = Depends(require(*ALL_ROLES))):
    stmt = select(Quotation).order_by(Quotation.id.desc()).limit(200)
    if customer_id:
        stmt = stmt.where(Quotation.customer_id == customer_id)
    out = []
    for q in s.scalars(stmt):
        last = q.versions[-1]
        out.append({"id": q.id, "number": q.number, "customer": q.customer.name if q.customer else None,
                    "version": last.version, "status": last.status, "currency": last.currency,
                    "created_at": q.created_at, "deal_total": (last.totals or {}).get("deal_total")})
    return out


@router.get("/api/v1/quotations/{qid}/versions/{ver}")
def get_version(qid: int, ver: int, s: Session = Depends(db), _: User = Depends(require(*ALL_ROLES))):
    return version_out(s, _quotation(s, qid), _version(s, qid, ver))


class HeaderPatch(BaseModel):
    currency: Optional[Literal["BGN", "EUR"]] = None
    valid_until: Optional[date] = None
    delivery_terms: Optional[str] = Field(default=None, max_length=2000)
    payment_terms: Optional[str] = Field(default=None, max_length=2000)
    client_notes: Optional[str] = Field(default=None, max_length=4000)
    internal_notes: Optional[str] = Field(default=None, max_length=4000)


@router.patch("/api/v1/quotations/{qid}/versions/{ver}")
def patch_version(qid: int, ver: int, body: HeaderPatch, s: Session = Depends(db),
                  u: User = Depends(require(*QUOTERS))):
    v = _version(s, qid, ver)
    _wf(qs.update_header, s, u, v, body.model_dump(exclude_unset=True))
    return version_out(s, _quotation(s, qid), v)


def _no_float(v):
    return str(v) if isinstance(v, float) else v


class ProductLine(BaseModel):
    request: dict[str, Any]
    sales_price_per_item: Optional[Decimal] = Field(default=None, gt=0)
    target_margin_pct: Optional[Decimal] = Field(default=None, lt=100)
    markup_pct: Optional[Decimal] = Field(default=None, gt=-100)
    description: Optional[str] = Field(default=None, max_length=500)
    _f = field_validator("sales_price_per_item", "target_margin_pct", "markup_pct", mode="before")(_no_float)


@router.post("/api/v1/quotations/{qid}/versions/{ver}/lines")
def add_line(qid: int, ver: int, body: ProductLine, s: Session = Depends(db), u: User = Depends(require(*QUOTERS))):
    v = _version(s, qid, ver)
    pricing = {k: str(val) for k, val in body.model_dump(include={"sales_price_per_item", "target_margin_pct",
                                                                   "markup_pct"}).items() if val is not None}
    _wf(qs.add_product_line, s, u, v, body.request, pricing, body.description)
    return version_out(s, _quotation(s, qid), v)


class PalletLine(BaseModel):
    pallet_code: str
    count: int = Field(gt=0)
    unit_sales_price: Optional[Decimal] = Field(default=None, ge=0)
    _f = field_validator("unit_sales_price", mode="before")(_no_float)


@router.post("/api/v1/quotations/{qid}/versions/{ver}/pallets")
def add_pallets(qid: int, ver: int, body: PalletLine, s: Session = Depends(db), u: User = Depends(require(*QUOTERS))):
    v = _version(s, qid, ver)
    _wf(qs.add_pallet_line, s, u, v, body.pallet_code, body.count, body.unit_sales_price)
    return version_out(s, _quotation(s, qid), v)


@router.delete("/api/v1/quotations/{qid}/versions/{ver}/lines/{line_id}")
def delete_line(qid: int, ver: int, line_id: int, s: Session = Depends(db), u: User = Depends(require(*QUOTERS))):
    v = _version(s, qid, ver)
    _wf(qs.remove_line, s, u, v, line_id)
    return version_out(s, _quotation(s, qid), v)


@router.post("/api/v1/quotations/{qid}/versions/{ver}/finalize")
def finalize(qid: int, ver: int, s: Session = Depends(db), u: User = Depends(require(*QUOTERS))):
    v = _version(s, qid, ver)
    _wf(qs.finalize, s, u, v)
    return version_out(s, _quotation(s, qid), v)


@router.post("/api/v1/quotations/{qid}/versions/{ver}/new-version")
def new_version(qid: int, ver: int, s: Session = Depends(db), u: User = Depends(require(*QUOTERS))):
    v = _wf(qs.new_version, s, u, qid, ver)
    return version_out(s, _quotation(s, qid), v)


@router.post("/api/v1/quotations/{qid}/versions/{ver}/recalculate")
def recalculate(qid: int, ver: int, s: Session = Depends(db), u: User = Depends(require(*QUOTERS))):
    v = _version(s, qid, ver)
    changes = _wf(qs.recalculate, s, u, v)
    return {"changes": changes, **version_out(s, _quotation(s, qid), v)}


class StatusBody(BaseModel):
    status: Literal["sent", "accepted", "rejected", "cancelled"]


@router.post("/api/v1/quotations/{qid}/versions/{ver}/status")
def set_status(qid: int, ver: int, body: StatusBody, s: Session = Depends(db), u: User = Depends(require(*QUOTERS))):
    """„sent“ е изричното действие „изпратено към клиента“ (самото изпращане по e-mail е следваща стъпка)."""
    v = _version(s, qid, ver)
    _wf(qs.set_status, s, u, v, body.status)
    return version_out(s, _quotation(s, qid), v)


# ---------------------------------------------------------------- клиентски изход

def _client(s: Session, qid: int, ver: int):
    q, v = _quotation(s, qid), _version(s, qid, ver)
    codes = {m.code: m.display_code for m in s.scalars(select(Material))}
    return build_client_quotation(q, v, codes)


@router.get("/api/v1/quotations/{qid}/versions/{ver}/client")
def client_model(qid: int, ver: int, s: Session = Depends(db), _: User = Depends(require(*ALL_ROLES))):
    return _client(s, qid, ver)


@router.get("/quotations/{qid}/v/{ver}/print", response_class=HTMLResponse, include_in_schema=False)
def client_print(qid: int, ver: int, s: Session = Depends(db), _: User = Depends(require(*ALL_ROLES))):
    return HTMLResponse(render_html(_client(s, qid, ver)),
                        headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                                            "script-src 'unsafe-inline'"})


# ---------------------------------------------------------------- търсене и история

@router.get("/api/v1/quotation-search")
def quotation_search(length_mm: int, width_mm: int, height_mm: int, fefco: str = "0201",
                     dimension_basis: str = "internal", material_code: Optional[str] = None,
                     customer_id: Optional[int] = None, s: Session = Depends(db),
                     _: User = Depends(require(*ALL_ROLES))):
    if min(length_mm, width_mm, height_mm) <= 0:
        raise HTTPException(422, "Размерите трябва да са положителни")
    return qs.search(s, length_mm, width_mm, height_mm, fefco, dimension_basis, material_code, customer_id)


@router.get("/api/v1/customers/{customer_id}/history")
def history(customer_id: int, s: Session = Depends(db), _: User = Depends(require(*ALL_ROLES))):
    return qs.customer_history(s, customer_id)
