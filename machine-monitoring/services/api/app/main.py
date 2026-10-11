"""Nueva machine monitoring - internal REST API (v1).

Exposes the normalized machine model, events, production and downtime
from the monitoring database. The order-session endpoints write ONLY to the
monitoring/ERP tables; nothing in this service can reach a PLC.

OpenAPI: /api/v1/openapi.json, interactive docs: /api/v1/docs
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .db import create_pool, record, records

STALE_AFTER_S = float(os.environ.get("API_STALE_AFTER_S", "15"))
VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool(os.environ["DATABASE_URL"])
    yield
    await app.state.pool.close()


app = FastAPI(
    title="Nueva Machine Monitoring API",
    version=VERSION,
    description="Read-only view of machine state plus ERP order sessions. Never writes to a PLC.",
    openapi_url="/api/v1/openapi.json",
    docs_url="/api/v1/docs",
    redoc_url=None,
    lifespan=lifespan,
)


def pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def window(from_: datetime | None, to: datetime | None, default_hours: int = 24) -> tuple[datetime, datetime]:
    to = to or utcnow()
    from_ = from_ or (to - timedelta(hours=default_hours))
    if from_.tzinfo is None:
        from_ = from_.replace(tzinfo=timezone.utc)
    if to.tzinfo is None:
        to = to.replace(tzinfo=timezone.utc)
    if from_ >= to:
        raise HTTPException(400, "'from' must be before 'to'")
    return from_, to


async def machine_row(db: asyncpg.Pool, machine: str) -> asyncpg.Record:
    """Resolve a machine by numeric id or code (case-insensitive)."""
    if machine.isdigit():
        row = await db.fetchrow("SELECT * FROM machines WHERE id = $1", int(machine))
    else:
        row = await db.fetchrow("SELECT * FROM machines WHERE code = upper($1)", machine)
    if row is None:
        raise HTTPException(404, f"machine {machine!r} not found")
    return row


def present_state(m: asyncpg.Record, cs: asyncpg.Record | None) -> dict[str, Any]:
    """Current state with the monitoring path's health applied.

    If the collector has stopped reporting, the machine state is UNKNOWN:
    a monitoring failure is never presented as a machine stop.
    """
    base = {"machine_id": m["id"], "code": m["code"], "simulated": m["simulated"], "enabled": m["enabled"]}
    if cs is None:
        return {**base, "state": "UNKNOWN", "quality": "bad", "since": None,
                "reason": "no data received from collector yet",
                "monitoring": {"collector_reporting": False, "plc_connected": False,
                               "last_update": None, "last_successful_read": None},
                "normalized": {}}
    age = (utcnow() - cs["updated_at"]).total_seconds()
    reporting = age <= STALE_AFTER_S
    state, quality, reason = cs["state"], cs["quality"], cs["reason"]
    if not reporting:
        state, quality = "UNKNOWN", "stale"
        reason = f"collector has not reported for {int(age)}s (monitoring failure, not a machine state)"
    return {
        **base,
        "state": state,
        "quality": quality,
        "since": cs["state_since"] if reporting else cs["updated_at"],
        "reason": reason,
        "monitoring": {
            "collector_reporting": reporting,
            "plc_connected": bool(cs["plc_connected"]) and reporting,
            "last_update": cs["updated_at"],
            "last_successful_read": cs["last_successful_read"],
        },
        "normalized": cs["normalized"] if reporting else {},
    }


# ---------------------------------------------------------------------------
@app.get("/api/v1/health", tags=["platform"])
async def health(db: asyncpg.Pool = Depends(pool)) -> dict[str, Any]:
    try:
        rows = await db.fetch(
            """SELECT m.code, m.enabled, cs.updated_at, cs.plc_connected
               FROM machines m LEFT JOIN machine_current_state cs ON cs.machine_id = m.id ORDER BY m.code""")
    except (OSError, asyncpg.PostgresError) as e:
        raise HTTPException(503, f"database unavailable: {e}") from e
    now = utcnow()
    machines = []
    for r in rows:
        age = (now - r["updated_at"]).total_seconds() if r["updated_at"] else None
        machines.append({
            "code": r["code"], "enabled": r["enabled"],
            "collector_reporting": age is not None and age <= STALE_AFTER_S,
            "plc_connected": bool(r["plc_connected"]) if age is not None and age <= STALE_AFTER_S else False,
            "last_update_age_s": round(age, 1) if age is not None else None,
        })
    return {"status": "ok", "version": VERSION, "database": "ok", "time": now, "machines": machines}


@app.get("/api/v1/machines", tags=["machines"])
async def list_machines(db: asyncpg.Pool = Depends(pool)) -> list[dict[str, Any]]:
    ms = await db.fetch("SELECT m.*, s.code AS site FROM machines m JOIN sites s ON s.id = m.site_id ORDER BY m.code")
    cs = {r["machine_id"]: r for r in await db.fetch("SELECT * FROM machine_current_state")}
    out = []
    for m in ms:
        st = present_state(m, cs.get(m["id"]))
        out.append({"id": m["id"], "code": m["code"], "name": m["name"], "site": m["site"],
                    "vendor": m["vendor"], "model": m["model"], "enabled": m["enabled"],
                    "simulated": m["simulated"], "state": st["state"], "quality": st["quality"],
                    "since": st["since"], "monitoring": st["monitoring"]})
    return out


@app.get("/api/v1/machines/{machine}", tags=["machines"])
async def get_machine(machine: str, db: asyncpg.Pool = Depends(pool)) -> dict[str, Any]:
    m = await machine_row(db, machine)
    site = await db.fetchval("SELECT code FROM sites WHERE id = $1", m["site_id"])
    ctl = await db.fetchrow("SELECT vendor, model, driver, host, port, read_only FROM controllers WHERE machine_id=$1",
                            m["id"])
    maps = await db.fetch("""SELECT normalized_name, source_signal, source_address, transform, unit
                             FROM device_mappings WHERE machine_id=$1 ORDER BY normalized_name""", m["id"])
    counts = await db.fetchrow("""SELECT count(*) FILTER (WHERE status='verified') AS verified,
                                         count(*) FILTER (WHERE status='unverified') AS unverified
                                  FROM machine_devices WHERE machine_id=$1 AND active""", m["id"])
    return {
        "id": m["id"], "code": m["code"], "name": m["name"], "site": site, "vendor": m["vendor"],
        "model": m["model"], "enabled": m["enabled"], "simulated": m["simulated"],
        "config_hash": m["config_hash"], "updated_at": m["updated_at"],
        "controller": record(ctl), "mappings": records(maps),
        "signals": {"verified": counts["verified"], "unverified": counts["unverified"]},
    }


@app.get("/api/v1/machines/{machine}/state", tags=["machines"])
async def get_state(machine: str, db: asyncpg.Pool = Depends(pool)) -> dict[str, Any]:
    m = await machine_row(db, machine)
    cs = await db.fetchrow("SELECT * FROM machine_current_state WHERE machine_id=$1", m["id"])
    return present_state(m, cs)


@app.get("/api/v1/machines/{machine}/signals", tags=["machines"])
async def get_signals(machine: str, db: asyncpg.Pool = Depends(pool)) -> dict[str, Any]:
    m = await machine_row(db, machine)
    cs = await db.fetchrow("SELECT signals, updated_at FROM machine_current_state WHERE machine_id=$1", m["id"])
    devices = await db.fetch("""SELECT name, address, data_type, poll_group, status, confidence, unit, description
                                FROM machine_devices WHERE machine_id=$1 AND active ORDER BY address""", m["id"])
    live = cs["signals"] if cs else {}
    stale = cs is None or (utcnow() - cs["updated_at"]).total_seconds() > STALE_AFTER_S
    out = []
    for d in devices:
        cur = live.get(d["name"], {})
        out.append({**record(d), "value": None if stale else cur.get("value"),
                    "quality": "stale" if stale else cur.get("quality", "bad")})
    return {"machine": m["code"], "updated_at": cs["updated_at"] if cs else None, "signals": out}


@app.get("/api/v1/machines/{machine}/events", tags=["machines"])
async def get_events(
    machine: str,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    limit: int = Query(200, ge=1, le=5000),
    db: asyncpg.Pool = Depends(pool),
) -> dict[str, Any]:
    m = await machine_row(db, machine)
    f, t = window(from_, to)
    rows = await db.fetch(
        """SELECT * FROM (
             SELECT time, event_type AS type, data FROM machine_events
              WHERE machine_id=$1 AND time >= $2 AND time < $3
             UNION ALL
             SELECT time, 'STATE_CHANGE', jsonb_build_object('previous', previous_state, 'new', new_state,
                    'reason', reason, 'quality', quality)
               FROM machine_state_events WHERE machine_id=$1 AND time >= $2 AND time < $3
           ) e ORDER BY time DESC LIMIT $4""", m["id"], f, t, limit)
    return {"machine": m["code"], "from": f, "to": t, "events": records(rows)}


@app.get("/api/v1/machines/{machine}/production", tags=["production"])
async def get_production(
    machine: str,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    db: asyncpg.Pool = Depends(pool),
) -> dict[str, Any]:
    m = await machine_row(db, machine)
    f, t = window(from_, to)
    counters = await db.fetch(
        """SELECT counter, coalesce(sum(delta), 0)::bigint AS produced, min(time) AS first, max(time) AS last
           FROM production_counters WHERE machine_id=$1 AND time >= $2 AND time < $3 GROUP BY counter""",
        m["id"], f, t)
    current = await db.fetch(
        """SELECT DISTINCT ON (counter) counter, value, time FROM production_counters
           WHERE machine_id=$1 ORDER BY counter, time DESC""", m["id"])
    runtime = await state_durations(db, m["id"], f, t)
    session = await active_session(db, m["id"])
    produced_total = next((c["produced"] for c in counters if c["counter"] == "counter.total"), 0)
    running_s = runtime.get("RUNNING", 0.0)
    return {
        "machine": m["code"], "from": f, "to": t,
        "window": {c["counter"]: {"produced": c["produced"], "first": c["first"], "last": c["last"]} for c in counters},
        "current": {c["counter"]: {"value": c["value"], "time": c["time"]} for c in current},
        "state_seconds": runtime,
        "average_rate_per_min": round(produced_total / (running_s / 60), 2) if running_s > 0 else None,
        "active_order_session": session,
    }


@app.get("/api/v1/machines/{machine}/downtime", tags=["production"])
async def get_downtime(
    machine: str,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    db: asyncpg.Pool = Depends(pool),
) -> dict[str, Any]:
    m = await machine_row(db, machine)
    f, t = window(from_, to)
    rows = await db.fetch(
        """SELECT id, started_at, ended_at, state, reason, operator_reason, order_session_id,
                  extract(epoch FROM (least(coalesce(ended_at, now()), $3) - greatest(started_at, $2)))::float
                    AS seconds_in_window
           FROM downtime_events
           WHERE machine_id=$1 AND started_at < $3 AND (ended_at IS NULL OR ended_at > $2)
           ORDER BY started_at DESC""", m["id"], f, t)
    total = sum(r["seconds_in_window"] for r in rows)
    return {"machine": m["code"], "from": f, "to": t, "total_seconds": round(total, 1), "periods": records(rows)}


# ---------------------------------------------------------------------------
# ERP order sessions (monitoring database only)
# ---------------------------------------------------------------------------
class OrderSessionOpen(BaseModel):
    order_number: str = Field(..., min_length=1, max_length=64)
    product: str | None = None
    target_quantity: int | None = Field(None, ge=0)
    operator: str | None = None
    shift: str | None = None


class OrderSessionClose(BaseModel):
    notes: str | None = None


async def active_session(db: asyncpg.Pool | asyncpg.Connection, machine_id: int) -> dict[str, Any] | None:
    s = await db.fetchrow(
        """SELECT s.id, o.order_number, o.product, o.target_quantity, s.operator, s.shift, s.started_at,
                  s.start_counter
           FROM machine_order_sessions s JOIN orders o ON o.id = s.order_id
           WHERE s.machine_id=$1 AND s.ended_at IS NULL""", machine_id)
    if s is None:
        return None
    cur = await current_counter(db, machine_id)
    produced = (cur - s["start_counter"]) if cur is not None and s["start_counter"] is not None else None
    return {**record(s), "current_counter": cur, "produced": produced}


async def current_counter(db: asyncpg.Pool | asyncpg.Connection, machine_id: int) -> int | None:
    v = await db.fetchval(
        """SELECT value FROM production_counters WHERE machine_id=$1 AND counter='counter.total'
           ORDER BY time DESC LIMIT 1""", machine_id)
    return int(v) if v is not None else None


async def state_durations(db: asyncpg.Pool, machine_id: int, f: datetime, t: datetime) -> dict[str, float]:
    rows = await db.fetch(
        """WITH changes AS (
             (SELECT time, new_state FROM machine_state_events
               WHERE machine_id=$1 AND time < $2 ORDER BY time DESC LIMIT 1)
             UNION ALL
             SELECT time, new_state FROM machine_state_events WHERE machine_id=$1 AND time >= $2 AND time < $3
           ), spans AS (
             SELECT new_state, greatest(time, $2) AS s,
                    least(coalesce(lead(time) OVER (ORDER BY time), now()), $3) AS e
             FROM changes
           )
           SELECT new_state, sum(extract(epoch FROM e - s))::float AS seconds FROM spans
           WHERE e > s GROUP BY new_state""", machine_id, f, t)
    return {r["new_state"]: round(r["seconds"], 1) for r in rows}


@app.post("/api/v1/machines/{machine}/order-session", status_code=201, tags=["erp"])
async def open_order_session(machine: str, body: OrderSessionOpen, db: asyncpg.Pool = Depends(pool)) -> dict[str, Any]:
    """Start a production session for an ERP order. Monitoring database only - does not touch the PLC."""
    m = await machine_row(db, machine)
    async with db.acquire() as con, con.transaction():
        order_id = await con.fetchval(
            """INSERT INTO orders (order_number, product, target_quantity) VALUES ($1,$2,$3)
               ON CONFLICT (order_number) DO UPDATE SET
                 product = coalesce(EXCLUDED.product, orders.product),
                 target_quantity = coalesce(EXCLUDED.target_quantity, orders.target_quantity)
               RETURNING id""", body.order_number, body.product, body.target_quantity)
        start = await current_counter(con, m["id"])
        try:
            sid = await con.fetchval(
                """INSERT INTO machine_order_sessions (machine_id, order_id, operator, shift, start_counter)
                   VALUES ($1,$2,$3,$4,$5) RETURNING id""", m["id"], order_id, body.operator, body.shift, start)
        except asyncpg.UniqueViolationError:
            raise HTTPException(409, "machine already has an open order session; close it first") from None
        await con.execute("UPDATE downtime_events SET order_session_id=$2 WHERE machine_id=$1 AND ended_at IS NULL",
                          m["id"], sid)
    return {"session_id": sid, **(await active_session(db, m["id"]) or {})}


@app.post("/api/v1/machines/{machine}/order-session/close", tags=["erp"])
async def close_order_session(machine: str, body: OrderSessionClose | None = None,
                              db: asyncpg.Pool = Depends(pool)) -> dict[str, Any]:
    """Close the open production session and return its result. Monitoring database only."""
    m = await machine_row(db, machine)
    async with db.acquire() as con, con.transaction():
        s = await con.fetchrow(
            "SELECT * FROM machine_order_sessions WHERE machine_id=$1 AND ended_at IS NULL FOR UPDATE", m["id"])
        if s is None:
            raise HTTPException(404, "no open order session")
        end = await current_counter(con, m["id"])
        produced = (end - s["start_counter"]) if end is not None and s["start_counter"] is not None else None
        row = await con.fetchrow(
            """UPDATE machine_order_sessions SET ended_at=now(), end_counter=$2, produced=$3,
                 notes=coalesce($4, notes) WHERE id=$1 RETURNING *""",
            s["id"], end, produced, body.notes if body else None)
        downtime = await con.fetchval(
            """SELECT coalesce(sum(extract(epoch FROM coalesce(ended_at, now()) - greatest(started_at, $2))), 0)::float
               FROM downtime_events WHERE machine_id=$1 AND coalesce(ended_at, now()) > $2""",
            m["id"], s["started_at"])
    duration = (row["ended_at"] - row["started_at"]).total_seconds()
    return {**record(row), "duration_seconds": round(duration, 1), "downtime_seconds": round(downtime, 1),
            "runtime_seconds": round(max(0.0, duration - downtime), 1)}

