"""Детерминиран парсер за бърз вход и последващи чат въпроси.

Работи без AI. Извлича само параметри; не смята цени и не избира ценови праг.
Контекстът е структурирано състояние (dict, съвместим с CalculateRequest).
LLM parser може да се добави по-късно със същия изход и валидация.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Optional

from pydantic import BaseModel

DIM3 = re.compile(r"(\d{2,5})\s*[/x×*хХ]\s*(\d{2,5})\s*[/x×*хХ]\s*(\d{2,5})")
DIM2 = re.compile(r"(\d{2,5})\s*(?:[/x×*хХ]|\s+на\s+)\s*(\d{2,5})(?!\s*[/x×*хХ]\s*\d)", re.IGNORECASE)
MATERIAL = re.compile(r"\b([A-Z]{1,2}\d{3,5})\b")
FLUTES = ("EB", "EC", "BC", "BE", "CB", "CE", "BB", "E", "B", "C", "F")
FLUTE = re.compile(r"(?<![A-Za-z\d])(" + "|".join(FLUTES) + r")(?![A-Za-z\d])")
NUMBER = r"(\d+(?:[.,]\d+)?)"
PERCENT = re.compile(NUMBER + r"\s*%\s*(марж|margin|markup|надценка|отпадък|брак)?", re.IGNORECASE)
PRICE = re.compile(r"(?:цена|по|на)?\s*" + NUMBER + r"\s*(лв\.?|лева|bgn|евро|eur|€)", re.IGNORECASE)
PER_1000 = re.compile(r"за\s*1\s*000\s*(?:квадрата|кв\.?\s*м|m2|м2|m²)|/\s*1000\s*(?:m2|м2|m²)", re.IGNORECASE)
PER_M2 = re.compile(r"за\s*(?:квадрат|кв\.?\s*м|m2|м2|m²)|/\s*(?:m2|м2|m²)", re.IGNORECASE)
PER_SHEET = re.compile(r"за\s*(?:лист|брой)|/\s*лист", re.IGNORECASE)
IN_SHEET = re.compile(r"(\d+)\s*(?:бр\.?\s*)?(?:в|на)\s*лист|(?:изделия\s*в\s*лист)\s*(\d+)", re.IGNORECASE)
QTY = re.compile(r"(?<![\d.,/])(\d{1,7})(?:\s*(?:бр|броя|pcs))?(?![\d.,/%])", re.IGNORECASE)


class ParseResult(BaseModel):
    intent: str
    request: dict[str, Any]
    changed: list[str]
    missing_inputs: list[str]
    notes: list[str]
    calculable: bool


def _currency(token: str) -> str:
    t = token.lower()
    return "EUR" if t in ("евро", "eur", "€") else "BGN"


def parse(text: str, state: Optional[dict[str, Any]] = None) -> ParseResult:
    st: dict[str, Any] = copy.deepcopy(state) if state else {}
    original = text
    text = text.strip()
    low = text.lower()
    changed: list[str] = []
    notes: list[str] = []
    intent = "calculate"

    def set_(key, value):
        if st.get(key) != value:
            st[key] = value
            changed.append(key)

    work = text

    # --- размери
    m3 = DIM3.search(work)
    if m3:
        L, W, H = (int(g) for g in m3.groups())
        set_("box", {**(st.get("box") or {}), "length_mm": L, "width_mm": W, "height_mm": H})
        set_("mode", "box")
        work = work[:m3.start()] + " " + work[m3.end():]

    is_sheet_text = "лист" in low
    if not m3:
        m2 = DIM2.search(work)
        if m2 and (is_sheet_text or not st.get("box")):
            a, b = int(m2.group(1)), int(m2.group(2))
            sheet = {**(st.get("sheet") or {}), "length_mm": a, "width_mm": b}
            set_("sheet", sheet)
            if not st.get("box"):
                set_("mode", "sheet")
            work = work[:m2.start()] + " " + work[m2.end():]

    m_in = IN_SHEET.search(work)
    if m_in:
        n = int(m_in.group(1) or m_in.group(2))
        sheet = {**(st.get("sheet") or {}), "items_per_sheet": n}
        set_("sheet", sheet)
        if st.get("mode") == "sheet":
            set_("mode", "sheet_items")
        work = work[:m_in.start()] + " " + work[m_in.end():]

    # --- материал / вълна
    mm = MATERIAL.search(work)
    if mm:
        set_("material_code", mm.group(1))
        st.pop("flute", None)
        work = work[:mm.start()] + " " + work[mm.end():]
    else:
        mf = FLUTE.search(work)
        if mf:
            set_("flute", mf.group(1))
            if st.pop("material_code", None):
                changed.append("material_code")
                notes.append("Смяна на вълна: изборът на материал се прави наново.")
            work = work[:mf.start()] + " " + work[mf.end():]

    if re.search(r"по-евтин|икономичн|economy", low):
        intent = "select_economy"
        notes.append("Ще се използва economy алтернативата от препоръката, ако има такава.")

    # --- проценти
    for mp in PERCENT.finditer(work):
        val, kind = mp.group(1).replace(",", "."), (mp.group(2) or "").lower()
        if kind in ("markup", "надценка"):
            set_("sales", {**(st.get("sales") or {}), "markup_pct": val, "target_margin_pct": None,
                           "sales_price_per_item": None})
        elif kind in ("отпадък", "брак"):
            set_("waste", {"rate_pct": val, "type": "loss_of_input", "source": "чат"})
            notes.append("Брак приет като загуба от входа (÷(1−r)); сменете типа при нужда.")
        else:
            set_("sales", {**(st.get("sales") or {}), "target_margin_pct": val, "markup_pct": None,
                           "sales_price_per_item": None})
        work = work[:mp.start()] + " " * (mp.end() - mp.start()) + work[mp.end():]

    # --- цени
    mprice = PRICE.search(work)
    if mprice:
        val, cur = mprice.group(1).replace(",", "."), _currency(mprice.group(2))
        if PER_1000.search(low) or PER_M2.search(low) or PER_SHEET.search(low) or "цена" in low:
            unit = ("per_1000m2" if PER_1000.search(low) else "per_m2" if PER_M2.search(low)
                    else "per_sheet" if PER_SHEET.search(low) else None)
            if unit is None:
                notes.append("Липсва ценова база (за m², 1000 m² или лист).")
                st["_pending_price"] = {"price": val, "currency": cur}
            else:
                set_("manual_price", {"price": val, "unit": unit, "currency": cur, "tax_basis": "excl_vat"})
        else:
            # „На 4.50 евро колко остава?“ — продажна цена за изделие
            set_("sales", {"sales_price_per_item": val, "currency": cur})
            intent = "what_remains"
        work = work[:mprice.start()] + " " + work[mprice.end():]
        work = PER_1000.sub(" ", work)

    # --- количество (последното самостоятелно цяло число)
    qtys = [int(x) for x in QTY.findall(work) if int(x) > 0]
    if qtys:
        set_("quantity", qtys[-1])

    missing = []
    mode = st.get("mode")
    if not mode:
        missing.append("размери (L/W/H или лист)")
    if mode in ("box", "sheet_items") and not st.get("quantity"):
        missing.append("количество")
    if not (st.get("material_code") or st.get("flute") or st.get("manual_price")):
        missing.append("материал, вълна или ръчна цена")
    if st.get("_pending_price"):
        missing.append("ценова база")

    if not changed and intent == "calculate":
        intent = "unknown"
        notes.append(f"Не разпознах параметри в „{original}“.")

    request = {k: v for k, v in st.items() if v is not None}
    return ParseResult(intent=intent, request=request, changed=changed, missing_inputs=missing, notes=notes,
                       calculable=not missing)
