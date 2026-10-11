"""Зареждане на engine Config от PostgreSQL и начален seed от JSON."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engine import config as ec
from ..engine.numbers import clean
from .audit import audit
from .models import (BoxStyle, ExchangeRate, FormulaVersion, Material, MaterialAlternative, MaterialPriceTier,
                     MaterialTag, PriceList, PriceListVersion, PriceTier, RuleVersion, Supplier, User)

ENGINE_SETTINGS = "engine_settings"


def _c(v: Optional[Decimal]) -> Optional[Decimal]:
    return clean(v) if v is not None else None


def build_config(s: Session) -> ec.Config:
    rv = s.scalar(select(RuleVersion).where(RuleVersion.kind == ENGINE_SETTINGS, RuleVersion.active))
    if rv is None:
        raise RuntimeError("Няма активна версия на engine настройките (rule_versions)")
    settings = rv.data["settings"]

    rates = [ec.ExchangeRate.model_validate({"from": r.from_ccy, "to": r.to_ccy, "rate": clean(r.rate),
                                             "date": r.rate_date.isoformat(), "source": r.source})
             for r in s.scalars(select(ExchangeRate).order_by(ExchangeRate.rate_date.desc(), ExchangeRate.id.desc()))]

    styles = {}
    for fv in s.scalars(select(FormulaVersion).where(FormulaVersion.active)):
        bs = s.get(BoxStyle, fv.box_style_code)
        styles[fv.box_style_code] = ec.BoxStyle(name=bs.name, formula=fv.formula, formula_version=fv.version,
                                                status=fv.status, additives=fv.additives,
                                                additive_overrides=fv.additive_overrides or [])

    materials = {}
    for m in s.scalars(select(Material)):
        materials[m.code] = ec.Material(
            display_code=m.display_code, factory_code=m.factory_code, flute=m.flute, layers=m.layers,
            paper_combination=m.paper_combination, kraft_outer=m.kraft_outer, kraft_inner=m.kraft_inner,
            grammage_gsm=_c(m.grammage_gsm), technical=m.technical or [], tags=[t.tag for t in m.tags],
            alternatives=[ec.Alternative(code=a.alternative_code, type=a.type, priority=a.priority,
                                         technical_equivalence=a.technical_equivalence,
                                         equivalence_conditions=a.equivalence_conditions)
                          for a in m.alternatives])

    price_lists = []
    q = (select(PriceListVersion).where(PriceListVersion.status.in_(("active", "reference")))
         .order_by(PriceListVersion.status, PriceListVersion.id))
    for v in s.scalars(q):
        prices: dict[str, dict[str, Decimal]] = {}
        for p in v.prices:
            prices.setdefault(p.material_code, {})[p.tier.code] = clean(p.price)
        price_lists.append(ec.PriceList(
            id=v.price_list.code, version=v.version, supplier=v.price_list.supplier.name,
            company_contract=v.price_list.company_contract, currency=v.currency, price_unit=v.price_unit,
            tax_basis=v.tax_basis, status=v.status, valid_from=v.valid_from.isoformat(),
            valid_to=v.valid_to.isoformat() if v.valid_to else None, tier_basis=v.tier_basis,
            order_rules=ec.OrderRules(min_order_m2=_c(v.min_order_m2), min_invoiced_m2=_c(v.min_invoiced_m2)),
            tiers=[ec.Tier(code=t.code, lower_m2=clean(t.lower_m2), lower_inclusive=t.lower_inclusive,
                           upper_m2=_c(t.upper_m2), upper_inclusive=t.upper_inclusive) for t in v.tiers],
            prices=prices))

    return ec.Config(test_data_only=bool(rv.data.get("test_data_only")), rule_version=rv.version,
                     settings=ec.Settings.model_validate(settings), exchange_rates=rates, box_styles=styles,
                     materials=materials, price_lists=price_lists)


def ensure_user(s: Session, username: str, role: str, display_name: Optional[str] = None) -> User:
    u = s.scalar(select(User).where(User.username == username))
    if u is None:
        u = User(username=username, role=role, display_name=display_name or username)
        s.add(u)
        s.flush()
        audit(s, None, "create", "user", u.id, new={"username": username, "role": role})
    return u


def seed_from_json(s: Session, path: Optional[str | Path] = None, actor: Optional[User] = None) -> None:
    """Еднократно зарежда тестовите/началните данни. Не презаписва съществуващи записи."""
    path = Path(path or ec.DEFAULT_CONFIG)
    data = json.loads(path.read_text(encoding="utf-8"), parse_float=str)
    cfg = ec.Config.model_validate(data)
    actor_id = actor.id if actor else None

    if s.scalar(select(RuleVersion).where(RuleVersion.kind == ENGINE_SETTINGS,
                                          RuleVersion.version == cfg.rule_version)) is None:
        s.add(RuleVersion(kind=ENGINE_SETTINGS, version=cfg.rule_version, active=True, created_by=actor_id,
                          data={"settings": data["settings"], "test_data_only": cfg.test_data_only}))
    if s.scalar(select(ExchangeRate)) is None:
        for r in cfg.exchange_rates:
            s.add(ExchangeRate(from_ccy=r.from_, to_ccy=r.to, rate=r.rate, rate_date=date.fromisoformat(r.date),
                               source=r.source))
    for code, st in cfg.box_styles.items():
        if s.get(BoxStyle, code) is None:
            s.add(BoxStyle(code=code, name=st.name))
            s.flush()
            s.add(FormulaVersion(box_style_code=code, formula=st.formula, version=st.formula_version,
                                 status=st.status, active=True,
                                 additives={k: str(v) for k, v in st.additives.items()},
                                 additive_overrides=[o.model_dump(mode="json") for o in st.additive_overrides]))
    for code, m in cfg.materials.items():
        if s.get(Material, code) is None:
            s.add(Material(code=code, display_code=m.display_code, factory_code=m.factory_code, flute=m.flute,
                           layers=m.layers, paper_combination=m.paper_combination, kraft_outer=m.kraft_outer,
                           kraft_inner=m.kraft_inner, grammage_gsm=_c(m.grammage_gsm),
                           technical=[t.model_dump(mode="json") for t in m.technical],
                           tags=[MaterialTag(tag=t) for t in m.tags]))
    s.flush()
    for code, m in cfg.materials.items():
        mat = s.get(Material, code)
        if not mat.alternatives:
            for a in m.alternatives:
                mat.alternatives.append(MaterialAlternative(alternative_code=a.code, type=a.type, priority=a.priority,
                                                            technical_equivalence=a.technical_equivalence))
    s.flush()

    for pl in cfg.price_lists:
        if s.scalar(select(PriceList).where(PriceList.code == pl.id)) is not None:
            continue
        sup = s.scalar(select(Supplier).where(Supplier.name == pl.supplier)) or Supplier(name=pl.supplier)
        s.add(sup)
        s.flush()
        plist = PriceList(code=pl.id, supplier_id=sup.id, company_contract=pl.company_contract)
        s.add(plist)
        s.flush()
        v = PriceListVersion(price_list_id=plist.id, version=pl.version, status="draft", currency=pl.currency,
                             price_unit=pl.price_unit, tax_basis=pl.tax_basis,
                             valid_from=date.fromisoformat(pl.valid_from),
                             valid_to=date.fromisoformat(pl.valid_to) if pl.valid_to else None,
                             tier_basis=pl.tier_basis, min_order_m2=pl.order_rules.min_order_m2,
                             min_invoiced_m2=pl.order_rules.min_invoiced_m2, created_by=actor_id,
                             notes="Seed от тестов JSON")
        s.add(v)
        s.flush()
        tiers = {}
        for i, t in enumerate(pl.tiers):
            tiers[t.code] = PriceTier(version_id=v.id, code=t.code, sort=i, lower_m2=t.lower_m2,
                                      lower_inclusive=t.lower_inclusive, upper_m2=t.upper_m2,
                                      upper_inclusive=t.upper_inclusive)
            s.add(tiers[t.code])
        s.flush()
        for mcode, per_tier in pl.prices.items():
            for tcode, price in per_tier.items():
                s.add(MaterialPriceTier(version_id=v.id, tier_id=tiers[tcode].id, material_code=mcode, price=price,
                                        source_ref={"seed": str(path.name)}))
        s.flush()
        # draft -> approved -> (active | reference): само разрешени преходи
        v.status = "approved"
        s.flush()
        if pl.status in ("active", "reference"):
            v.status = pl.status
            s.flush()
        audit(s, actor_id, "seed", "price_list_version", v.id, new={"code": pl.id, "version": pl.version,
                                                                     "status": v.status})
