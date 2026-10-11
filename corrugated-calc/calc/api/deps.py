"""Общи зависимости: сесия, потребител и проверка на роли в API.

ВРЕМЕННО: потребителят се идентифицира със заглавка X-User (индивидуален акаунт от таблица users).
Това НЕ е автентикация; заменя се с локален вход/сесии в стъпката „роли и автентикация“.
"""

from __future__ import annotations

from typing import Iterator, Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import User
from ..db.session import database_url, session_scope


def db() -> Iterator[Session]:
    if not database_url():
        raise HTTPException(503, "Базата не е конфигурирана (DATABASE_URL)")
    with session_scope() as s:
        yield s


def current_user(x_user: Optional[str] = Header(default=None), s: Session = Depends(db)) -> User:
    if not x_user:
        raise HTTPException(401, "Липсва потребител (X-User)")
    u = s.scalar(select(User).where(User.username == x_user, User.active))
    if u is None:
        raise HTTPException(401, "Непознат или неактивен потребител")
    return u


def require(*roles: str):
    def dep(u: User = Depends(current_user)) -> User:
        if u.role not in roles:
            raise HTTPException(403, f"Ролята {u.role} няма право за това действие")
        return u
    return dep


PRICE_EDITORS = ("sales_manager", "administrator")
ACTIVATORS = ("administrator",)
