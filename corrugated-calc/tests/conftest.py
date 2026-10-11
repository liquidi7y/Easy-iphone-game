import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

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
    from calc.db.repository import ensure_user, seed_from_json
    from calc.db.session import session_scope
    eng = create_engine(db_url)
    with eng.begin() as c:
        tables = c.execute(text("select tablename from pg_tables where schemaname='public' "
                                "and tablename <> 'alembic_version'")).scalars().all()
        c.execute(text("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE"))
    with session_scope(db_url) as s:
        admin = ensure_user(s, "admin", "administrator")
        ensure_user(s, "manager", "sales_manager")
        ensure_user(s, "sales", "sales")
        seed_from_json(s, actor=admin)
    eng.dispose()
    return db_url
