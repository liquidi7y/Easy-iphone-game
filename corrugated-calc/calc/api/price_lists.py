"""API за импорт и версии на ценоразписи."""

from __future__ import annotations

import io
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, undefer

from ..db.models import AuditLog, ImportFile, ImportJob, PriceList, User
from ..engine.numbers import clean
from ..imports import service
from ..imports.extract import ExtractionError, PREVIEW_SCALE, pdf_page_count, render_pdf_page
from .deps import ACTIVATORS, PRICE_EDITORS, current_user, db, require

router = APIRouter(prefix="/api/v1")


def _wf(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except (service.WorkflowError, ExtractionError) as e:
        raise HTTPException(422, str(e))
    except KeyError as e:
        raise HTTPException(422, f"Липсва параметър {e}")


def _job(s: Session, job_id: int) -> ImportJob:
    job = s.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(404, "Няма такъв импорт")
    return job


def job_out(job: ImportJob, full: bool = True) -> dict[str, Any]:
    out = {"id": job.id, "status": job.status, "price_list_code": job.price_list_code,
           "created_by": job.created_by, "created_at": job.created_at, "updated_at": job.updated_at,
           "result_version_id": job.result_version_id,
           "files": [{"id": f.id, "filename": f.filename, "kind": f.kind, "mime": f.mime, "size": f.size,
                      "sha256": f.sha256} for f in job.files]}
    if full:
        out.update({"options": job.options, "proposal": job.proposal, "validation": job.validation,
                    "extraction": job.extraction})
    return out


@router.get("/me")
def me(u: User = Depends(current_user)):
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "role": u.role}


# ---------------------------------------------------------------- imports

@router.post("/imports")
async def upload(files: list[UploadFile] = File(...), price_list_code: Optional[str] = Form(None),
                 supplier: Optional[str] = Form(None), s: Session = Depends(db),
                 u: User = Depends(require(*PRICE_EDITORS))):
    data = [(f.filename or "file", f.content_type or "", await f.read()) for f in files]
    job = _wf(service.create_import, s, u, data, price_list_code or None, supplier or None)
    return job_out(job)


class ManualImport(BaseModel):
    price_list_code: Optional[str] = None
    supplier: Optional[str] = None


@router.post("/imports/manual")
def manual(body: ManualImport, s: Session = Depends(db), u: User = Depends(require(*PRICE_EDITORS))):
    """Ръчно въвеждане (напр. когато оригиналът не може да се разчете автоматично)."""
    return job_out(_wf(service.create_import, s, u, [], body.price_list_code, body.supplier))


@router.get("/imports")
def list_imports(s: Session = Depends(db), u: User = Depends(current_user)):
    jobs = s.scalars(select(ImportJob).order_by(ImportJob.id.desc()).limit(100))
    return [job_out(j, full=False) for j in jobs]


@router.get("/imports/{job_id}")
def get_import(job_id: int, s: Session = Depends(db), u: User = Depends(current_user)):
    return job_out(_job(s, job_id))


def _file(s: Session, job_id: int, file_id: int) -> ImportFile:
    f = s.scalar(select(ImportFile).options(undefer(ImportFile.content), undefer(ImportFile.preview_png))
                 .where(ImportFile.id == file_id, ImportFile.job_id == job_id))
    if f is None:
        raise HTTPException(404, "Няма такъв файл")
    return f


SAFE_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox",
                "Cache-Control": "private, max-age=3600"}


@router.get("/imports/{job_id}/files/{file_id}")
def original(job_id: int, file_id: int, s: Session = Depends(db), u: User = Depends(current_user)):
    """Оригиналният файл, непроменен (sha256 в метаданните)."""
    f = _file(s, job_id, file_id)
    inline = f.kind == "pdf"
    mime = "application/pdf" if inline else "application/octet-stream"
    disp = "inline" if inline else "attachment"
    return Response(f.content, media_type=mime, headers={
        **SAFE_HEADERS, "Content-Disposition": f'{disp}; filename="file-{f.id}"; filename*=UTF-8\'\'{_q(f.filename)}'})


def _q(name: str) -> str:
    from urllib.parse import quote
    return quote(name)


@router.get("/imports/{job_id}/files/{file_id}/preview")
def preview(job_id: int, file_id: int, page: int = 1, s: Session = Depends(db), u: User = Depends(current_user)):
    """PNG за преглед: PDF страница (PREVIEW_SCALE) или снимка след EXIF/HEIC преобразуване."""
    f = _file(s, job_id, file_id)
    if f.kind == "image":
        png = f.preview_png
    elif f.kind == "pdf":
        if page < 1 or page > pdf_page_count(f.content):
            raise HTTPException(404, "Няма такава страница")
        buf = io.BytesIO()
        render_pdf_page(f.content, page, PREVIEW_SCALE).save(buf, format="PNG")
        png = buf.getvalue()
    else:
        raise HTTPException(404, "За таблични файлове прегледът е таблицата в extraction")
    return Response(png, media_type="image/png", headers=SAFE_HEADERS)


