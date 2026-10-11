import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

os.environ.setdefault("COOKIE_SECURE", "0")  # TestClient работи по http
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("LOGIN_RATE_LIMIT_PER_MINUTE", "100000")

PASSWORD = "Test-parola-123"
USERS = {"admin": "administrator", "manager": "sales_manager", "sales": "sales", "tech": "technologist",
         "viewer": "read_only"}

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://postgres@/calc_test?host=/tmp&port=5433")


def _db_available() -> bool:
    try:
        from sqlalchemy import create_engine, text
        with create_engine(TEST_DB).connect() as c:
            c.execute(text("select 1"))
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def db_url():
    if not _db_available():
        pytest.skip("PostgreSQL за тестове не е достъпен (TEST_DATABASE_URL)")
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    eng = create_engine(TEST_DB)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    os.environ["DATABASE_URL"] = TEST_DB
    command.upgrade(cfg, "head")
    return TEST_DB


@pytest.fixture()
def db(db_url):
    """Чиста база за всеки тест: truncate + seed + потребители."""
    from sqlalchemy import create_engine, text
    from calc.auth import limiter
    from calc.db.repository import ensure_user, seed_from_json
    from calc.db.session import session_scope
    limiter.reset()
    eng = create_engine(db_url)
    with eng.begin() as c:
        tables = c.execute(text("select tablename from pg_tables where schemaname='public' "
                                "and tablename <> 'alembic_version'")).scalars().all()
        c.execute(text("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE"))
    ph = _password_hash()
    with session_scope(db_url) as s:
        users = {name: ensure_user(s, name, role, password_hash=ph) for name, role in USERS.items()}
        seed_from_json(s, actor=users["admin"])
    eng.dispose()
    return db_url


_PH = None


def _password_hash():
    global _PH
    if _PH is None:
        from calc.auth import hash_password
        _PH = hash_password(PASSWORD)
    return _PH


def login_client(username: str, password: str = PASSWORD):
    from fastapi.testclient import TestClient
    from calc.api.app import app
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    c.headers["X-Requested-With"] = "calc"
    return c


class AsUser:
    """client.post(url, headers={"X-User": "manager"}) → заявка от реално влязъл потребител (cookie + CSRF)."""

    def __init__(self):
        from fastapi.testclient import TestClient
        from calc.api.app import app
        self.anon = TestClient(app)
        self._clients = {}

    def _pick(self, headers):
        headers = dict(headers or {})
        name = headers.pop("X-User", None)
        if name is None:
            return self.anon, headers
        if name not in self._clients:
            self._clients[name] = login_client(name)
        return self._clients[name], headers

    def __getattr__(self, method):
        def call(url, headers=None, **kw):
            c, h = self._pick(headers)
            return getattr(c, method)(url, headers=h, **kw)
        return call


@pytest.fixture()
def client(db):
    return AsUser()


@pytest.fixture()
def sales_client(db):
    return login_client("sales")
