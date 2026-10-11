"""Връзка с PostgreSQL. DATABASE_URL, напр. postgresql+psycopg://user:pass@db:5432/calc"""

from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def database_url() -> Optional[str]:
    return os.environ.get("DATABASE_URL")


@lru_cache(maxsize=4)
def get_engine(url: Optional[str] = None) -> Engine:
    url = url or database_url()
    if not url:
        raise RuntimeError("DATABASE_URL не е зададен")
    return create_engine(url, pool_pre_ping=True, future=True)


@lru_cache(maxsize=4)
def _factory(url: Optional[str] = None) -> sessionmaker:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


@contextmanager
def session_scope(url: Optional[str] = None) -> Iterator[Session]:
    s = _factory(url)()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s
