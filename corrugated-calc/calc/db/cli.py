"""python -m calc.db.cli migrate | seed [path] | add-user <username> <role> [display name] | set-password <username>

Паролата се чете от CALC_PASSWORD или интерактивно; при add-user потребителят сменя паролата при първия вход.
"""

import getpass
import os
import sys

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from ..auth import AuthError, hash_password
from .models import ROLES, User
from .repository import ensure_user, seed_from_json
from .session import session_scope


def migrate():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    cfg = AlembicConfig(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, "head")


def _password() -> str:
    pw = os.environ.get("CALC_PASSWORD")
    if pw:
        return pw
    pw = getpass.getpass("Парола: ")
    if pw != getpass.getpass("Повторете: "):
        raise SystemExit("Паролите не съвпадат")
    return pw


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    try:
        if cmd == "migrate":
            migrate()
        elif cmd == "seed":
            with session_scope() as s:
                seed_from_json(s, argv[1] if len(argv) > 1 else None)
        elif cmd == "add-user":
            if len(argv) < 3 or argv[2] not in ROLES:
                raise SystemExit(f"add-user <username> <{'|'.join(ROLES)}> [име]")
            ph = hash_password(_password())
            with session_scope() as s:
                if s.scalar(select(User).where(User.username == argv[1])):
                    raise SystemExit("Потребителят съществува; използвайте set-password")
                u = ensure_user(s, argv[1], argv[2], " ".join(argv[3:]) or None, password_hash=ph,
                                must_change_password=True)
                print(f"user {u.username} ({u.role}) id={u.id}")
        elif cmd == "set-password":
            ph = hash_password(_password())
            with session_scope() as s:
                u = s.scalar(select(User).where(User.username == argv[1]))
                if u is None:
                    raise SystemExit("Няма такъв потребител")
                u.password_hash, u.failed_logins, u.locked_until = ph, 0, None
                print(f"паролата на {u.username} е сменена")
        else:
            print(__doc__)
            return 1
    except AuthError as e:
        raise SystemExit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
