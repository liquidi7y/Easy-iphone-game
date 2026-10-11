from fastapi.testclient import TestClient

from calc.api.app import app
from calc.parser.quick import parse

client = TestClient(app)


def test_parse_examples():
    assert parse("1192/792/779 EC3008 160").request == {
        "box": {"length_mm": 1192, "width_mm": 792, "height_mm": 779}, "mode": "box",
        "material_code": "EC3008", "quantity": 160}
    r = parse("1192/792/779 EC 160").request
    assert r["flute"] == "EC" and "material_code" not in r
    r = parse("Лист 1200 на 800, цена 1060 лв. за 1000 квадрата").request
    assert r["sheet"] == {"length_mm": 1200, "width_mm": 800}
    assert r["manual_price"] == {"price": "1060", "unit": "per_1000m2", "currency": "BGN", "tax_basis": "excl_vat"}


def test_followups_keep_structured_state():
    s = parse("1192/792/779 EC3008 160").request
    s = parse("А за 240?", s).request
    assert s["quantity"] == 240 and s["material_code"] == "EC3008"
    s2 = parse("С BC?", s).request
    assert s2["flute"] == "BC" and "material_code" not in s2
    s3 = parse("20% марж", s).request
    assert s3["sales"]["target_margin_pct"] == "20"
    p = parse("На 4.50 евро колко остава?", s)
    assert p.intent == "what_remains" and p.request["sales"] == {"sales_price_per_item": "4.50", "currency": "EUR"}


def test_api_calculate_decimals_as_strings():
    resp = client.post("/api/v1/calculate", json={"box": {"length_mm": 1192, "width_mm": 792, "height_mm": 779},
                                                  "material_code": "EC3008", "quantity": 160})
    assert resp.status_code == 200
    d = resp.json()
    assert d["material"]["total"]["amount"] == "1067.8979328"
    assert d["blank"]["area_m2"] == "6.296568"
    assert d["trace"] and d["engine_version"]


def test_api_unsupported_fefco_422():
    resp = client.post("/api/v1/calculate", json={"box": {"length_mm": 1, "width_mm": 1, "height_mm": 1,
                                                          "fefco": "9999"}, "quantity": 1})
    assert resp.status_code == 422


def test_chat_flow_and_economy():
    d = client.post("/api/v1/chat/message", json={"text": "1192/792/779 EC 160"}).json()
    assert d["result"]["material"]["material_code"] == "EC3008"
    d = client.post("/api/v1/chat/message", json={"text": "По-евтиния?", "state": d["state"]}).json()
    assert d["result"]["material"]["material_code"] == "EC4027"
    d = client.post("/api/v1/chat/message", json={"text": "На 4.50 евро колко остава?", "state": d["state"]}).json()
    assert d["result"]["sales"]["sales_price_per_item"]["currency"] == "EUR"


def test_chat_missing_inputs_no_invented_result():
    d = client.post("/api/v1/chat/message", json={"text": "здравей"}).json()
    assert d["result"] is None and d["parse"]["missing_inputs"]


def test_form_works_without_ai():
    # формата вика директно /calculate; не зависи от chat/LLM
    resp = client.post("/api/v1/calculate", json={"sheet": {"length_mm": 1200, "width_mm": 800},
                                                  "manual_price": {"price": "1060", "unit": "per_1000m2",
                                                                   "currency": "BGN"}})
    assert resp.json()["material"]["cost_per_sheet"]["amount"] == "1.0176"


def test_index_served():
    assert client.get("/").status_code == 200
