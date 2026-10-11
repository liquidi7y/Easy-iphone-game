"""Вход, сесии, CSRF, заключване, смяна на парола, API токени, роли, rate limiting."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from conftest import PASSWORD, login_client

BOX = {"box": {"length_mm": 1192, "width_mm": 792, "height_mm": 779}, "material_code": "EC3008", "quantity": 160}


@pytest.fixture()
def anon(db):
    from calc.api.app import app
    return TestClient(app)


def test_calculation_requires_login(anon):
    assert anon.post("/api/v1/calculate", json=BOX).status_code == 401
    assert anon.get("/api/v1/health").json()["status"] == "ok"


def test_login_cookie_and_logout(anon):
    r = anon.post("/api/v1/auth/login", json={"username": "sales", "password": PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert anon.get("/api/v1/me").json()["role"] == "sales"
    # CSRF: промяна с cookie без заглавката се отказва
    assert anon.post("/api/v1/calculate", json=BOX).status_code == 403
    assert anon.post("/api/v1/calculate", json=BOX, headers={"X-Requested-With": "calc"}).status_code == 200
    anon.post("/api/v1/auth/logout", headers={"X-Requested-With": "calc"})
    assert anon.get("/api/v1/me").status_code == 401


def test_wrong_password_generic_error_and_lockout(anon, db):
    for _ in range(5):
        r = anon.post("/api/v1/auth/login", json={"username": "sales", "password": "грешна-парола"})
        assert r.status_code == 401
    unknown = anon.post("/api/v1/auth/login", json={"username": "nobody", "password": "грешна-парола"})
    assert unknown.json()["detail"] == r.json()["detail"]  # не издава дали потребителят съществува
    # заключен: и правилната парола не минава
    assert anon.post("/api/v1/auth/login", json={"username": "sales", "password": PASSWORD}).status_code == 401
    with create_engine(db).connect() as c:
        n = c.execute(text("select count(*) from audit_log where action = 'auth.login_failed'")).scalar()
        locked = c.execute(text("select locked_until from users where username = 'sales'")).scalar()
    assert n == 7 and locked is not None


def test_idle_timeout(db):
    c = login_client("sales")
    assert c.get("/api/v1/me").status_code == 200
    with create_engine(db).begin() as conn:
        conn.execute(text("update user_sessions set last_seen_at = :t"),
                      {"t": datetime.now(timezone.utc) - timedelta(minutes=31)})
    assert c.get("/api/v1/me").status_code == 401


def test_password_not_stored_in_plain_text(db):
    with create_engine(db).connect() as c:
        ph = c.execute(text("select password_hash from users where username = 'sales'")).scalar()
    assert ph.startswith("scrypt$") and PASSWORD not in ph


def test_admin_creates_user_must_change_password(db):
    admin = login_client("admin")
    r = admin.post("/api/v1/users", json={"username": "ivan", "display_name": "Иван", "role": "sales",
                                          "password": "късa"})
    assert r.status_code == 422  # твърде кратка
    r = admin.post("/api/v1/users", json={"username": "ivan", "display_name": "Иван", "role": "sales",
                                          "password": "Начална-парола-1"})
    assert r.status_code == 200 and r.json()["must_change_password"]
    ivan = login_client("ivan", "Начална-парола-1")
    assert ivan.post("/api/v1/calculate", json=BOX).status_code == 403
    r = ivan.post("/api/v1/auth/change-password", json={"current_password": "Начална-парола-1",
                                                        "new_password": "Моята-нова-парола"})
    assert r.status_code == 200
    assert ivan.post("/api/v1/calculate", json=BOX).status_code == 200
    assert login_client("ivan", "Моята-нова-парола").get("/api/v1/me").json()["username"] == "ivan"


def test_deactivation_and_role_change_take_effect_immediately(db):
    admin, sales = login_client("admin"), login_client("sales")
    uid = sales.get("/api/v1/me").json()["id"]
    admin.patch(f"/api/v1/users/{uid}", json={"active": False})
    assert sales.get("/api/v1/me").status_code == 401
    assert admin.patch(f"/api/v1/users/{admin.get('/api/v1/me').json()['id']}",
                       json={"active": False}).status_code == 422


def test_api_token_for_erp(db, anon):
    admin = login_client("admin")
    uid = login_client("sales").get("/api/v1/me").json()["id"]
    t = admin.post(f"/api/v1/users/{uid}/tokens", json={"name": "ERP"}).json()
    assert t["token"].startswith("cc_")
    h = {"Authorization": f"Bearer {t['token']}"}
    r = anon.post("/api/v1/calculate", json=BOX, headers=h)  # без cookie и без CSRF
    assert r.status_code == 200 and r.json()["material"]["total"]["amount"] == "1067.8979328"
    listed = admin.get(f"/api/v1/users/{uid}/tokens").json()
    assert "token" not in listed[0] and listed[0]["last_used_at"]
    admin.delete(f"/api/v1/tokens/{t['id']}")
    assert anon.post("/api/v1/calculate", json=BOX, headers=h).status_code == 401
    with create_engine(db).connect() as c:
        stored = c.execute(text("select token_hash from api_tokens")).scalar()
    assert t["token"] not in stored


def test_role_checks(db):
    viewer, sales = login_client("viewer"), login_client("sales")
    assert viewer.post("/api/v1/calculate", json=BOX).status_code == 403
    assert sales.get("/api/v1/users").status_code == 403
    assert viewer.get("/api/v1/price-lists").status_code == 200


def test_rate_limit(db, monkeypatch, anon):
    monkeypatch.setenv("LOGIN_RATE_LIMIT_PER_MINUTE", "3")
    codes = [anon.post("/api/v1/auth/login", json={"username": "x", "password": "y"}).status_code for _ in range(5)]
    assert codes[:3] == [401] * 3 and codes[3:] == [429, 429]
    from calc.auth import limiter
    limiter.reset()
    c = login_client("sales")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "5")
    codes = [c.get("/api/v1/me").status_code for _ in range(8)]
    assert 429 in codes
