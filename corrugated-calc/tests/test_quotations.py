"""Оферти: snapshots, неизменимост, версии, преизчисляване, палети, търсене, клиентски изход; регистри."""

import json
from decimal import Decimal as D

import pytest
from sqlalchemy import create_engine, text

import fixtures_gen as g
from conftest import login_client

BOX = {"length_mm": 1192, "width_mm": 792, "height_mm": 779}
REQ = {"box": BOX, "material_code": "EC3008", "quantity": 160}
INTERNAL_NOTE = "Да не се предлага под EC3008"


@pytest.fixture()
def sales(db):
    return login_client("sales")


@pytest.fixture()
def admin(db):
    return login_client("admin")


@pytest.fixture()
def manager(db):
    return login_client("manager")


def _pallet(manager, code="EUR", purchase="18.50", sales_price="22.00"):
    manager.post("/api/v1/pallet-types", json={"code": code, "length_mm": 1200, "width_mm": 800,
                                               "description": "EUR палет"})
    r = manager.post(f"/api/v1/pallet-types/{code}/prices",
                     json={"purchase_price": purchase, "sales_price": sales_price, "currency": "BGN",
                           "valid_from": "2026-01-01"})
    assert r.status_code == 200, r.text


def _quote(sales, manager, margin="20", with_pallets=True):
    c = sales.post("/api/v1/customers", json={"name": "Клиент АД", "vat_number": "BG123456789",
                                              "internal_notes": INTERNAL_NOTE}).json()
    q = sales.post("/api/v1/quotations", json={"customer_id": c["id"], "currency": "BGN",
                                               "valid_until": "2026-12-31", "delivery_terms": "Франко склад клиент",
                                               "internal_notes": "вътрешно: клиентът натиска за цена"}).json()
    r = sales.post(f"/api/v1/quotations/{q['quotation_id']}/versions/1/lines",
                   json={"request": REQ, "target_margin_pct": margin})
    assert r.status_code == 200, r.text
    if with_pallets:
        _pallet(manager)
        r = sales.post(f"/api/v1/quotations/{q['quotation_id']}/versions/1/pallets",
                       json={"pallet_code": "EUR", "count": 4})
        assert r.status_code == 200, r.text
    return q["quotation_id"], c["id"], r.json()


def test_product_and_pallet_lines_separate_results(sales, manager):
    qid, _, v = _quote(sales, manager)
    prod, pal = v["lines"]
    assert D(prod["unit_cost"]) == D("6.67436208")
    # 6.67436208 / 0.8 = 8.3429526 → офертна цена 8.34 (rounding policy), марж се смята от нея
    assert prod["unit_sales_price"] == "8.34"
    assert prod["snapshot"]["unit_price_rounding"]["exact_from_pricing"] == "8.3429526"
    t = v["totals"]
    assert t["products"]["sales"]["amount"] == "1334.4"  # = 8.34 × 160
    profit = D("1334.4") - D("1067.8979328")
    assert D(t["products"]["margin_percent"]) == profit * 100 / D("1334.4")
    assert D(t["products"]["markup_percent"]) == profit * 100 / D("1067.8979328")
    assert t["products"]["cost_basis_label"] == "Марж спрямо материалната себестойност"
    assert D(t["products"]["cost"]["amount"]) == D("1067.8979328")  # без палети
    assert t["pallets"]["sales"]["amount"] == "88" and t["pallets"]["cost"]["amount"] == "74"
    assert t["pallets"]["profit"]["amount"] == "14"
    assert D(t["deal_total"]["amount"]) == D(t["products"]["sales"]["amount"]) + 88
    assert pal["snapshot"]["price_record"]["purchase_price"] == "18.5"


