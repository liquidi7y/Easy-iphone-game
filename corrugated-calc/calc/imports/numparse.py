"""Разчитане на числа от ценоразписи без предположения.

„1 060,50“ → 1060.50; „1,060“ е двусмислено (1060 или 1.060), освен ако документът еднозначно
показва кой е десетичният разделител. Двусмислените стойности остават за уточнение.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

Sep = Literal["auto", "comma", "dot"]

_SPACES = re.compile(r"[\s   ']+")
_STRIP = re.compile(r"(?i)(лв\.?|лева|bgn|eur|евро|€|/\s*(m2|м2|m²|кв\.?\s*м|лист|бр\.?))")
_NUMERIC = re.compile(r"^[+-]?[\d.,]+$")


@dataclass
class Parsed:
    value: Optional[Decimal]
    status: Literal["ok", "empty", "ambiguous", "unreadable"]
    candidates: list[Decimal] = field(default_factory=list)
    note: Optional[str] = None


def _clean(text: str) -> str:
    t = _STRIP.sub("", text or "")
    t = _SPACES.sub("", t)
    return t.strip()


def separator_evidence(text: str) -> Optional[str]:
    """Връща 'comma'/'dot', ако стойността еднозначно показва десетичния разделител."""
    t = _clean(text)
    if not _NUMERIC.match(t or "x"):
        return None
    if "," in t and "." in t:
        return "comma" if t.rfind(",") > t.rfind(".") else "dot"
    for sep, name in ((",", "comma"), (".", "dot")):
        if t.count(sep) == 1:
            frac = t.split(sep)[1]
            if len(frac) != 3:  # 1060,5 / 1060,50 → десетичен
                return name
    return None


def infer_separator(texts: list[str]) -> Optional[str]:
    votes = {"comma": 0, "dot": 0}
    for t in texts:
        e = separator_evidence(t)
        if e:
            votes[e] += 1
    if votes["comma"] and not votes["dot"]:
        return "comma"
    if votes["dot"] and not votes["comma"]:
        return "dot"
    return None


def _grouped(int_part: str) -> bool:
    groups = int_part.split("|")
    return 1 <= len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:])


def parse_number(text: Optional[str], sep: Optional[str] = None) -> Parsed:
    """sep: 'comma' / 'dot' (потвърден за документа) или None (неизвестен)."""
    if text is None or not str(text).strip():
        return Parsed(None, "empty")
    raw = str(text)
    t = _clean(raw)
    if not t:
        return Parsed(None, "empty")
    if not _NUMERIC.match(t):
        return Parsed(None, "unreadable", note=f"Нечислови символи в „{raw.strip()}“")
    sign = ""
    if t[0] in "+-":
        sign, t = t[0], t[1:]

    def dec(s: str) -> Optional[Decimal]:
        try:
            return Decimal(sign + s)
        except InvalidOperation:
            return None

    has_c, has_d = "," in t, "." in t
    if has_c and has_d:
        dsep, tsep = (",", ".") if t.rfind(",") > t.rfind(".") else (".", ",")
        int_part, _, frac = t.rpartition(dsep)
        if dsep in int_part or not _grouped(int_part.replace(tsep, "|")):
            return Parsed(None, "unreadable", note=f"Неясно групиране в „{raw.strip()}“")
        v = dec(int_part.replace(tsep, "") + "." + frac)
        return Parsed(v, "ok") if v is not None else Parsed(None, "unreadable")

    if not has_c and not has_d:
        v = dec(t)
        return Parsed(v, "ok") if v is not None else Parsed(None, "unreadable")

    ch = "," if has_c else "."
    parts = t.split(ch)
    if len(parts) > 2:  # 1.060.500 → само хиляди
        if _grouped("|".join(parts)):
            return Parsed(dec("".join(parts)), "ok")
        return Parsed(None, "unreadable", note=f"Неясно групиране в „{raw.strip()}“")
    int_part, frac = parts
    as_decimal = dec((int_part or "0") + "." + frac)
    as_thousands = dec(int_part + frac) if int_part and len(frac) == 3 and len(int_part) <= 3 else None
    doc_sep = {"comma": ",", "dot": "."}.get(sep or "")
    if doc_sep == ch:
        return Parsed(as_decimal, "ok")
    if doc_sep and doc_sep != ch:
        if as_thousands is not None:
            return Parsed(as_thousands, "ok")
        return Parsed(None, "ambiguous", [as_decimal], note=f"„{ch}“ не е десетичният разделител на документа")
    if as_thousands is None:
        return Parsed(as_decimal, "ok")
    return Parsed(None, "ambiguous", [as_thousands, as_decimal],
                  note=f"„{raw.strip()}“ може да е {as_thousands} или {as_decimal}")
