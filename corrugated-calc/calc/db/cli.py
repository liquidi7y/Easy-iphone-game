"""python -m calc.db.cli migrate | seed [path] | add-user <username> <role> [display name]"""

import sys

from alembic import command
from alembic.config import Config as AlembicConfig

from .repository import ensure_user, seed_from_json
from .session import session_scope


def migrate():
    from pathlib import Path
    cfg = AlembicConfig(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "migrations"))
    command.upgrade(cfg, "head")


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "migrate":
        migrate()
    elif cmd == "seed":
        with session_scope() as s:
            seed_from_json(s, argv[1] if len(argv) > 1 else None)
    elif cmd == "add-user":
        with session_scope() as s:
            u = ensure_user(s, argv[1], argv[2], " ".join(argv[3:]) or None)
            print(f"user {u.username} ({u.role}) id={u.id}")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
