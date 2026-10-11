from __future__ import annotations

import json
from typing import Any

import asyncpg


async def _init(con: asyncpg.Connection) -> None:
    for t in ("json", "jsonb"):
        await con.set_type_codec(t, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=8, init=_init)


def record(r: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def records(rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
