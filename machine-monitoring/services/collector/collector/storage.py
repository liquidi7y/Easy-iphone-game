"""PostgreSQL/TimescaleDB persistence.

All writes go through a bounded in-memory queue drained by a single writer
task, so a slow or unavailable database never blocks polling. If the
database is down the queue buffers (up to MAX_QUEUE items, oldest dropped
first and counted) and the writer retries with backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg

from . import events as ev
from .config import MachineConfig
from .metrics import DB_DROPPED, DB_WRITE_LATENCY, QUEUE_DEPTH

log = logging.getLogger("collector.storage")
MAX_QUEUE = 200_000
BATCH = 5_000


def ts(t: float) -> datetime:
    return datetime.fromtimestamp(t, tz=timezone.utc)


def as_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    return float(v)


@dataclass
class RawSample:
    machine_id: int
    time: float
    signal: str
    address: str
    value: Any
    quality: str


@dataclass
class NormSample:
    machine_id: int
    time: float
    name: str
    value: Any
    quality: str


@dataclass
class CurrentState:
    machine_id: int
    state: str
    state_since: float
    quality: str
    reason: str
    plc_connected: bool
    last_successful_read: float | None
    normalized: dict[str, Any]
    signals: dict[str, Any]
    collector_id: str
    updated_at: float


@dataclass
class Health:
    machine_id: int
    time: float
    collector_id: str
    plc_connected: bool
    poll_duration_ms: float | None
    poll_errors_total: int
    last_successful_read: float | None
    details: dict[str, Any]


@dataclass
class OpItem:
    machine_id: int
    op: ev.Op


Item = RawSample | NormSample | CurrentState | Health | OpItem


class Storage:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None
        self.queue: deque[Item] = deque()
        self._wake = asyncio.Event()

    async def connect(self, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            try:
                self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4)
                log.info("connected to database")
                return
            except (OSError, asyncpg.PostgresError) as e:
                log.warning("database not available (%s); retrying in %.0fs", e, delay)
                try:
                    await asyncio.wait_for(stop.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 30)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    # -- registry -------------------------------------------------------------
    async def sync_machine(self, cfg: MachineConfig) -> int:
        """Upsert the machine registry from YAML. Returns machines.id."""
        assert self.pool is not None
        async with self.pool.acquire() as con, con.transaction():
            site_id = await con.fetchval(
                "INSERT INTO sites (code, name) VALUES ($1, $1) "
                "ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code RETURNING id", cfg.site)
            mid = await con.fetchval(
                """INSERT INTO machines (site_id, code, name, vendor, model, enabled, simulated, config_hash, config, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb, now())
                   ON CONFLICT (code) DO UPDATE SET site_id=EXCLUDED.site_id, name=EXCLUDED.name,
                     vendor=EXCLUDED.vendor, model=EXCLUDED.model, enabled=EXCLUDED.enabled,
                     simulated=EXCLUDED.simulated, config_hash=EXCLUDED.config_hash,
                     config=EXCLUDED.config, updated_at=now()
                   RETURNING id""",
                site_id, cfg.code, cfg.name, cfg.vendor, cfg.model, cfg.enabled, cfg.simulated,
                cfg.config_hash, json.dumps(cfg.document))
            ctl = cfg.document.get("controller") or {}
            await con.execute(
                """INSERT INTO controllers (machine_id, vendor, model, driver, host, port, read_only, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,true,now())
                   ON CONFLICT (machine_id) DO UPDATE SET vendor=EXCLUDED.vendor, model=EXCLUDED.model,
                     driver=EXCLUDED.driver, host=EXCLUDED.host, port=EXCLUDED.port, updated_at=now()""",
                mid, ctl.get("vendor"), ctl.get("model"), cfg.driver, cfg.host, cfg.port)

            # Devices: upsert by address; a renamed signal keeps its address row.
            await con.execute("UPDATE machine_devices SET active=false, name = name || '#' || id "
                              "WHERE machine_id=$1", mid)
            for s in cfg.signals:
                r = s.raw
                await con.execute(
                    """INSERT INTO machine_devices (machine_id, address, name, protocol, data_type, poll_group,
                         read_only, status, confidence, normal_state, unit, description, evidence, active, updated_at)
                       VALUES ($1,$2,$3,$4,$5,$6,true,$7,$8,$9,$10,$11,$12,true,now())
                       ON CONFLICT (machine_id, address) DO UPDATE SET name=EXCLUDED.name,
                         protocol=EXCLUDED.protocol, data_type=EXCLUDED.data_type, poll_group=EXCLUDED.poll_group,
                         status=EXCLUDED.status, confidence=EXCLUDED.confidence, normal_state=EXCLUDED.normal_state,
                         unit=EXCLUDED.unit, description=EXCLUDED.description, evidence=EXCLUDED.evidence,
                         active=true, updated_at=now()""",
                    mid, s.address, s.name, cfg.driver, s.type, s.poll, s.status, r.get("confidence"),
                    None if r.get("normal_state") is None else str(r.get("normal_state")),
                    s.unit, r.get("description") or r.get("physical_function"), r.get("evidence"))
            await con.execute("DELETE FROM device_mappings WHERE machine_id=$1", mid)
            for name, mp in cfg.mappings.items():
                sig = cfg.signal(mp.signal)
                transform = {k: v for k, v in (("invert", mp.invert), ("scale", mp.scale)) if v}
                await con.execute(
                    """INSERT INTO device_mappings (machine_id, normalized_name, source_signal, source_address,
                         transform, unit, normal_state) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7)""",
                    mid, name, sig.name, sig.address, json.dumps(transform), mp.unit or sig.unit,
                    None if sig.raw.get("normal_state") is None else str(sig.raw.get("normal_state")))
        return mid

    async def last_counters(self, machine_id: int) -> dict[str, int]:
        assert self.pool is not None
        rows = await self.pool.fetch(
            """SELECT DISTINCT ON (counter) counter, value FROM production_counters
               WHERE machine_id=$1 ORDER BY counter, time DESC""", machine_id)
        return {r["counter"]: r["value"] for r in rows}

    # -- queue ------------------------------------------------------------------
    def enqueue(self, item: Item) -> None:
        if len(self.queue) >= MAX_QUEUE:
            self.queue.popleft()
            DB_DROPPED.inc()
        self.queue.append(item)
        QUEUE_DEPTH.set(len(self.queue))

    async def writer(self, stop: asyncio.Event, interval: float = 1.0) -> None:
        delay = interval
        while True:
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
            if not self.queue:
                delay = interval
                if stop.is_set():
                    return
                continue
            batch = [self.queue.popleft() for _ in range(min(BATCH, len(self.queue)))]
            t0 = time.perf_counter()
            try:
                await self._write(batch)
                DB_WRITE_LATENCY.observe((time.perf_counter() - t0) * 1000)
                delay = interval if not self.queue else 0
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                log.warning("database write failed (%s); %d items buffered", e, len(batch) + len(self.queue))
                self.queue.extendleft(reversed(batch))
                while len(self.queue) > MAX_QUEUE:
                    self.queue.popleft()
                    DB_DROPPED.inc()
                delay = min(max(delay * 2, 2), 30)
                if stop.is_set():
                    log.error("shutting down with %d unwritten items", len(self.queue))
                    return
            QUEUE_DEPTH.set(len(self.queue))

    async def _write(self, batch: list[Item]) -> None:
        assert self.pool is not None
        raw = [(ts(i.time), i.machine_id, i.signal, i.address, as_float(i.value), i.quality)
               for i in batch if isinstance(i, RawSample)]
        norm = [(ts(i.time), i.machine_id, i.name, as_float(i.value), i.quality)
                for i in batch if isinstance(i, NormSample)]
        health = [(ts(i.time), i.machine_id, i.collector_id, i.plc_connected, i.poll_duration_ms,
                   i.poll_errors_total, ts(i.last_successful_read) if i.last_successful_read else None,
                   json.dumps(i.details)) for i in batch if isinstance(i, Health)]
        current: dict[int, CurrentState] = {}
        for i in batch:
            if isinstance(i, CurrentState):
                current[i.machine_id] = i  # coalesce: only the latest matters
        ops = [i for i in batch if isinstance(i, OpItem)]

        async with self.pool.acquire() as con, con.transaction():
            if raw:
                await con.executemany(
                    "INSERT INTO raw_samples (time, machine_id, signal, address, value, quality) "
                    "VALUES ($1,$2,$3,$4,$5,$6)", raw)
            if norm:
                await con.executemany(
                    "INSERT INTO normalized_samples (time, machine_id, name, value, quality) "
                    "VALUES ($1,$2,$3,$4,$5)", norm)
            if health:
                await con.executemany(
                    """INSERT INTO collector_health (time, machine_id, collector_id, plc_connected,
                         poll_duration_ms, poll_errors_total, last_successful_read, details)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)""", health)
            for item in ops:  # ordered: downtime/fault ensure/close depend on sequence
                await self._write_op(con, item.machine_id, item.op)
            for c in current.values():
                await con.execute(
                    """INSERT INTO machine_current_state (machine_id, state, state_since, quality, reason,
                         plc_connected, last_successful_read, normalized, signals, collector_id, updated_at)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11)
                       ON CONFLICT (machine_id) DO UPDATE SET state=EXCLUDED.state,
                         state_since=EXCLUDED.state_since, quality=EXCLUDED.quality, reason=EXCLUDED.reason,
                         plc_connected=EXCLUDED.plc_connected, last_successful_read=EXCLUDED.last_successful_read,
                         normalized=EXCLUDED.normalized, signals=EXCLUDED.signals,
                         collector_id=EXCLUDED.collector_id, updated_at=EXCLUDED.updated_at""",
                    c.machine_id, c.state, ts(c.state_since), c.quality, c.reason, c.plc_connected,
                    ts(c.last_successful_read) if c.last_successful_read else None,
                    json.dumps(c.normalized, default=str), json.dumps(c.signals, default=str),
                    c.collector_id, ts(c.updated_at))

    async def _write_op(self, con: asyncpg.Connection, mid: int, op: ev.Op) -> None:
        if isinstance(op, ev.StateChange):
            await con.execute(
                """INSERT INTO machine_state_events (machine_id, time, previous_state, new_state, reason,
                     quality, source_snapshot) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)""",
                mid, ts(op.time), op.previous, op.new, op.reason, op.quality,
                json.dumps(op.snapshot, default=str))
        elif isinstance(op, ev.MachineEvent):
            await con.execute(
                "INSERT INTO machine_events (machine_id, time, event_type, data) VALUES ($1,$2,$3,$4::jsonb)",
                mid, ts(op.time), op.type, json.dumps(op.data, default=str))
        elif isinstance(op, ev.CounterSample):
            await con.execute(
                "INSERT INTO production_counters (time, machine_id, counter, value, delta) VALUES ($1,$2,$3,$4,$5)",
                ts(op.time), mid, op.counter, op.value, op.delta)
        elif isinstance(op, ev.DowntimeEnsure):
            await con.execute(
                "UPDATE downtime_events SET ended_at=$2 WHERE machine_id=$1 AND ended_at IS NULL AND state <> $3",
                mid, ts(op.time), op.state)
            await con.execute(
                """INSERT INTO downtime_events (machine_id, started_at, state, reason, order_session_id)
                   SELECT $1, $2, $3, $4,
                          (SELECT id FROM machine_order_sessions WHERE machine_id=$1 AND ended_at IS NULL)
                   WHERE NOT EXISTS (SELECT 1 FROM downtime_events WHERE machine_id=$1 AND ended_at IS NULL)""",
                mid, ts(op.time), op.state, op.reason)
        elif isinstance(op, ev.DowntimeClose):
            await con.execute(
                "UPDATE downtime_events SET ended_at=$2 WHERE machine_id=$1 AND ended_at IS NULL",
                mid, ts(op.time))
        elif isinstance(op, ev.FaultEnsure):
            if op.code is not None:  # a code arriving after the fault bit completes the open fault
                await con.execute("UPDATE fault_events SET fault_code=$2 WHERE machine_id=$1 AND ended_at IS NULL "
                                  "AND fault_code IS NULL", mid, op.code)
            await con.execute(
                "UPDATE fault_events SET ended_at=$2 WHERE machine_id=$1 AND ended_at IS NULL "
                "AND $3::integer IS NOT NULL AND fault_code IS DISTINCT FROM $3", mid, ts(op.time), op.code)
            await con.execute(
                """INSERT INTO fault_events (machine_id, started_at, fault_code)
                   SELECT $1, $2, $3
                   WHERE NOT EXISTS (SELECT 1 FROM fault_events WHERE machine_id=$1 AND ended_at IS NULL)""",
                mid, ts(op.time), op.code)
        elif isinstance(op, ev.FaultClose):
            await con.execute(
                "UPDATE fault_events SET ended_at=$2 WHERE machine_id=$1 AND ended_at IS NULL",
                mid, ts(op.time))
