"""Audit log: кой, кога, какво, стара/нова стойност."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.orm import Session

from .models import AuditLog


def _jsonable(v: Any) -> Any:
    if v is None:
        return None
    return json.loads(json.dumps(v, default=lambda o: str(o) if isinstance(o, Decimal) else o.isoformat()
                                 if hasattr(o, "isoformat") else str(o)))


def audit(s: Session, actor_id: Optional[int], action: str, entity: str, entity_id: Any,
          old: Any = None, new: Any = None, details: Any = None) -> None:
    s.add(AuditLog(actor_id=actor_id, action=action, entity=entity, entity_id=str(entity_id),
                   old=_jsonable(old), new=_jsonable(new), details=_jsonable(details)))
