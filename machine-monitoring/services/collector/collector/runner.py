"""Per-machine polling, evaluation and publishing."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol

from . import events as ev
from .blocks import Block, decode_block, plan_blocks
from .config import MachineConfig
from .drivers import Driver, DriverError, DriverNotImplemented
from .metrics import LAST_READ, MACHINE_STATE, PLC_UP, POLL_DURATION, POLL_ERRORS
from .model import BAD, GOOD, STATES, UNKNOWN, Evaluation, SignalTable, evaluate
from .storage import CurrentState, Health, NormSample, OpItem, RawSample

log = logging.getLogger("collector.runner")

HEARTBEAT_S = 60.0          # re-write unchanged raw/normalized values this often
CURRENT_STATE_S = 1.0       # machine_current_state refresh
STATE_PUBLISH_S = 5.0       # retained MQTT state refresh
HEALTH_S = 10.0
MAX_CONSECUTIVE_ERRORS = 3  # failed cycles before the connection is reset
MAX_BACKOFF_S = 30.0


class Sink(Protocol):
    def enqueue(self, item: Any) -> None: ...


class MachineRunner:
    def __init__(self, cfg: MachineConfig, machine_id: int, driver: Driver, sink: Sink, publisher: Any,
                 collector_id: str, last_counters: dict[str, int] | None = None) -> None:
        self.cfg = cfg
        self.mid = machine_id
        self.driver = driver
        self.sink = sink
        self.pub = publisher
        self.collector_id = collector_id
        self.table = SignalTable(cfg)
        self.tracker = ev.EventTracker(last_counters)
        self.groups: dict[str, list[Block]] = {}
        for group in ("fast", "normal", "slow"):
            sigs = [s for s in cfg.signals if s.poll == group]
            if sigs:
                self.groups[group] = plan_blocks(sigs, driver.address_index)
        self.connected = False
        self.connection_error: str | None = "not connected yet"
        self.poll_errors_total = 0
        self.last_poll_ms: float | None = None
        self.last_read: float | None = None
        self.state = UNKNOWN
        self.state_since = time.time()
        self.last_eval: Evaluation | None = None
        self._last_raw: dict[str, tuple[Any, str, float]] = {}
        self._last_norm: dict[str, tuple[Any, str, float]] = {}
        self._last_current = 0.0
        self._last_state_pub = 0.0
        self._last_signals_pub = 0.0
        self._last_signals_payload: Any = None
        self.code = cfg.code
        self.topic = cfg.topic_prefix

    # -- top level ------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        log.info("[%s] starting: driver=%s host=%s groups=%s", self.code, self.cfg.driver, self.cfg.host,
                 {g: len(b) for g, b in self.groups.items()})
        tasks = [
            asyncio.create_task(self._connection_loop(stop), name=f"{self.code}-conn"),
            asyncio.create_task(self._eval_loop(stop), name=f"{self.code}-eval"),
            asyncio.create_task(self._health_loop(stop), name=f"{self.code}-health"),
        ]
        await stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.driver.disconnect()

    # -- connection + polling ---------------------------------------------------
    async def _connection_loop(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            pollers: list[asyncio.Task] = []
            try:
                await asyncio.wait_for(self.driver.connect(), self.driver.timeout * 3)
                self.connected, self.connection_error = True, None
                PLC_UP.labels(self.code).set(1)
                log.info("[%s] connected to %s", self.code, self.cfg.host)
                backoff = 1.0
                pollers = [asyncio.create_task(self._poll_group(g, blocks)) for g, blocks in self.groups.items()]
                if not pollers:
                    await stop.wait()
                    return
                done, _ = await asyncio.wait(pollers, return_when=asyncio.FIRST_EXCEPTION)
                for t in done:
                    t.result()  # re-raise
            except DriverNotImplemented as e:
                self._lost(str(e))
                log.error("[%s] %s - machine reported as UNKNOWN", self.code, e)
                backoff = 300.0
            except (DriverError, asyncio.TimeoutError, OSError) as e:
                self._lost(str(e) or type(e).__name__)
                log.warning("[%s] connection lost: %s (retry in %.0fs)", self.code, self.connection_error, backoff)
            finally:
                for t in pollers:
                    t.cancel()
                await asyncio.gather(*pollers, return_exceptions=True)
            try:
                await self.driver.disconnect()
            except Exception:  # noqa: BLE001 - best effort
                pass
            try:
                await asyncio.wait_for(stop.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            if backoff < MAX_BACKOFF_S:
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    def _lost(self, error: str) -> None:
        self.connected, self.connection_error = False, error
        PLC_UP.labels(self.code).set(0)
        self.table.mark_bad(None, error)

    async def _poll_group(self, group: str, blocks: list[Block]) -> None:
        interval = self.cfg.poll_ms[group] / 1000.0
        names = [s.name for b in blocks for s in b.signals]
        errors = 0
        next_t = time.monotonic()
        while True:
            t0 = time.monotonic()
            try:
                for b in blocks:
                    start = self.driver.format_number(b.device, b.start)
                    raw = await asyncio.wait_for(self.driver.read_block(b.device, start, b.count, b.bits),
                                                 self.driver.timeout * 2)
                    wall, mono = time.time(), time.monotonic()
                    for name, value in decode_block(b, raw, self.driver.address_index).items():
                        self.table.update(name, value, wall, mono)
                        self._raw(name, value, GOOD, wall)
                errors = 0
                self.last_read = time.time()
                LAST_READ.labels(self.code).set(self.last_read)
                self.last_poll_ms = (time.monotonic() - t0) * 1000
                POLL_DURATION.labels(self.code, group).observe(self.last_poll_ms)
            except (DriverError, asyncio.TimeoutError) as e:
                errors += 1
                self.poll_errors_total += 1
                POLL_ERRORS.labels(self.code, group).inc()
                self.table.mark_bad(names, str(e) or type(e).__name__)
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    raise DriverError(f"{group}: {errors} consecutive failures, last: {e}") from e
            next_t += interval
            delay = next_t - time.monotonic()
            if delay < 0:  # overrun: don't try to catch up with a burst of reads
                next_t, delay = time.monotonic(), 0
            await asyncio.sleep(delay)

    def _raw(self, name: str, value: Any, quality: str, wall: float) -> None:
        last = self._last_raw.get(name)
        if last and last[0] == value and last[1] == quality and wall - last[2] < HEARTBEAT_S:
            return
        self._last_raw[name] = (value, quality, wall)
        sig = self.cfg.signal(name)
        self.sink.enqueue(RawSample(self.mid, wall, name, sig.address, value, quality))

    # -- evaluation ---------------------------------------------------------------
    async def _eval_loop(self, stop: asyncio.Event) -> None:
        interval = min(1.0, max(0.1, min(self.cfg.poll_ms.values()) / 1000.0))
        while not stop.is_set():
            try:
                self.evaluate_once()
            except Exception:  # noqa: BLE001 - never let a bug in evaluation stop collection
                log.exception("[%s] evaluation failed", self.code)
            await asyncio.sleep(interval)

    def evaluate_once(self, now: float | None = None) -> Evaluation:
        now = now if now is not None else time.time()
        snap = self.table.snapshot()
        for name, sv in snap.items():
            if sv.quality != GOOD:
                self._raw(name, None, sv.quality, now)
        e = evaluate(self.cfg, snap)
        self.last_eval = e

        for op in self.tracker.process(e, now):
            self.sink.enqueue(OpItem(self.mid, op))
            if isinstance(op, ev.StateChange):
                self.state, self.state_since = op.new, op.time
                for s in STATES:
                    MACHINE_STATE.labels(self.code, s).set(1 if s == op.new else 0)
                log.info("[%s] state %s -> %s (%s)", self.code, op.previous, op.new, op.reason)
            elif isinstance(op, ev.MachineEvent):
                self.pub.publish(f"{self.topic}/event", self.code, {"type": op.type, **op.data},
                                 e.state_quality, op.time, qos=1)
            elif isinstance(op, ev.CounterSample):
                self.pub.publish(f"{self.topic}/counter", self.code,
                                 {"counter": op.counter, "value": op.value, "delta": op.delta},
                                 e.normalized[op.counter].quality, op.time, retain=True)

        state_changed = self._last_state_pub < self.state_since
        if state_changed or now - self._last_state_pub >= STATE_PUBLISH_S:
            self.pub.publish(f"{self.topic}/state", self.code, self.state_payload(), e.state_quality, now,
                             retain=True, qos=1)
            self._last_state_pub = now

        for name, nv in e.normalized.items():
            last = self._last_norm.get(name)
            if last and last[0] == nv.value and last[1] == nv.quality and now - last[2] < HEARTBEAT_S:
                continue
            self._last_norm[name] = (nv.value, nv.quality, now)
            self.sink.enqueue(NormSample(self.mid, now, name, nv.value, nv.quality))

        if now - self._last_signals_pub >= 1.0:
            payload = self.signals_payload(snap)
            if payload != self._last_signals_payload or now - self._last_signals_pub >= STATE_PUBLISH_S:
                self.pub.publish(f"{self.topic}/signals", self.code, payload, e.state_quality, now)
                self._last_signals_payload = payload
            self._last_signals_pub = now

        if state_changed or now - self._last_current >= CURRENT_STATE_S:
            self.sink.enqueue(CurrentState(
                self.mid, self.state, self.state_since, e.state_quality, e.reason, self.connected,
                self.last_read, {k: {"value": v.value, "quality": v.quality, "unit": v.unit}
                                 for k, v in e.normalized.items()},
                self.signals_payload(snap), self.collector_id, now))
            self._last_current = now
        return e

    def state_payload(self) -> dict[str, Any]:
        e = self.last_eval
        return {
            "state": self.state,
            "since": self.state_since,
            "reason": e.reason if e else None,
            "simulated": self.cfg.simulated,
            "plc_connected": self.connected,
            "normalized": {k: v.value for k, v in e.normalized.items()} if e else {},
        }

    def signals_payload(self, snap) -> dict[str, Any]:
        return {n: {"address": self.cfg.signal(n).address, "value": sv.value if sv.quality != BAD else None,
                    "quality": sv.quality, "verified": self.cfg.signal(n).verified}
                for n, sv in snap.items()}

    # -- health -----------------------------------------------------------------
    async def _health_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            now = time.time()
            details = {"error": self.connection_error, "driver": self.cfg.driver,
                       "driver_health": self.driver.health().as_dict()}
            self.sink.enqueue(Health(self.mid, now, self.collector_id, self.connected, self.last_poll_ms,
                                     self.poll_errors_total, self.last_read, details))
            self.pub.publish(f"{self.topic}/health", self.code, {
                "collector_up": True, "plc_connected": self.connected,
                "last_successful_read": self.last_read, "poll_duration_ms": self.last_poll_ms,
                "poll_errors_total": self.poll_errors_total, "error": self.connection_error,
            }, "good" if self.connected else "bad", now, retain=True)
            try:
                await asyncio.wait_for(stop.wait(), HEALTH_S)
            except asyncio.TimeoutError:
                pass
