"""Вход, изход, смяна на парола, управление на потребители и API токени."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth
from ..db.audit import audit
from ..db.models import ROLES, ApiToken, User
from .deps import ADMINS, current_user, db, require

router = APIRouter(prefix="/api/v1")


def user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "role": u.role, "active": u.active,
            "must_change_password": u.must_change_password}


def _client_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


class LoginBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=200)


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response, s: Session = Depends(db)):
    cfg = auth.settings()
    ip = _client_ip(request) or "?"
    if not auth.limiter.allow(f"login:{ip}", cfg["login_rate_per_minute"]):
        raise HTTPException(429, "Твърде много опити за вход; опитайте след минута.")
    try:
        u, token = auth.login(s, body.username, body.password, ip, request.headers.get("user-agent"))
    except auth.AuthError as e:
        s.commit()  # броячът на неуспешни опити и audit записът се пазят
        raise HTTPException(e.status, str(e))
    response.set_cookie(auth.SESSION_COOKIE, token, httponly=True, secure=cfg["cookie_secure"], samesite="strict",
                        max_age=cfg["max_hours"] * 3600, path="/")
    return user_out(u)


@router.post("/auth/logout")
def logout(request: Request, response: Response, s: Session = Depends(db), u: User = Depends(current_user)):
    cookie = request.cookies.get(auth.SESSION_COOKIE)
    if cookie:
        auth.logout(s, cookie)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(u: User = Depends(current_user)):
    return user_out(u)


class ChangePassword(BaseModel):
    current_password: str
    new_password: str


@router.post("/auth/change-password")
def change_password(body: ChangePassword, request: Request, s: Session = Depends(db),
                    u: User = Depends(current_user)):
    try:
        auth.change_password(s, u, body.current_password, body.new_password,
                             keep_token=request.cookies.get(auth.SESSION_COOKIE))
    except auth.AuthError as e:
        raise HTTPException(e.status, str(e))
    return user_out(u)


# ---------------------------------------------------------------- администриране

@router.get("/users")
def users(s: Session = Depends(db), _: User = Depends(require(*ADMINS))):
    return [user_out(u) for u in s.scalars(select(User).order_by(User.username))]


class NewUser(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    display_name: str = Field(min_length=1, max_length=200)
    role: str
    password: str


@router.post("/users")
def create_user(body: NewUser, s: Session = Depends(db), actor: User = Depends(require(*ADMINS))):
    if body.role not in ROLES:
        raise HTTPException(422, f"Роля: {', '.join(ROLES)}")
    if s.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "Потребителското име е заето")
    try:
        ph = auth.hash_password(body.password)
    except auth.AuthError as e:
        raise HTTPException(e.status, str(e))
    u = User(username=body.username, display_name=body.display_name, role=body.role, password_hash=ph,
             must_change_password=True)
    s.add(u)
    s.flush()
    audit(s, actor.id, "user.create", "user", u.id, new={"username": u.username, "role": u.role})
    return user_out(u)


class UserPatch(BaseModel):
    display_name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, s: Session = Depends(db), actor: User = Depends(require(*ADMINS))):
    u = s.get(User, user_id)
    if u is None:
        raise HTTPException(404, "Няма такъв потребител")
    if body.role is not None and body.role not in ROLES:
        raise HTTPException(422, f"Роля: {', '.join(ROLES)}")
    if u.id == actor.id and (body.active is False or (body.role and body.role != u.role)):
        raise HTTPException(422, "Не можете да деактивирате или смените ролята на собствения си акаунт.")
    old = user_out(u)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(u, k, v)
    if body.active is False or (body.role and body.role != old["role"]):
        auth.revoke_all(s, u.id)  # правата влизат в сила веднага
    audit(s, actor.id, "user.update", "user", u.id, old=old, new=user_out(u))
    return user_out(u)


class ResetPassword(BaseModel):
    password: str


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, body: ResetPassword, s: Session = Depends(db),
                   actor: User = Depends(require(*ADMINS))):
    u = s.get(User, user_id)
    if u is None:
        raise HTTPException(404, "Няма такъв потребител")
    try:
        u.password_hash = auth.hash_password(body.password)
    except auth.AuthError as e:
        raise HTTPException(e.status, str(e))
    u.must_change_password = True
    u.failed_logins = 0
    u.locked_until = None
    auth.revoke_all(s, u.id)
    audit(s, actor.id, "user.reset_password", "user", u.id)
    return user_out(u)


class NewToken(BaseModel):
    name: str = Field(min_length=1, max_length=100)


@router.post("/users/{user_id}/tokens")
def create_token(user_id: int, body: NewToken, s: Session = Depends(db), actor: User = Depends(require(*ADMINS))):
    """API токен за ERP/интеграция. Стойността се връща само веднъж."""
    u = s.get(User, user_id)
    if u is None or not u.active:
        raise HTTPException(404, "Няма такъв активен потребител")
    t, raw = auth.create_api_token(s, actor, u, body.name)
    return {"id": t.id, "name": t.name, "prefix": t.prefix, "token": raw}


@router.get("/users/{user_id}/tokens")
def list_tokens(user_id: int, s: Session = Depends(db), _: User = Depends(require(*ADMINS))):
    return [{"id": t.id, "name": t.name, "prefix": t.prefix, "created_at": t.created_at,
             "last_used_at": t.last_used_at, "revoked_at": t.revoked_at}
            for t in s.scalars(select(ApiToken).where(ApiToken.user_id == user_id).order_by(ApiToken.id))]


@router.delete("/tokens/{token_id}")
def revoke_token(token_id: int, s: Session = Depends(db), actor: User = Depends(require(*ADMINS))):
    t = s.get(ApiToken, token_id)
    if t is None:
        raise HTTPException(404, "Няма такъв токен")
    if t.revoked_at is None:
        t.revoked_at = auth._now()
        audit(s, actor.id, "auth.token_revoke", "api_token", t.id)
    return {"ok": True}
