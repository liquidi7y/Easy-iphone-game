"""Публичен клиентски модел на оферта (allowlist).

Строи се поле по поле само от разрешени стойности: спецификация, количество, продажна цена, палети,
доставка, валидност и условия. Себестойност, покупни цени, марж, прагове, ценоразписи и вътрешни бележки
нямат поле тук, затова не могат да попаднат в клиентския изход.
"""

from __future__ import annotations

import html
from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict

from ..db.models import Quotation, QuotationVersion
from ..engine.numbers import mul, quantize


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientSpec(_Strict):
    dimensions: Optional[str] = None
    construction: Optional[str] = None
    material: Optional[str] = None
    flute: Optional[str] = None


class ClientLine(_Strict):
    position: int
    description: str
    specification: Optional[ClientSpec] = None
    quantity: int
    unit: str
    unit_price: str
    total: str


class ClientCustomer(_Strict):
    name: str
    vat_number: Optional[str] = None


class ClientQuotation(_Strict):
    number: str
    version: int
    date: Optional[str]
    draft: bool
    customer: Optional[ClientCustomer]
    currency: str
    prices_note: str
    lines: list[ClientLine]
    pallets: list[ClientLine]
    total_products: str
    total_pallets: str
    total: str
    valid_until: Optional[str]
    delivery_terms: Optional[str]
    payment_terms: Optional[str]
    notes: Optional[str]


def _money(x: Decimal) -> str:
    return str(quantize(x, 2))


def build_client_quotation(q: Quotation, v: QuotationVersion, display_codes: dict[str, str]) -> ClientQuotation:
    lines, pallets = [], []
    tot_p = tot_pal = Decimal(0)
    for l in v.lines:
        total = mul(l.unit_sales_price, Decimal(l.quantity))
        if l.kind == "product":
            tot_p += total
            res = l.result or {}
            blank = res.get("blank") or {}
            req = l.request or {}
            box = req.get("box") or {}
            sheet = req.get("sheet") or {}
            if box:
                dims = f"{box['length_mm']}×{box['width_mm']}×{box['height_mm']} mm " \
                       f"({'вътрешни' if blank.get('dimension_basis') == 'internal' else 'външни'})"
            elif sheet:
                dims = f"{sheet['length_mm']}×{sheet['width_mm']} mm"
            else:
                dims = None
            mat = l.material_code
            spec = ClientSpec(dimensions=dims, construction=f"FEFCO {blank['fefco']}" if blank.get("fefco") else None,
                              material=display_codes.get(mat, mat) if mat else None,
                              flute=(l.snapshot or {}).get("material", {}).get("flute") if l.snapshot else None)
            lines.append(ClientLine(position=l.position, description=l.description or "", specification=spec,
                                    quantity=l.quantity, unit="бр.", unit_price=_money(l.unit_sales_price),
                                    total=_money(total)))
        else:
            tot_pal += total
            pallets.append(ClientLine(position=l.position, description=l.description or "", quantity=l.quantity,
                                      unit="бр.", unit_price=_money(l.unit_sales_price), total=_money(total)))
    when = v.finalized_at.date().isoformat() if v.finalized_at else date.today().isoformat()
    return ClientQuotation(
        number=q.number, version=v.version, date=when, draft=v.status == "draft",
        customer=ClientCustomer(name=q.customer.name, vat_number=q.customer.vat_number) if q.customer else None,
        currency=v.currency, prices_note="Цените са без ДДС.", lines=lines, pallets=pallets,
        total_products=_money(tot_p), total_pallets=_money(tot_pal), total=_money(tot_p + tot_pal),
        valid_until=v.valid_until.isoformat() if v.valid_until else None, delivery_terms=v.delivery_terms,
        payment_terms=v.payment_terms, notes=v.client_notes)


def render_html(c: ClientQuotation) -> str:
    e = lambda x: html.escape(str(x)) if x is not None else ""  # noqa: E731

    def rows(items, with_spec):
        out = []
        for l in items:
            spec = ""
            if with_spec and l.specification:
                sp = l.specification
                spec = "<br><small>" + " · ".join(e(x) for x in (sp.construction, sp.dimensions, sp.material,
                                                                    sp.flute and f"вълна {sp.flute}") if x) + "</small>"
            out.append(f"<tr><td>{l.position}</td><td>{e(l.description)}{spec}</td><td class=n>{l.quantity}</td>"
                       f"<td class=n>{e(l.unit_price)}</td><td class=n>{e(l.total)}</td></tr>")
        return "".join(out)

    head = "<tr><th>№</th><th>Описание</th><th class=n>Количество</th><th class=n>Ед. цена</th><th class=n>Сума</th></tr>"
    pallets = (f"<h3>Палети</h3><table>{head}{rows(c.pallets, False)}</table>" if c.pallets else "")
    terms = "".join(f"<p><b>{label}:</b> {e(val)}</p>" for label, val in (
        ("Валидност", c.valid_until), ("Доставка", c.delivery_terms), ("Плащане", c.payment_terms),
        ("Бележки", c.notes)) if val)
    customer = f"<p><b>Клиент:</b> {e(c.customer.name)}{' · ' + e(c.customer.vat_number) if c.customer and c.customer.vat_number else ''}</p>" if c.customer else ""
    draft = '<p class=draft>ЧЕРНОВА — не е за изпращане</p>' if c.draft else ""
    return f"""<!doctype html><html lang="bg"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Оферта {e(c.number)}</title>
<style>body{{font:14px/1.45 system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#111;background:#fff}}
table{{border-collapse:collapse;width:100%;margin:8px 0}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}
.n{{text-align:right;white-space:nowrap}}.tot td{{font-weight:600;border:0}}.draft{{color:#b00;font-weight:700}}
small{{color:#555}}@media print{{.noprint{{display:none}}}}</style></head><body>
<p class=noprint><button onclick="print()">Печат / PDF</button></p>
{draft}<h2>Оферта {e(c.number)} · версия {c.version}</h2><p>Дата: {e(c.date)}</p>{customer}
<table>{head}{rows(c.lines, True)}</table>{pallets}
<table class=tot><tr><td>Кашони</td><td class=n>{e(c.total_products)} {e(c.currency)}</td></tr>
<tr><td>Палети</td><td class=n>{e(c.total_pallets)} {e(c.currency)}</td></tr>
<tr><td>Общо</td><td class=n>{e(c.total)} {e(c.currency)}</td></tr></table>
<p>{e(c.prices_note)}</p>{terms}</body></html>"""
