"""Автентикация: индивидуални акаунти, пароли (scrypt), сесии с timeout, API токени за ERP, rate limiting.

Пазят се само hash-ове на пароли, сесии и токени. Няма споделени акаунти.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .db.audit import audit
from .db.models import ApiToken, User, UserSession

SESSION_COOKIE = "calc_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "calc"
MIN_PASSWORD_LENGTH = 10
TOKEN_PREFIX = "cc_"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def settings() -> dict:
    return {
        "idle_minutes": _env_int("SESSION_IDLE_MINUTES", 30),
        "max_hours": _env_int("SESSION_MAX_HOURS", 12),
        "cookie_secure": os.environ.get("COOKIE_SECURE", "1") != "0",
        "max_fails": _env_int("LOGIN_MAX_FAILS", 5),
        "lock_minutes": _env_int("LOGIN_LOCK_MINUTES", 15),
        "rate_per_minute": _env_int("RATE_LIMIT_PER_MINUTE", 300),
        "login_rate_per_minute": _env_int("LOGIN_RATE_LIMIT_PER_MINUTE", 10),
    }


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- пароли

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    validate_password(password)
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(SCRYPT_N, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(),
                                          base64.b64encode(dk).decode())


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored:
        # еднакво време при липсващ потребител/парола
        hashlib.scrypt(password.encode(), salt=b"0" * 16, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
        return False
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(base64.b64decode(dk)))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def validate_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Паролата трябва да е поне {MIN_PASSWORD_LENGTH} символа.", 422)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- вход и сесии

GENERIC_LOGIN_ERROR = "Грешно потребителско име или парола, или акаунтът е временно заключен."


def login(s: Session, username: str, password: str, ip: Optional[str], user_agent: Optional[str]) -> tuple[User, str]:
    cfg = settings()
    u = s.scalar(select(User).where(User.username == username))
    now = _now()
    if u is None or not u.active:
        verify_password(password, None)
        audit(s, None, "auth.login_failed", "user", username, details={"ip": ip, "reason": "unknown_or_inactive"})
        raise AuthError(GENERIC_LOGIN_ERROR)
    if u.locked_until and u.locked_until > now:
        audit(s, u.id, "auth.login_failed", "user", u.id, details={"ip": ip, "reason": "locked"})
        raise AuthError(GENERIC_LOGIN_ERROR)
    if not verify_password(password, u.password_hash):
        u.failed_logins = (u.failed_logins or 0) + 1
        details = {"ip": ip, "reason": "bad_password", "failed_logins": u.failed_logins}
        if u.failed_logins >= cfg["max_fails"]:
            u.locked_until = now + timedelta(minutes=cfg["lock_minutes"])
            u.failed_logins = 0
            details["locked_until"] = u.locked_until.isoformat()
        audit(s, u.id, "auth.login_failed", "user", u.id, details=details)
        raise AuthError(GENERIC_LOGIN_ERROR)
    u.failed_logins = 0
    u.locked_until = None
    token = secrets.token_urlsafe(32)
    s.add(UserSession(id=_sha(token), user_id=u.id, last_seen_at=now,
                      expires_at=now + timedelta(hours=cfg["max_hours"]), ip=(ip or "")[:64],
                      user_agent=(user_agent or "")[:300]))
    audit(s, u.id, "auth.login", "user", u.id, details={"ip": ip})
    return u, token


def resolve_session(s: Session, token: str) -> Optional[User]:
    cfg = settings()
    sess = s.get(UserSession, _sha(token))
    now = _now()
    if sess is None or sess.revoked or sess.expires_at <= now:
        return None
    if sess.last_seen_at + timedelta(minutes=cfg["idle_minutes"]) <= now:
        sess.revoked = True
        return None
    u = s.get(User, sess.user_id)
    if u is None or not u.active:
        return None
    sess.last_seen_at = now
    return u


def logout(s: Session, token: str) -> None:
    sess = s.get(UserSession, _sha(token))
    if sess is not None and not sess.revoked:
        sess.revoked = True
        audit(s, sess.user_id, "auth.logout", "user", sess.user_id)


def revoke_all(s: Session, user_id: int) -> None:
    s.execute(update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked.is_(False))
              .values(revoked=True))
    s.execute(update(ApiToken).where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
              .values(revoked_at=_now()))


def change_password(s: Session, u: User, current: str, new: str, keep_token: Optional[str] = None) -> None:
    if not verify_password(current, u.password_hash):
        raise AuthError("Текущата парола е грешна.", 403)
    if current == new:
        raise AuthError("Новата парола трябва да е различна.", 422)
    u.password_hash = hash_password(new)
    u.must_change_password = False
    # прекратява другите сесии на потребителя
    q = update(UserSession).where(UserSession.user_id == u.id, UserSession.revoked.is_(False))
    if keep_token:
        q = q.where(UserSession.id != _sha(keep_token))
    s.execute(q.values(revoked=True))
    audit(s, u.id, "auth.change_password", "user", u.id)


# ---------------------------------------------------------------- API токени (ERP)

def create_api_token(s: Session, actor: User, user: User, name: str) -> tuple[ApiToken, str]:
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    t = ApiToken(user_id=user.id, name=name[:100], token_hash=_sha(raw), prefix=raw[:10], created_by=actor.id)
    s.add(t)
    s.flush()
    audit(s, actor.id, "auth.token_create", "api_token", t.id, new={"user_id": user.id, "name": name,
                                                                     "prefix": t.prefix})
    return t, raw


def resolve_api_token(s: Session, raw: str) -> Optional[User]:
    if not raw.startswith(TOKEN_PREFIX):
        return None
    t = s.scalar(select(ApiToken).where(ApiToken.token_hash == _sha(raw)))
    if t is None or t.revoked_at is not None:
        return None
    u = s.get(User, t.user_id)
    if u is None or not u.active:
        return None
    t.last_used_at = _now()
    return u


# ---------------------------------------------------------------- rate limiting

class RateLimiter:
    """Плъзгащ прозорец в паметта на процеса. При няколко инстанции — заменя се с Redis (по нужда)."""

    def __init__(self):
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 50_000:  # защита от неограничен растеж
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - window}
            return True

    def reset(self):
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()
