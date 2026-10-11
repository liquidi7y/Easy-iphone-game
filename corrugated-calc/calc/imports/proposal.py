"""Предложение за ценоразпис от разчетените таблици.

Всяка стойност пази raw текст, статус и връзка към източника. Нищо не се допълва с предположения:
двусмислени/нечетливи стойности остават празни със статус за уточнение.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel

from .extract import LOW_CONFIDENCE, text_lines
from .numparse import infer_separator, parse_number

FieldStatus = Literal["ok", "missing", "ambiguous", "unreadable", "low_confidence", "edited", "confirmed", "manual"]
NEEDS_REVIEW = ("ambiguous", "unreadable", "low_confidence")


class SourceRef(BaseModel):
    file_id: Optional[int] = None
    filename: Optional[str] = None
    table_id: Optional[str] = None
    kind: Optional[str] = None
    sheet: Optional[str] = None
    page: Optional[int] = None
    label: Optional[str] = None
    row: Optional[int] = None
    col: Optional[int] = None
    cell: Optional[str] = None
    bbox: Optional[list[float]] = None
    text: Optional[str] = None
    conf: Optional[int] = None


class PField(BaseModel):
    value: Optional[str] = None
    raw: Optional[str] = None
    status: FieldStatus = "missing"
    source: Optional[SourceRef] = None
    candidates: list[str] = []
    note: Optional[str] = None


class PTier(BaseModel):
    code: str
    lower: PField
    lower_inclusive: bool = True
    upper: PField = PField(status="ok")  # value=None → без горна граница
    upper_inclusive: bool = False
    header_raw: Optional[str] = None
    source: Optional[SourceRef] = None
    bounds_confirmed: bool = False


class PRow(BaseModel):
    id: str
    material_raw: Optional[str] = None
    material: PField
    prices: dict[str, PField] = {}
    min_order_m2: Optional[PField] = None  # минимум за поръчка, специфичен за материала
    source: Optional[SourceRef] = None


class TableMapping(BaseModel):
    table_id: str
    header_row: Optional[int] = None
    code_col: Optional[int] = None
    min_col: Optional[int] = None  # колона „мин. заявка“ по материал
    tier_cols: dict[str, int] = {}  # tier code -> колона
    skip: bool = False
    auto: bool = True


class Proposal(BaseModel):
    price_list_code: PField = PField()
    supplier: PField = PField()
    company_contract: Optional[str] = None
    currency: PField = PField()
    price_unit: PField = PField()
    tax_basis: PField = PField()
    valid_from: PField = PField()
    valid_to: PField = PField(status="ok")
    min_order_m2: PField = PField(status="ok")
    min_invoiced_m2: PField = PField(status="ok")
    decimal_separator: Optional[str] = None
    tiers: list[PTier] = []
    rows: list[PRow] = []
    mappings: list[TableMapping] = []
    notes: list[str] = []

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


HEADER_FIELDS = ("price_list_code", "supplier", "currency", "price_unit", "tax_basis", "valid_from", "valid_to",
                 "min_order_m2", "min_invoiced_m2")


# ---------------------------------------------------------------- разпознаване

CODE_RE = re.compile(r"^[A-ZА-Я]{1,3}\s?-?\d{2,5}[A-Z]?$")
CODE_HEADER = re.compile(r"(?i)код|качество|материал|марка|code|grade|quality")
MIN_HEADER = re.compile(r"(?i)мин|min")
NUM = r"(\d[\d\s .,]*\d|\d)"
UNIT = r"\s*(?:м2|m2|м²|m²|кв\.?\s*м\.?|квадрат\w*)?"
TIER_PATTERNS = [
    (re.compile(r"(?i)^\s*(?:от\s*)?" + NUM + UNIT + r"\s*(?:-|–|—|до)\s*" + NUM + UNIT + r"\s*$"), "range"),
    (re.compile(r"(?i)^\s*(?:над|>)\s*" + NUM + UNIT + r"\s*$"), "above"),
    (re.compile(r"(?i)^\s*(?:≥|>=|от)\s*" + NUM + UNIT + r"\s*$"), "from"),
    (re.compile(r"(?i)^\s*" + NUM + UNIT + r"\s*\+\s*$"), "from"),
    (re.compile(r"(?i)^\s*(?:до|<|≤|<=)\s*" + NUM + UNIT + r"\s*$"), "upto"),
]


def _int_m2(s: str) -> Optional[Decimal]:
    p = parse_number(s, "comma")
    return p.value if p.status == "ok" else None


def parse_tier_header(text: str) -> Optional[dict[str, Any]]:
    t = text.replace("\n", " ").strip()
    for rx, kind in TIER_PATTERNS:
        m = rx.match(t)
        if not m:
            continue
        nums = [_int_m2(g) for g in m.groups()]
        if any(n is None for n in nums):
            return None
        if kind == "range":
            if nums[1] <= nums[0]:
                return None
            return {"lower": nums[0], "upper": nums[1], "lower_inclusive": True, "upper_inclusive": False,
                    "kind": kind}
        if kind == "above":
            return {"lower": nums[0], "upper": None, "lower_inclusive": False, "upper_inclusive": False,
                    "kind": kind}
        if kind == "from":
            return {"lower": nums[0], "upper": None, "lower_inclusive": True, "upper_inclusive": False, "kind": kind}
        if kind == "upto":
            return {"lower": Decimal(0), "upper": nums[0], "lower_inclusive": True,
                    "upper_inclusive": "≤" in t or "<=" in t or "до" in t.lower(), "kind": kind}
    return None


def normalize_code(raw: str) -> str:
    return re.sub(r"[\s\-]", "", raw).upper().translate(str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX"))


def map_material(raw: str, known: dict[str, set[str]]) -> PField:
    """known: canonical code -> {code, display, factory} (нормализирани)."""
    norm = normalize_code(raw)
    hits = [code for code, aliases in known.items() if norm in aliases]
    if len(hits) == 1:
        return PField(value=hits[0], raw=raw, status="ok")
    if len(hits) > 1:
        return PField(raw=raw, status="ambiguous", candidates=sorted(hits),
                      note=f"„{raw}“ съответства на няколко материала")
    return PField(value=norm, raw=raw, status="unreadable" if not CODE_RE.match(norm) else "ok",
                  note="Кодът не е в регистъра на материалите")


def _src(table: dict, cell: Optional[dict], row: Optional[int] = None) -> SourceRef:
    return SourceRef(file_id=table["file_id"], filename=table["filename"], table_id=table["id"], kind=table["kind"],
                     sheet=table.get("sheet"), page=table.get("page"), label=table["label"],
                     row=cell["r"] if cell else row, col=cell["c"] if cell else None,
                     cell=cell.get("cell") if cell else None, bbox=cell.get("bbox") if cell else None,
                     text=cell["t"] if cell else None, conf=cell.get("conf") if cell else None)


def _find_header(table: dict, known: dict[str, set[str]]) -> Optional[TableMapping]:
    for row in table["rows"][:40]:
        tier_cells = [(c, parse_tier_header(c["t"])) for c in row]
        tier_cells = [(c, t) for c, t in tier_cells if t]
        if len(tier_cells) >= 1:
            code_col = next((c["c"] for c in row if CODE_HEADER.search(c["t"])), None)
            tier_col_ids = {c["c"] for c, _ in tier_cells}
            min_col = next((c["c"] for c in row if MIN_HEADER.search(c["t"]) and c["c"] not in tier_col_ids), None)
            m = TableMapping(table_id=table["id"], header_row=row[0]["r"], code_col=code_col, min_col=min_col)
            m.tier_cols = {f"#{c['c']}": c["c"] for c, _ in tier_cells}
            if code_col is None:
                m.code_col = _guess_code_col(table, m.header_row, known)
            return m
    return None


def _guess_code_col(table: dict, after_row: Optional[int], known) -> Optional[int]:
    score: dict[int, int] = {}
    for row in table["rows"]:
        if after_row is not None and row and row[0]["r"] <= after_row:
            continue
        for c in row:
            n = normalize_code(c["t"])
            if any(n in a for a in known.values()) or CODE_RE.match(n):
                score[c["c"]] = score.get(c["c"], 0) + 1
    return max(score, key=score.get) if score else None


# ---------------------------------------------------------------- документни полета

CURRENCY_RX = [(re.compile(r"(?i)\b(лв|лева|bgn)\b|лв\."), "BGN"), (re.compile(r"(?i)\b(eur|евро)\b|€"), "EUR")]
DATE_RX = re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})")


def _detect_document_fields(p: Proposal, extraction: dict[str, Any], tables_by_id: dict[str, dict]) -> None:
    lines = text_lines(extraction)
    found_ccy: dict[str, tuple[str, dict]] = {}
    for text, ref in lines:
        for rx, ccy in CURRENCY_RX:
            if rx.search(text) and ccy not in found_ccy:
                found_ccy[ccy] = (text, ref)
    if len(found_ccy) == 1:
        ccy, (text, ref) = next(iter(found_ccy.items()))
        p.currency = PField(value=ccy, raw=text, status="ok", source=_src(tables_by_id[ref["table_id"]], ref["cell"]))
    elif len(found_ccy) > 1:
        p.currency = PField(status="ambiguous", candidates=sorted(found_ccy), note="В документа има повече от една валута")

    def first(rx, fn):
        for text, ref in lines:
            m = rx.search(text)
            if m:
                return fn(m, text, _src(tables_by_id[ref["table_id"]], ref["cell"]))
        return None

    unit = first(re.compile(r"(?i)(за|/)\s*1\s?000\s*(м2|m2|м²|m²|кв\.?\s*м|квадрат)"),
                 lambda m, t, s: PField(value="per_1000m2", raw=t, status="ok", source=s)) \
        or first(re.compile(r"(?i)(лв|eur|€|евро)\.?\s*(за|/)\s*(м2|m2|м²|m²|кв\.?\s*м|квадрат)"),
                 lambda m, t, s: PField(value="per_m2", raw=t, status="ok", source=s)) \
        or first(re.compile(r"(?i)(лв|eur|€|евро)\.?\s*(за|/)\s*лист"),
                 lambda m, t, s: PField(value="per_sheet", raw=t, status="ok", source=s))
    if unit:
        p.price_unit = unit

    tax = first(re.compile(r"(?i)без\s+ддс|excl\.?\s*vat|net of vat"),
                lambda m, t, s: PField(value="excl_vat", raw=t, status="ok", source=s)) \
        or first(re.compile(r"(?i)(с|вкл\.?|включително)\s+ддс|incl\.?\s*vat"),
                 lambda m, t, s: PField(value="incl_vat", raw=t, status="ok", source=s))
    if tax:
        p.tax_basis = tax

    def as_date(m):
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None

    vf = first(re.compile(r"(?i)(?:валиден|в сила|важи)\s*(?:е\s*)?от\s*" + DATE_RX.pattern), lambda m, t, s: (m, t, s))
    if vf:
        m, t, s = vf
        d = as_date(m)
        p.valid_from = PField(value=d, raw=t, status="ok" if d else "unreadable", source=s)
    vt = first(re.compile(r"(?i)\bдо\s*" + DATE_RX.pattern), lambda m, t, s: (m, t, s))
    if vt:
        m, t, s = vt
        d = as_date(m)
        p.valid_to = PField(value=d, raw=t, status="ok" if d else "unreadable", source=s)
    mo = first(re.compile(r"(?i)мин(имална|\.)?\s*(заявка|поръчка|количество)[^\d]{0,20}" + NUM),
               lambda m, t, s: (m, t, s))
    if mo:
        m, t, s = mo
        v = _int_m2(m.group(3))
        p.min_order_m2 = PField(value=str(v) if v is not None else None, raw=t,
                                status="ok" if v is not None else "unreadable", source=s,
                                note="Минимална заявена квадратура; не е минимална фактура")


# ---------------------------------------------------------------- построяване

def build_proposal(extraction: dict[str, Any], known: dict[str, set[str]], options: dict[str, Any],
                   base: Optional[Proposal] = None) -> Proposal:
    """options: decimal_separator ('comma'|'dot'|None), mappings (ръчни TableMapping), header defaults."""
    tables = extraction.get("tables", [])
    tables_by_id = {t["id"]: t for t in tables}
    p = Proposal()
    _detect_document_fields(p, extraction, tables_by_id)

    manual_maps = {m["table_id"]: TableMapping(**{**m, "auto": False}) for m in options.get("mappings", [])}
    mappings: list[TableMapping] = []
    last: Optional[TableMapping] = None
    for t in tables:
        if t["kind"] == "text":
            mappings.append(TableMapping(table_id=t["id"], skip=True))
            continue
        m = manual_maps.get(t["id"]) or _find_header(t, known)
        if m is None and last is not None and last.code_col is not None:
            # продължение на таблица без заглавен ред (следваща страница/снимка)
            m = TableMapping(table_id=t["id"], header_row=None, code_col=last.code_col, tier_cols=dict(last.tier_cols),
                             min_col=last.min_col)
            p.notes.append(f"{t['label']}: няма заглавен ред; използвани са колоните от предходната таблица.")
        if m is None:
            m = TableMapping(table_id=t["id"], skip=True)
        mappings.append(m)
        if not m.skip:
            last = m

    # --- прагове: от първия заглавен ред, обединени по граници
    tier_defs: list[tuple[dict, dict, dict]] = []  # (parsed, cell, table)
    seen = set()
    for m in mappings:
        if m.skip or m.header_row is None:
            continue
        t = tables_by_id[m.table_id]
        hdr = next((row for row in t["rows"] if row and row[0]["r"] == m.header_row), [])
        for c in hdr:
            if c["c"] in m.tier_cols.values():
                parsed = parse_tier_header(c["t"])
                if parsed:
                    key = (parsed["lower"], parsed["upper"])
                    if key not in seen:
                        seen.add(key)
                        tier_defs.append((parsed, c, t))
    tier_defs.sort(key=lambda x: x[0]["lower"])
    key_to_code = {}
    for i, (parsed, cell, t) in enumerate(tier_defs):
        code = f"T{i + 1}"
        key_to_code[(parsed["lower"], parsed["upper"])] = code
        nxt = tier_defs[i + 1][0] if i + 1 < len(tier_defs) else None
        note = "Включването на границите трябва да се потвърди по договора."
        if nxt and parsed["upper"] is not None and nxt["lower"] != parsed["upper"]:
            note += f" Горната граница {parsed['upper']} и следващата долна {nxt['lower']} не съвпадат."
        p.tiers.append(PTier(code=code, header_raw=cell["t"], source=_src(t, cell),
                             lower=PField(value=str(parsed["lower"]), raw=cell["t"], status="ok", note=note),
                             lower_inclusive=parsed["lower_inclusive"],
                             upper=PField(value=str(parsed["upper"]) if parsed["upper"] is not None else None,
                                          raw=cell["t"], status="ok"),
                             upper_inclusive=parsed["upper_inclusive"]))
    # преименуване на колоните по код на праг
    for m in mappings:
        if m.skip or (not m.auto and all(k.startswith("T") for k in m.tier_cols)):
            continue
        t = tables_by_id[m.table_id]
        hdr = next((row for row in t["rows"] if row and row[0]["r"] == m.header_row), None)
        if hdr is None:
            continue
        new = {}
        for c in hdr:
            if c["c"] in m.tier_cols.values():
                parsed = parse_tier_header(c["t"])
                if parsed and (parsed["lower"], parsed["upper"]) in key_to_code:
                    new[key_to_code[(parsed["lower"], parsed["upper"])]] = c["c"]
        m.tier_cols = new
    # продълженията наследяват преименуваните колони
    prev = None
    for m in mappings:
        if not m.skip and m.header_row is None and prev is not None:
            m.tier_cols = dict(prev.tier_cols)
        if not m.skip and m.header_row is not None:
            prev = m
    p.mappings = mappings
    if base is not None and base.tiers and (not p.tiers or options.get("keep_tiers")):
        p.tiers = base.tiers

    # --- десетичен разделител
    price_texts = []
    for m in mappings:
        if m.skip:
            continue
        for row in tables_by_id[m.table_id]["rows"]:
            for c in row:
                if c["c"] in m.tier_cols.values() and not c.get("num") and (m.header_row is None or c["r"] > m.header_row):
                    price_texts.append(c["t"])
    sep = options.get("decimal_separator") or infer_separator(price_texts)
    p.decimal_separator = sep
    if sep is None and price_texts:
        p.notes.append("Десетичният разделител не може да се определи еднозначно; двусмислените стойности са маркирани.")

    # --- редове
    n = 0
    for m in mappings:
        if m.skip or m.code_col is None:
            continue
        t = tables_by_id[m.table_id]
        for row in t["rows"]:
            if not row or (m.header_row is not None and row[0]["r"] <= m.header_row):
                continue
            by_col = {c["c"]: c for c in row}
            price_cells = {code: by_col.get(col) for code, col in m.tier_cols.items()}
            if not any(price_cells.values()):
                continue
            code_cell = by_col.get(m.code_col)
            if code_cell is not None and parse_tier_header(code_cell["t"]):
                continue
            n += 1
            if code_cell is None:
                mat = PField(status="unreadable", note="Липсва код на материала в реда")
            else:
                mat = map_material(code_cell["t"], known)
                mat.source = _src(t, code_cell)
                if code_cell.get("conf") is not None and code_cell["conf"] < LOW_CONFIDENCE and mat.status == "ok":
                    mat.status = "low_confidence"
            prices = {}
            for code, cell in price_cells.items():
                if cell is None:
                    continue
                if cell.get("num"):
                    pr = parse_number(cell["t"], "dot")
                else:
                    pr = parse_number(cell["t"], sep)
                status = pr.status if pr.status != "empty" else "missing"
                if status == "ok" and cell.get("conf") is not None and cell["conf"] < LOW_CONFIDENCE:
                    status = "low_confidence"
                prices[code] = PField(value=str(pr.value) if pr.value is not None else None, raw=cell["t"],
                                      status=status, source=_src(t, cell), note=pr.note,
                                      candidates=[str(c) for c in pr.candidates])
            min_field = None
            min_cell = by_col.get(m.min_col) if m.min_col is not None else None
            if min_cell is not None:
                pm = parse_number(min_cell["t"], "dot" if min_cell.get("num") else sep)
                min_field = PField(value=str(pm.value) if pm.value is not None else None, raw=min_cell["t"],
                                   status=pm.status if pm.status != "empty" else "missing", source=_src(t, min_cell),
                                   note=pm.note, candidates=[str(c) for c in pm.candidates])
            p.rows.append(PRow(id=f"r{n}", material_raw=code_cell["t"] if code_cell else None, material=mat,
                               prices=prices, min_order_m2=min_field, source=_src(t, code_cell, row[0]["r"])))

    # --- ръчно зададени полета от потребителя имат предимство
    for f in HEADER_FIELDS:
        if f in options.get("header", {}) and options["header"][f] is not None:
            setattr(p, f, PField(value=str(options["header"][f]), status="manual"))
    if base is not None:
        for f in HEADER_FIELDS:
            bf: PField = getattr(base, f)
            if bf.status in ("edited", "confirmed", "manual"):
                setattr(p, f, bf)
    if not tables:
        p.notes.append("Няма разчетени таблици; въведете данните ръчно по оригинала.")
    return p