def test_finalize_freezes_and_snapshot_contents(sales, manager, db):
    qid, _, _ = _quote(sales, manager)
    v = sales.post(f"/api/v1/quotations/{qid}/versions/1/finalize").json()
    assert v["status"] == "final" and v["totals"]["deal_total"]
    snap = v["lines"][0]["snapshot"]
    assert snap["formula"]["formula_version"] == "0201-v1" and snap["formula"]["additives"]["glue_flap_mm"] == "40"
    assert snap["material"]["code"] == "EC3008" and snap["material"]["flute"] == "EC"
    assert snap["price_list"]["id"] == "TEST-SUPPLIER-2026" and snap["price_list"]["applied_tier"]["code"] == "T1"
    assert snap["rounding"]["mode"] == "ROUND_HALF_UP" and snap["exchange_rates"]
    assert snap["rule_version"] and snap["engine_version"]
    r = sales.post(f"/api/v1/quotations/{qid}/versions/1/lines", json={"request": REQ, "target_margin_pct": "10"})
    assert r.status_code == 422
    assert sales.patch(f"/api/v1/quotations/{qid}/versions/1", json={"client_notes": "x"}).status_code == 422
    with create_engine(db).begin() as c, pytest.raises(Exception, match="не се променят"):
        c.execute(text("update quotation_lines set unit_sales_price = 1"))
    with create_engine(db).begin() as c, pytest.raises(Exception, match="не се променя"):
        c.execute(text("update quotation_versions set totals = '{}'::jsonb"))


def _activate_new_price(manager, admin, t1="1200,00", t2="1100,00"):
    files = [("files", ("new.xlsx", g.xlsx(g.table_with({"EC3008": (t1, t2), "EC4027": ("1003,00", "")}))))]
    job = manager.post("/api/v1/imports", data={"price_list_code": "NEW-2026", "supplier": "Нов"}, files=files).json()
    manager.post(f"/api/v1/imports/{job['id']}/edits", json={"ops": [{"op": "confirm_tiers"}]})
    assert manager.post(f"/api/v1/imports/{job['id']}/validate").json()["ok"]
    assert manager.post(f"/api/v1/imports/{job['id']}/approve", json={"acknowledge_warnings": True}).status_code == 200
    assert admin.post("/api/v1/price-lists/NEW-2026/versions/1/activate").status_code == 200


def test_price_list_change_keeps_old_quotation_identical(sales, manager, admin):
    qid, _, _ = _quote(sales, manager)
    before = sales.post(f"/api/v1/quotations/{qid}/versions/1/finalize").json()
    _activate_new_price(manager, admin)
    calc = sales.post("/api/v1/calculate", json=REQ).json()
    assert calc["material"]["source"]["price_list_id"] == "NEW-2026"  # текущата сметка е с новата цена
    after = sales.get(f"/api/v1/quotations/{qid}/versions/1").json()
    assert json.dumps(after["lines"], sort_keys=True) == json.dumps(before["lines"], sort_keys=True)
    assert after["totals"] == before["totals"]

    # редакция = нова версия със същите числа; преизчисляването е отделно действие
    v2 = sales.post(f"/api/v1/quotations/{qid}/versions/1/new-version").json()
    assert v2["version"] == 2 and v2["based_on_version"] == 1 and v2["status"] == "draft"
    assert v2["lines"][0]["unit_cost"] == before["lines"][0]["unit_cost"]
    r = sales.post(f"/api/v1/quotations/{qid}/versions/2/recalculate").json()
    assert len(r["changes"]) == 1 and r["changes"][0]["new"]["price_list"] == {"id": "NEW-2026", "version": 1}
    assert D(r["lines"][0]["unit_cost"]) == D("1007.45088") * D("1.2") / 160
    assert sales.get(f"/api/v1/quotations/{qid}/versions/1").json()["lines"][0]["unit_cost"] == "6.67436208"


def test_client_output_has_no_internal_data(sales, manager):
    qid, _, _ = _quote(sales, manager)
    sales.post(f"/api/v1/quotations/{qid}/versions/1/finalize")
    client = sales.get(f"/api/v1/quotations/{qid}/versions/1/client").json()
    blob = json.dumps(client, ensure_ascii=False)
    for forbidden in (INTERNAL_NOTE, "вътрешно", "cost", "margin", "markup", "purchase", "TEST-SUPPLIER",
                      "Тестов доставчик", "tier", "6.67436208", "18.5", "price_list", "snapshot"):
        assert forbidden not in blob, forbidden
    assert client["lines"][0]["specification"]["material"] == "EC3008"
    assert client["pallets"][0]["quantity"] == 4 and client["pallets"][0]["unit_price"] == "22.00"
    line = client["lines"][0]
    assert D(line["unit_price"]) * line["quantity"] == D(line["total"])  # клиентът вижда съгласувани суми
    assert client["total"] == "1422.40"
    assert client["prices_note"] == "Цените са без ДДС."
    html = sales.get(f"/quotations/{qid}/v/1/print")
    assert html.status_code == 200 and "Клиент АД" in html.text
    for forbidden in (INTERNAL_NOTE, "вътрешно", "18.50", "6.67", "Тестов доставчик"):
        assert forbidden not in html.text, forbidden


