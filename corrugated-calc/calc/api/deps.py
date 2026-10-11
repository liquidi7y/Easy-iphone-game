"""Общи зависимости: сесия към базата, текущ потребител и проверка на роли в API.

Потребителят се идентифицира с httpOnly cookie на сесия (браузър) или с `Authorization: Bearer cc_...`
(API токен за ERP). За cookie заявки, които променят данни, се изисква заглавка X-Requested-With: calc (CSRF).
"""

from __future__ import annotations

from typing import Iterator, Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..auth import CSRF_HEADER, CSRF_VALUE, SESSION_COOKIE, resolve_api_token, resolve_session
from ..db.models import User
from ..db.session import database_url, session_scope

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# разрешени при задължителна смяна на паролата
PASSWORD_CHANGE_PATHS = {"/api/v1/auth/change-password", "/api/v1/auth/logout", "/api/v1/me"}


def db() -> Iterator[Session]:
    if not database_url():
        raise HTTPException(503, "Базата не е конфигурирана (DATABASE_URL)")
    with session_scope() as s:
        yield s


def _bearer(request: Request) -> Optional[str]:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else None


def current_user(request: Request, s: Session = Depends(db)) -> User:
    token = _bearer(request)
    if token:
        u = resolve_api_token(s, token)
        if u is None:
            raise HTTPException(401, "Невалиден или отменен API токен")
        request.state.auth = "token"
    else:
        cookie = request.cookies.get(SESSION_COOKIE)
        u = resolve_session(s, cookie) if cookie else None
        if u is None:
            raise HTTPException(401, "Необходим е вход")
        if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise HTTPException(403, "Липсва CSRF заглавка")
        request.state.auth = "session"
        if u.must_change_password and request.url.path not in PASSWORD_CHANGE_PATHS:
            raise HTTPException(403, "Необходима е смяна на паролата")
    request.state.user_id = u.id
    return u


def require(*roles: str):
    def dep(u: User = Depends(current_user)) -> User:
        if u.role not in roles:
            raise HTTPException(403, f"Ролята {u.role} няма право за това действие")
        return u
    return dep


ALL_ROLES = ("sales", "sales_manager", "technologist", "administrator", "read_only")
CALCULATORS = ("sales", "sales_manager", "technologist", "administrator")
QUOTERS = ("sales", "sales_manager", "administrator")
PRICE_EDITORS = ("sales_manager", "administrator")
MATERIAL_EDITORS = ("technologist", "administrator")
ACTIVATORS = ("administrator",)
ADMINS = ("administrator",)
