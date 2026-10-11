"""Поток: Upload → Extract → Preview/Edit → Validate → Approve → Activate.

AI/OCR само предлага; активна цена се появява едва след човешко одобрение и отделно активиране.
Всяка промяна се записва в audit log със стара/нова стойност.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.audit import audit
from ..db.models import (ImportFile, ImportJob, Material, MaterialOrderRule, MaterialPriceTier, PriceList,
                         PriceListVersion, PriceTier, RuleVersion, Supplier, User)
from ..db.repository import ENGINE_SETTINGS
from ..engine.numbers import clean
from .extract import FileInfo, detect_kind, extract_files, image_preview_png
from .numparse import parse_number
from .proposal import HEADER_FIELDS, PField, Proposal, PRow, PTier, build_proposal, normalize_code
from .validate import validate_proposal

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES = 20


class WorkflowError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


def known_materials(s: Session) -> dict[str, set[str]]:
    out = {}
    for m in s.scalars(select(Material)):
        out[m.code] = {normalize_code(x) for x in (m.code, m.display_code, m.factory_code) if x}
    return out


def _unusual_pct(s: Session) -> Decimal:
    rv = s.scalar(select(RuleVersion).where(RuleVersion.kind == ENGINE_SETTINGS, RuleVersion.active))
    v = (rv.data.get("settings", {}).get("price_import", {}) if rv else {}).get("unusual_change_pct", "15")
    return Decimal(str(v))


def _proposal(job: ImportJob) -> Proposal:
    return Proposal.model_validate(job.proposal or {})


def _save(job: ImportJob, p: Proposal) -> None:
    job.proposal = p.model_dump(mode="json")
    job.validation = None
    if job.status == "validated":
        job.status = "extracted"


def _require_open(job: ImportJob) -> None:
    if job.status not in ("extracted", "validated"):
        raise WorkflowError(f"Импорт #{job.id} е със статус {job.status} и не се редактира; създайте нова чернова.")


# ---------------------------------------------------------------- upload + extract

def create_import(s: Session, actor: User, files: list[tuple[str, str, bytes]],
                  price_list_code: Optional[str] = None, supplier: Optional[str] = None,
                  options: Optional[dict[str, Any]] = None) -> ImportJob:
    if len(files) > MAX_FILES:
        raise WorkflowError(f"Най-много {MAX_FILES} файла в един импорт.")
    options = dict(options or {})
    header = dict(options.get("header") or {})
    if price_list_code:
        header["price_list_code"] = price_list_code
        pl = s.scalar(select(PriceList).where(PriceList.code == price_list_code))
        if pl is not None and not supplier:
            supplier = pl.supplier.name
    if supplier:
        header["supplier"] = supplier
    options["header"] = header

    job = ImportJob(status="extracted", price_list_code=price_list_code, options=options, created_by=actor.id)
    s.add(job)
    s.flush()
    infos = []
    for filename, mime, content in files:
        if len(content) > MAX_FILE_BYTES:
            raise WorkflowError(f"{filename}: файлът е над {MAX_FILE_BYTES // 1024 // 1024} MB.")
        if not content:
            raise WorkflowError(f"{filename}: празен файл.")
        kind = detect_kind(filename, content)
        f = ImportFile(job_id=job.id, filename=filename[:300], mime=mime or "application/octet-stream", kind=kind,
                       sha256=hashlib.sha256(content).hexdigest(), size=len(content), content=content)
        if kind == "image":
            f.preview_png = image_preview_png(content)
        s.add(f)
        s.flush()
        infos.append(FileInfo(f.id, f.filename, content))

    extraction = extract_files(infos)
    job.extraction = extraction
    p = build_proposal(extraction, known_materials(s), options)
    _save(job, p)
    audit(s, actor.id, "import.upload", "import_job", job.id,
          new={"files": [{"filename": f[0], "sha256": hashlib.sha256(f[2]).hexdigest(), "size": len(f[2])}
                         for f in files], "price_list_code": price_list_code},
          details={"tables": len(extraction["tables"]), "rows": len(p.rows), "errors": extraction["errors"]})
    return job


def remap(s: Session, actor: User, job: ImportJob, mappings: Optional[list[dict]] = None,
          decimal_separator: Optional[str] = None, keep_tiers: bool = False) -> ImportJob:
    """Ръчно задаване на заглавен ред/колони/разделител и повторно построяване на редовете."""
    _require_open(job)
    old_opts = dict(job.options or {})
    opts = dict(old_opts)
    if mappings is not None:
        opts["mappings"] = mappings
    if decimal_separator is not None:
        if decimal_separator not in ("comma", "dot"):
            raise WorkflowError("decimal_separator: comma или dot")
        opts["decimal_separator"] = decimal_separator
    opts["keep_tiers"] = keep_tiers
    base = _proposal(job)
    p = build_proposal(job.extraction or {"tables": []}, known_materials(s), opts, base=base)
    job.options = opts
    _save(job, p)
    audit(s, actor.id, "import.remap", "import_job", job.id, old={k: old_opts.get(k) for k in
                                                                    ("mappings", "decimal_separator")},
          new={k: opts.get(k) for k in ("mappings", "decimal_separator")},
          details={"note": "Редовете са построени наново; предишните редакции по редове не се пренасят."})
    return job


# ---------------------------------------------------------------- редакция

def _user_decimal(v: Any, what: str) -> Optional[str]:
    if v is None or str(v).strip() == "":
        return None
    pr = parse_number(str(v))
    if pr.status != "ok":
        raise WorkflowError(f"{what}: „{v}“ не е еднозначно число ({pr.note or pr.status}). "
                            f"Използвайте формат 1060.50.")
    return str(pr.value)


def _row(p: Proposal, row_id: str) -> PRow:
    for r in p.rows:
        if r.id == row_id:
            return r
    raise WorkflowError(f"Няма ред {row_id}")


def _tier(p: Proposal, code: str) -> PTier:
    for t in p.tiers:
        if t.code == code:
            return t
    raise WorkflowError(f"Няма праг {code}")


def apply_edits(s: Session, actor: User, job: ImportJob, ops: list[dict[str, Any]]) -> ImportJob:
    _require_open(job)
    p = _proposal(job)
    for op in ops:
        kind = op.get("op")
        old: Any = None
        new: Any = None
        if kind == "set_field":
            field = op["field"]
            if field not in HEADER_FIELDS and field != "company_contract":
                raise WorkflowError(f"Непознато поле {field}")
            if field == "company_contract":
                old, p.company_contract = p.company_contract, op.get("value")
                new = p.company_contract
            else:
                f: PField = getattr(p, field)
                old = f.model_dump(mode="json")
                value = op.get("value")
                if field in ("min_order_m2", "min_invoiced_m2"):
                    value = _user_decimal(value, field)
                elif field in ("valid_from", "valid_to") and value:
                    value = date.fromisoformat(str(value)).isoformat()
                elif value is not None:
                    value = str(value).strip() or None
                status = "confirmed" if value == f.value and value is not None else ("edited" if value is not None
                                                                                       else "ok")
                if field in ("price_list_code", "supplier", "currency", "price_unit", "tax_basis",
                             "valid_from") and value is None:
                    status = "missing"
                setattr(p, field, PField(value=value, raw=f.raw, status=status, source=f.source))
                new = getattr(p, field).model_dump(mode="json")
        elif kind == "set_price":
            r = _row(p, op["row_id"])
            _tier(p, op["tier"])
            f = r.prices.get(op["tier"]) or PField(status="missing")
            old = f.model_dump(mode="json")
            value = _user_decimal(op.get("value"), f"{r.id}/{op['tier']}")
            r.prices[op["tier"]] = PField(value=value, raw=f.raw, source=f.source,
                                          status="edited" if value is not None else "missing")
            new = r.prices[op["tier"]].model_dump(mode="json")
        elif kind == "set_row_min_order":
            r = _row(p, op["row_id"])
            old = r.min_order_m2.model_dump(mode="json") if r.min_order_m2 else None
            value = _user_decimal(op.get("value"), f"{r.id}/мин. заявка")
            src = r.min_order_m2.source if r.min_order_m2 else None
            raw = r.min_order_m2.raw if r.min_order_m2 else None
            r.min_order_m2 = PField(value=value, raw=raw, source=src, status="edited") if value else None
            new = r.min_order_m2.model_dump(mode="json") if r.min_order_m2 else None
        elif kind == "confirm":
            if "field" in op:
                if op["field"] not in HEADER_FIELDS:
                    raise WorkflowError(f"Непознато поле {op['field']}")
                f = getattr(p, op["field"])
                target = f
            else:
                target = _row(p, op["row_id"]).prices.get(op["tier"]) if "tier" in op else _row(p, op["row_id"]).material
            if target is None or target.value is None:
                raise WorkflowError("Няма стойност за потвърждение; въведете стойност.")
            old = target.status
            target.status = "confirmed"
            new = "confirmed"
        elif kind == "set_material":
            r = _row(p, op["row_id"])
            old = r.material.model_dump(mode="json")
            code = normalize_code(str(op["value"])) if op.get("value") else None
            r.material = PField(value=code, raw=r.material.raw, source=r.material.source,
                                status="edited" if code else "missing")
            new = r.material.model_dump(mode="json")
        elif kind == "add_row":
            n = max([int(r.id[1:]) for r in p.rows if r.id[1:].isdigit()] + [0]) + 1
            r = PRow(id=f"r{n}", material=PField(value=normalize_code(op["material"]) if op.get("material") else None,
                                                 status="manual" if op.get("material") else "missing"))
            for tcode, v in (op.get("prices") or {}).items():
                _tier(p, tcode)
                val = _user_decimal(v, f"r{n}/{tcode}")
                r.prices[tcode] = PField(value=val, status="manual" if val else "missing")
            p.rows.append(r)
            new = r.model_dump(mode="json")
        elif kind == "remove_row":
            r = _row(p, op["row_id"])
            old = r.model_dump(mode="json")
            p.rows.remove(r)
        elif kind in ("set_tier", "add_tier"):
            if kind == "add_tier":
                n = max([int(t.code[1:]) for t in p.tiers if t.code[1:].isdigit()] + [0]) + 1
                t = PTier(code=f"T{n}", lower=PField(status="missing"))
                p.tiers.append(t)
            else:
                t = _tier(p, op["code"])
                old = t.model_dump(mode="json")
            if "lower" in op:
                t.lower = PField(value=_user_decimal(op["lower"], "долна граница"), status="edited")
            if "upper" in op:
                t.upper = PField(value=_user_decimal(op["upper"], "горна граница"), status="edited")
            if "lower_inclusive" in op:
                t.lower_inclusive = bool(op["lower_inclusive"])
            if "upper_inclusive" in op:
                t.upper_inclusive = bool(op["upper_inclusive"])
            t.bounds_confirmed = bool(op.get("confirm", False))
            new = t.model_dump(mode="json")
        elif kind == "remove_tier":
            t = _tier(p, op["code"])
            old = t.model_dump(mode="json")
            p.tiers.remove(t)
            for r in p.rows:
                r.prices.pop(t.code, None)
        elif kind == "confirm_tiers":
            old = {t.code: t.bounds_confirmed for t in p.tiers}
            for t in p.tiers:
                t.bounds_confirmed = True
            new = {t.code: True for t in p.tiers}
        else:
            raise WorkflowError(f"Непозната операция {kind}")
        audit(s, actor.id, f"import.edit.{kind}", "import_job", job.id, old=old, new=new, details=op)
    _save(job, p)
    return job


# ---------------------------------------------------------------- validate / approve

def _previous(s: Session, code: Optional[str]) -> Optional[dict[str, Any]]:
    if not code:
        return None
    pl = s.scalar(select(PriceList).where(PriceList.code == code))
    if pl is None:
        return None
    candidates = [v for v in pl.versions if v.status in ("active", "approved", "reference")]
    if not candidates:
        return None
    v = next((x for x in candidates if x.status == "active"), candidates[-1])
    prices = {}
    for mp in v.prices:
        t = mp.tier
        prices[(mp.material_code, str(clean(t.lower_m2)) if t.lower_m2 is not None else None,
                str(clean(t.upper_m2)) if t.upper_m2 is not None else None)] = clean(mp.price)
    return {"version": v.version, "currency": v.currency, "price_unit": v.price_unit, "tax_basis": v.tax_basis,
            "prices": prices}


def _norm_bounds(p: Proposal) -> Proposal:
    for t in p.tiers:
        if t.lower.value is not None:
            t.lower.value = str(clean(Decimal(t.lower.value)))
        if t.upper.value is not None:
            t.upper.value = str(clean(Decimal(t.upper.value)))
    return p


def validate_job(s: Session, actor: User, job: ImportJob) -> dict[str, Any]:
    _require_open(job)
    p = _norm_bounds(_proposal(job))
    job.proposal = p.model_dump(mode="json")
    known = set(known_materials(s))
    result = validate_proposal(p, known, _previous(s, p.price_list_code.value), _unusual_pct(s))
    job.validation = {**result, "validated_at": _now().isoformat(), "validated_by": actor.id}
    job.status = "validated" if result["ok"] else "extracted"
    audit(s, actor.id, "import.validate", "import_job", job.id,
          new={"ok": result["ok"], "errors": len(result["errors"]), "warnings": len(result["warnings"])})
    return job.validation


def approve_job(s: Session, actor: User, job: ImportJob, acknowledge_warnings: bool = False) -> PriceListVersion:
    if job.status != "validated" or not job.validation or not job.validation.get("ok"):
        raise WorkflowError("Импортът трябва да е валидиран без грешки преди одобрение.")
    p = _proposal(job)
    if job.validation["digest"] != p.digest():
        raise WorkflowError("Предложението е променено след валидиране; валидирайте отново.")
    if job.validation["warnings"] and not acknowledge_warnings:
        raise WorkflowError("Има предупреждения (напр. необичайни промени); потвърдете ги изрично.")

    code = p.price_list_code.value
    pl = s.scalar(select(PriceList).where(PriceList.code == code))
    if pl is None:
        sup = s.scalar(select(Supplier).where(Supplier.name == p.supplier.value))
        if sup is None:
            sup = Supplier(name=p.supplier.value)
            s.add(sup)
            s.flush()
        pl = PriceList(code=code, supplier_id=sup.id, company_contract=p.company_contract)
        s.add(pl)
        s.flush()
        audit(s, actor.id, "create", "price_list", pl.id, new={"code": code, "supplier": p.supplier.value})
    elif pl.supplier.name != p.supplier.value:
        raise WorkflowError(f"Ценоразпис {code} е на доставчик {pl.supplier.name}, а не {p.supplier.value}.")

    next_version = (s.scalar(select(func.max(PriceListVersion.version)).where(
        PriceListVersion.price_list_id == pl.id)) or 0) + 1
    v = PriceListVersion(
        price_list_id=pl.id, version=next_version, status="draft", currency=p.currency.value,
        price_unit=p.price_unit.value, tax_basis=p.tax_basis.value, valid_from=date.fromisoformat(p.valid_from.value),
        valid_to=date.fromisoformat(p.valid_to.value) if p.valid_to.value else None,
        min_order_m2=Decimal(p.min_order_m2.value) if p.min_order_m2.value else None,
        min_invoiced_m2=Decimal(p.min_invoiced_m2.value) if p.min_invoiced_m2.value else None,
        source_import_id=job.id, created_by=job.created_by, notes="; ".join(p.notes) or None)
    s.add(v)
    s.flush()
    tiers = {}
    for i, t in enumerate(sorted(p.tiers, key=lambda t: Decimal(t.lower.value))):
        tiers[t.code] = PriceTier(version_id=v.id, code=f"T{i + 1}", sort=i, lower_m2=Decimal(t.lower.value),
                                  lower_inclusive=t.lower_inclusive,
                                  upper_m2=Decimal(t.upper.value) if t.upper.value is not None else None,
                                  upper_inclusive=t.upper_inclusive)
        s.add(tiers[t.code])
    s.flush()
    for r in p.rows:
        for tcode, f in r.prices.items():
            if f.value is None:
                continue
            s.add(MaterialPriceTier(version_id=v.id, tier_id=tiers[tcode].id, material_code=r.material.value,
                                    price=Decimal(f.value),
                                    source_ref={"import_id": job.id, "row_id": r.id, "raw": f.raw, "status": f.status,
                                                "source": f.source.model_dump(mode="json") if f.source else None}))
    for r in p.rows:
        if r.min_order_m2 is not None and r.min_order_m2.value is not None:
            s.add(MaterialOrderRule(version_id=v.id, material_code=r.material.value,
                                    min_order_m2=Decimal(r.min_order_m2.value),
                                    source_ref={"import_id": job.id, "row_id": r.id, "raw": r.min_order_m2.raw,
                                                "source": r.min_order_m2.source.model_dump(mode="json")
                                                if r.min_order_m2.source else None}))
    s.flush()
    v.status = "approved"
    v.approved_by = actor.id
    v.approved_at = _now()
    job.status = "approved"
    job.result_version_id = v.id
    s.flush()
    audit(s, actor.id, "price_list.approve", "price_list_version", v.id,
          new={"code": code, "version": v.version, "status": "approved", "import_id": job.id,
               "acknowledged_warnings": job.validation["warnings"] if acknowledge_warnings else []})
    return v


# ---------------------------------------------------------------- activate / status

def get_version(s: Session, code: str, version: int) -> PriceListVersion:
    v = s.scalar(select(PriceListVersion).join(PriceList).where(PriceList.code == code,
                                                                PriceListVersion.version == version))
    if v is None:
        raise WorkflowError(f"Няма {code} v{version}")
    return v


def activate(s: Session, actor: User, code: str, version: int) -> PriceListVersion:
    v = get_version(s, code, version)
    if v.status != "approved":
        raise WorkflowError(f"Активира се само одобрена версия; {code} v{version} е {v.status}.")
    if v.valid_to and v.valid_to < date.today():
        raise WorkflowError("Валидността на версията е изтекла.")
    prev = s.scalar(select(PriceListVersion).where(PriceListVersion.price_list_id == v.price_list_id,
                                                   PriceListVersion.status == "active"))
    if prev is not None:
        prev.status = "archived"
        prev.archived_at = _now()
        s.flush()
        audit(s, actor.id, "price_list.archive", "price_list_version", prev.id, old={"status": "active"},
              new={"status": "archived"}, details={"replaced_by": v.version})
    v.status = "active"
    v.activated_by = actor.id
    v.activated_at = _now()
    s.flush()
    audit(s, actor.id, "price_list.activate", "price_list_version", v.id, old={"status": "approved"},
          new={"status": "active"}, details={"code": code, "version": version,
                                              "previous_active": prev.version if prev else None})
    return v


def set_status(s: Session, actor: User, code: str, version: int, status: str) -> PriceListVersion:
    if status not in ("reference", "archived"):
        raise WorkflowError("Разрешени статуси тук: reference, archived. За активиране използвайте activate.")
    v = get_version(s, code, version)
    old = v.status
    v.status = status
    if status == "archived":
        v.archived_at = _now()
    try:
        s.flush()
    except Exception as e:
        raise WorkflowError(f"Недопустим преход {old} → {status}") from e
    audit(s, actor.id, f"price_list.{status}", "price_list_version", v.id, old={"status": old},
          new={"status": status})
    return v


def draft_from_version(s: Session, actor: User, code: str, version: int) -> ImportJob:
    """Нова чернова за редакция на база съществуваща версия; старата остава непроменена."""
    v = get_version(s, code, version)
    p = Proposal(price_list_code=PField(value=code, status="confirmed"),
                 supplier=PField(value=v.price_list.supplier.name, status="confirmed"),
                 company_contract=v.price_list.company_contract,
                 currency=PField(value=v.currency, status="confirmed"),
                 price_unit=PField(value=v.price_unit, status="confirmed"),
                 tax_basis=PField(value=v.tax_basis, status="confirmed"),
                 valid_from=PField(status="missing", note=f"Задайте нова дата (v{version}: {v.valid_from})"),
                 valid_to=PField(value=v.valid_to.isoformat() if v.valid_to else None, status="ok"),
                 min_order_m2=PField(value=str(v.min_order_m2) if v.min_order_m2 is not None else None, status="ok"),
                 min_invoiced_m2=PField(value=str(v.min_invoiced_m2) if v.min_invoiced_m2 is not None else None,
                                        status="ok"),
                 notes=[f"Чернова на база {code} v{version}"])
    for t in v.tiers:
        p.tiers.append(PTier(code=t.code, lower=PField(value=str(clean(t.lower_m2)), status="confirmed"),
                             lower_inclusive=t.lower_inclusive,
                             upper=PField(value=str(clean(t.upper_m2)) if t.upper_m2 is not None else None,
                                          status="confirmed"),
                             upper_inclusive=t.upper_inclusive, bounds_confirmed=True))
    rows: dict[str, PRow] = {}
    for mp in v.prices:
        r = rows.setdefault(mp.material_code, PRow(id=f"r{len(rows) + 1}",
                                                   material=PField(value=mp.material_code, status="confirmed")))
        r.prices[mp.tier.code] = PField(value=str(clean(mp.price)), status="confirmed")
    for mr in v.material_rules:
        r = rows.setdefault(mr.material_code, PRow(id=f"r{len(rows) + 1}",
                                                   material=PField(value=mr.material_code, status="confirmed")))
        r.min_order_m2 = PField(value=str(clean(mr.min_order_m2)), status="confirmed")
    p.rows = list(rows.values())
    job = ImportJob(status="extracted", price_list_code=code, options={"based_on": {"code": code, "version": version}},
                    extraction={"tables": [], "errors": []}, created_by=actor.id)
    s.add(job)
    s.flush()
    _save(job, p)
    audit(s, actor.id, "import.draft_from_version", "import_job", job.id, details={"code": code, "version": version})
    return job