def test_status_flow_and_roles(sales, manager, db):
    qid, _, _ = _quote(sales, manager, with_pallets=False)
    viewer = login_client("viewer")
    assert viewer.post("/api/v1/quotations", json={}).status_code == 403
    assert viewer.get(f"/api/v1/quotations/{qid}/versions/1").status_code == 200
    assert sales.post(f"/api/v1/quotations/{qid}/versions/1/status", json={"status": "sent"}).status_code == 422
    sales.post(f"/api/v1/quotations/{qid}/versions/1/finalize")
    assert sales.post(f"/api/v1/quotations/{qid}/versions/1/status", json={"status": "sent"}).json()["status"] == "sent"
    assert sales.post(f"/api/v1/quotations/{qid}/versions/1/status",
                      json={"status": "accepted"}).json()["status"] == "accepted"
    assert sales.post(f"/api/v1/quotations/{qid}/versions/1/status", json={"status": "sent"}).status_code == 422
    with create_engine(db).connect() as c:
        actions = c.execute(text("select action from audit_log where entity in ('quotation','quotation_version') "
                                 "order by id")).scalars().all()
    assert {"quotation.create", "quotation.line_add", "quotation.finalize", "quotation.sent",
            "quotation.accepted"} <= set(actions)


def test_no_invented_price_for_line(sales):
    q = sales.post("/api/v1/quotations", json={"currency": "BGN"}).json()
    r = sales.post(f"/api/v1/quotations/{q['quotation_id']}/versions/1/lines",
                   json={"request": {**REQ, "quantity": 100}, "target_margin_pct": "20"})
    assert r.status_code == 422 and "BELOW_MIN_ORDER" in r.json()["detail"]
    r = sales.post(f"/api/v1/quotations/{q['quotation_id']}/versions/1/lines",
                   json={"request": REQ, "target_margin_pct": "20", "markup_pct": "10"})
    assert r.status_code == 422


def test_eur_quotation_uses_configured_rate(sales):
    q = sales.post("/api/v1/quotations", json={"currency": "EUR"}).json()
    v = sales.post(f"/api/v1/quotations/{q['quotation_id']}/versions/1/lines",
                   json={"request": REQ, "sales_price_per_item": "4.50"}).json()
    line = v["lines"][0]
    assert D(line["unit_cost"]) == D("6.67436208") / D("1.95583")
    assert line["result"]["sales"]["exchange_rate"]["rate"] == "1/1.95583"


def test_search_exact_near_and_history(sales, manager):
    qid, cid, _ = _quote(sales, manager, with_pallets=False)
    sales.post(f"/api/v1/quotations/{qid}/versions/1/finalize")
    r = sales.get("/api/v1/quotation-search", params={"length_mm": 1192, "width_mm": 792, "height_mm": 779,
                                                      "material_code": "EC3008"}).json()
    assert len(r["exact"]) == 1 and r["exact"][0]["number"].startswith("Q")
    r = sales.get("/api/v1/quotation-search", params={"length_mm": 1200, "width_mm": 792, "height_mm": 779}).json()
    assert not r["exact"] and r["near"][0]["label"] == "Подобен, не идентичен"
    assert "размери" in r["near"][0]["differences"][0]
    r = sales.get("/api/v1/quotation-search", params={"length_mm": 1192, "width_mm": 792, "height_mm": 779,
                                                      "material_code": "EC4027"}).json()
    assert not r["exact"] and "материал EC3008" in r["near"][0]["differences"]
    r = sales.get("/api/v1/quotation-search", params={"length_mm": 600, "width_mm": 400, "height_mm": 300}).json()
    assert r == {**r, "exact": [], "near": []}
    hist = sales.get(f"/api/v1/customers/{cid}/history").json()
    assert hist[0]["quantity"] == 160 and hist[0]["material_code"] == "EC3008"


# ---------------------------------------------------------------- регистри