class Edits(BaseModel):
    ops: list[dict[str, Any]]


@router.post("/imports/{job_id}/edits")
def edits(job_id: int, body: Edits, s: Session = Depends(db), u: User = Depends(require(*PRICE_EDITORS))):
    return job_out(_wf(service.apply_edits, s, u, _job(s, job_id), body.ops))


class Remap(BaseModel):
    mappings: Optional[list[dict[str, Any]]] = None
    decimal_separator: Optional[str] = None
    keep_tiers: bool = False


@router.post("/imports/{job_id}/remap")
def remap(job_id: int, body: Remap, s: Session = Depends(db), u: User = Depends(require(*PRICE_EDITORS))):
    return job_out(_wf(service.remap, s, u, _job(s, job_id), body.mappings, body.decimal_separator,
                       body.keep_tiers))


@router.post("/imports/{job_id}/validate")
def validate(job_id: int, s: Session = Depends(db), u: User = Depends(require(*PRICE_EDITORS))):
    return _wf(service.validate_job, s, u, _job(s, job_id))


class Approve(BaseModel):
    acknowledge_warnings: bool = False


@router.post("/imports/{job_id}/approve")
def approve(job_id: int, body: Approve, s: Session = Depends(db), u: User = Depends(require(*PRICE_EDITORS))):
    v = _wf(service.approve_job, s, u, _job(s, job_id), body.acknowledge_warnings)
    return {"price_list_code": v.price_list.code, "version": v.version, "status": v.status}


# ---------------------------------------------------------------- price lists

def version_out(v, full=False) -> dict[str, Any]:
    out = {"version": v.version, "status": v.status, "currency": v.currency, "price_unit": v.price_unit,
           "tax_basis": v.tax_basis, "valid_from": v.valid_from, "valid_to": v.valid_to,
           "min_order_m2": str(clean(v.min_order_m2)) if v.min_order_m2 is not None else None,
           "min_invoiced_m2": str(clean(v.min_invoiced_m2)) if v.min_invoiced_m2 is not None else None,
           "source_import_id": v.source_import_id, "created_by": v.created_by, "created_at": v.created_at,
           "approved_by": v.approved_by, "approved_at": v.approved_at, "activated_by": v.activated_by,
           "activated_at": v.activated_at, "archived_at": v.archived_at}
    if full:
        out["tiers"] = [{"code": t.code, "lower_m2": str(clean(t.lower_m2)), "lower_inclusive": t.lower_inclusive,
                         "upper_m2": str(clean(t.upper_m2)) if t.upper_m2 is not None else None,
                         "upper_inclusive": t.upper_inclusive} for t in v.tiers]
        out["prices"] = [{"material_code": p.material_code, "tier": p.tier.code, "price": str(clean(p.price)),
                          "source_ref": p.source_ref} for p in v.prices]
    return out


@router.get("/price-lists")
def price_lists(s: Session = Depends(db), u: User = Depends(current_user)):
    return [{"code": pl.code, "supplier": pl.supplier.name, "company_contract": pl.company_contract,
             "versions": [version_out(v) for v in pl.versions]}
            for pl in s.scalars(select(PriceList).order_by(PriceList.code))]


@router.get("/price-lists/{code}/versions/{version}")
def price_list_version(code: str, version: int, s: Session = Depends(db), u: User = Depends(current_user)):
    v = _wf(service.get_version, s, code, version)
    return {"code": code, "supplier": v.price_list.supplier.name, **version_out(v, full=True)}


@router.post("/price-lists/{code}/versions/{version}/activate")
def activate(code: str, version: int, s: Session = Depends(db), u: User = Depends(require(*ACTIVATORS))):
    v = _wf(service.activate, s, u, code, version)
    return version_out(v)


class StatusBody(BaseModel):
    status: str


@router.post("/price-lists/{code}/versions/{version}/status")
def set_status(code: str, version: int, body: StatusBody, s: Session = Depends(db),
               u: User = Depends(require(*ACTIVATORS))):
    return version_out(_wf(service.set_status, s, u, code, version, body.status))


@router.post("/price-lists/{code}/versions/{version}/draft")
def draft(code: str, version: int, s: Session = Depends(db), u: User = Depends(require(*PRICE_EDITORS))):
    return job_out(_wf(service.draft_from_version, s, u, code, version))


@router.get("/audit")
def audit_log(entity: Optional[str] = None, entity_id: Optional[str] = None, limit: int = 200,
              s: Session = Depends(db), u: User = Depends(require("administrator", "sales_manager"))):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000))
    if entity:
        q = q.where(AuditLog.entity == entity)
    if entity_id:
        q = q.where(AuditLog.entity_id == entity_id)
    return [{"id": a.id, "at": a.at, "actor_id": a.actor_id, "action": a.action, "entity": a.entity,
             "entity_id": a.entity_id, "old": a.old, "new": a.new, "details": a.details} for a in s.scalars(q)]
