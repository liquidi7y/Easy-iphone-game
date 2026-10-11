"""Rule-based подбор на материал. Не преминава автоматично към по-слаб материал."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .config import Config
from .models import CalculateRequest, MaterialCost, MaterialRecommendation
from .numbers import clean


def size_class(cfg: Config, volume_m3: Optional[Decimal]) -> Optional[str]:
    if volume_m3 is None:
        return None
    for sc in cfg.settings.size_classes:
        if sc.interval.contains(volume_m3):
            return sc.code
    return None


def candidates_for(cfg: Config, flute: Optional[str], code: Optional[str]) -> list[str]:
    if code:
        mat = cfg.materials.get(code)
        alts = [a.code for a in sorted(mat.alternatives, key=lambda a: a.priority)] if mat else []
        return [code] + [a for a in alts if a in cfg.materials]
    if flute:
        return [c for c, m in cfg.materials.items() if m.flute.upper() == flute.upper()]
    return []


def pick_primary(cfg: Config, flute: Optional[str]) -> Optional[str]:
    """Само при зададена вълна без материал: RECOMMENDED, после STANDARD. Никога най-евтиния по подразбиране."""
    cands = candidates_for(cfg, flute, None)
    for tag in ("RECOMMENDED", "STANDARD"):
        for c in cands:
            if tag in cfg.materials[c].tags:
                return c
    return None


def recommend(cfg: Config, req: CalculateRequest, primary: Optional[str], volume: Optional[Decimal],
              costs: dict[str, MaterialCost]) -> MaterialRecommendation:
    reasons: list[str] = []
    missing: list[str] = []
    sc = size_class(cfg, volume)
    if req.load_kg is None:
        missing.append("load_kg")
    if req.stacking is None:
        missing.append("stacking")
    if req.export is None:
        missing.append("export")
    if req.rough_handling is None:
        missing.append("rough_handling")

    risk = "UNKNOWN" if req.load_kg is None else "MEDIUM"
    if sc == "XL" and req.load_kg is None:
        risk = "HIGH"
        reasons.append("XL_UNKNOWN_LOAD")
    if (req.environment in ("humid", "wet", "cold")) or req.export or sc in ("LARGE", "XL"):
        reasons.append("KRAFT_PREFERRED")
    if req.material_code:
        reasons.append("USER_SELECTED")
    elif primary:
        reasons.append("TAG_RECOMMENDED_FOR_FLUTE")
    else:
        reasons.append("NO_RECOMMENDED_MATERIAL")

    prim_cost = costs.get(primary) if primary else None
    prim_total = prim_cost.total.amount if prim_cost and prim_cost.total else None
    mat = cfg.materials.get(primary) if primary else None
    alt_types = {a.code: a for a in (mat.alternatives if mat else [])}

    alternatives = []
    economy = upgrade = None
    economy_validated = False
    for code, c in costs.items():
        if code == primary:
            continue
        total = c.total.amount if c.total else None
        alt = alt_types.get(code)
        entry = {"code": code, "status": c.status, "alternative_type": alt.type if alt else None,
                 "technical_equivalence": bool(alt and alt.technical_equivalence),
                 "total": c.total.model_dump(mode="json") if c.total else None,
                 "cost_per_item": c.cost_per_item.model_dump(mode="json") if c.cost_per_item else None}
        if total is not None and prim_total:
            entry["difference_pct"] = str(clean((total - prim_total) * 100 / prim_total))
            if total < prim_total and (alt is None or alt.type in ("ECONOMY", "SIMILAR")):
                if economy is None or total < costs[economy].total.amount:
                    economy = code
                    economy_validated = bool(alt and alt.technical_equivalence)
            if alt and alt.type == "UPGRADE" and total >= prim_total:
                if (total - prim_total) * 100 / prim_total <= cfg.settings.material_upgrade_threshold_pct:
                    upgrade = code
                    reasons.append("UPGRADE_WITHIN_THRESHOLD")
        alternatives.append(entry)

    if economy:
        reasons.append("ECONOMY_AVAILABLE" if economy_validated else "ECONOMY_NOT_TECHNICALLY_VALIDATED")
    return MaterialRecommendation(
        recommended=primary, economy=economy, upgrade=upgrade, risk=risk,
        technical_validation_status="APPROVED" if economy and economy_validated else "NOT_VALIDATED",
        size_class=sc, missing_inputs=missing, reason_codes=reasons, alternatives=alternatives)