def test_materials_registry_rules(db, admin):
    tech, sales = login_client("tech"), login_client("sales")
    body = {"code": "EC5000", "flute": "EC", "layers": 5, "tags": ["STANDARD"],
            "technical": [{"name": "ECT", "value": "7.5", "unit": "kN/m", "source": "лист от доставчика"}]}
    assert sales.post("/api/v1/materials", json=body).status_code == 403
    r = tech.post("/api/v1/materials", json=body)
    assert r.status_code == 200 and r.json()["technical"][0]["value"] == "7.5"
    assert tech.post("/api/v1/materials", json={**body, "code": "EC5001", "display_code": "EC3008"}).status_code == 409
    assert tech.put("/api/v1/materials/EC5000/tags", json={"tags": ["CHEAP"]}).status_code == 422
    alt = {"code": "EC3008", "type": "UPGRADE", "technical_equivalence": True}
    assert admin.put("/api/v1/materials/EC5000/alternatives", json=alt).status_code == 403
    assert tech.put("/api/v1/materials/EC5000/alternatives", json=alt).status_code == 422  # без условия
    r = tech.put("/api/v1/materials/EC5000/alternatives", json={**alt, "equivalence_conditions": "суха среда"})
    assert r.json()["alternatives"][0]["approved_by"] == tech.get("/api/v1/me").json()["id"]
    calc = sales.post("/api/v1/calculate", json={**REQ, "material_code": "EC5000"}).json()
    assert calc["material"]["status"] == "NO_PRICE_LIST"  # нов материал без цена → няма измислена цена
    with create_engine(db).connect() as c:
        n = c.execute(text("select count(*) from audit_log where entity = 'material' and entity_id = 'EC5000'")).scalar()
    assert n == 2  # създаване + алтернатива; отказаните заявки не променят нищо


def test_pallet_price_history_append_only(db, manager):
    _pallet(manager)
    r = manager.post("/api/v1/pallet-types/EUR/prices", json={"purchase_price": "19", "sales_price": "23",
                                                              "currency": "BGN", "valid_from": "2026-06-01"}).json()
    assert r["sales_price"] == "23" and len(r["price_history"]) == 2
    with create_engine(db).begin() as c, pytest.raises(Exception, match="не се променя"):
        c.execute(text("update pallet_prices set sales_price = 1"))


def test_material_specific_minimum_from_import(db, manager, admin, sales):
    """Колона „Мин. заявка m²“ в ценоразписа: EC4027 се поръчва минимум 2000 m²."""
    table = [["Код", "1000-1500 м2", "от 1500 м2", "Мин. заявка m2"], ["EC3008", "1060,00", "963,00", ""],
             ["EC4027", "1003,00", "950,00", "2000"]]
    files = [("files", ("min.csv", g.csv_bytes(table)))]
    job = manager.post("/api/v1/imports", data={"price_list_code": "MIN-2026", "supplier": "Мин"}, files=files).json()
    rows = {r["material"]["value"]: r for r in job["proposal"]["rows"]}
    assert rows["EC4027"]["min_order_m2"]["value"] == "2000" and rows["EC3008"]["min_order_m2"] is None
    assert rows["EC4027"]["min_order_m2"]["source"]["cell"] == "D7"
    manager.post(f"/api/v1/imports/{job['id']}/edits", json={"ops": [{"op": "confirm_tiers"}]})
    assert manager.post(f"/api/v1/imports/{job['id']}/validate").json()["ok"]
    manager.post(f"/api/v1/imports/{job['id']}/approve", json={"acknowledge_warnings": True})
    admin.post("/api/v1/price-lists/MIN-2026/versions/1/activate")
    ver = sales.get("/api/v1/price-lists/MIN-2026/versions/1").json()
    assert ver["material_min_order_m2"] == {"EC4027": "2000"}
    r = sales.post("/api/v1/calculate", json={**REQ, "material_code": "EC4027", "quantity": 240}).json()  # 1511 m²
    assert r["material"]["status"] == "BELOW_MIN_ORDER" and "2000" in r["material"]["status_message"]
    assert r["optimization"]["proposed_quantity"] == 318
    r = sales.post("/api/v1/calculate", json={**REQ, "material_code": "EC4027", "quantity": 318}).json()
    assert r["material"]["status"] == "OK" and r["material"]["tier"]["price"] == "950"
    r = sales.post("/api/v1/calculate", json={**REQ, "quantity": 160}).json()  # EC3008: общият минимум 1000
    assert r["material"]["status"] == "OK"
