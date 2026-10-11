"""Signal table (value + quality + staleness) and the normalized machine model.

Quality vocabulary used everywhere (DB, MQTT, API):
  good       value read successfully and recently, from a verified signal
  uncertain  value is fresh but derived from at least one UNVERIFIED signal
  stale      last good read is older than the signal's stale limit
  bad        communication failure / never read

Canonical states: OFF READY RUNNING IDLE STOPPED FAULT E_STOP UNKNOWN.
UNKNOWN is reported whenever the inputs needed to decide are not fresh.
Missing data is never reported as STOPPED.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import MachineConfig

GOOD, UNCERTAIN, STALE, BAD = "good", "uncertain", "stale", "bad"
UNKNOWN = "UNKNOWN"
STATES = ("OFF", "READY", "RUNNING", "IDLE", "STOPPED", "FAULT", "E_STOP", UNKNOWN)


@dataclass
class SignalValue:
    value: Any = None
    quality: str = BAD
    ts: float | None = None          # wall clock of last successful read
    mono: float | None = None        # monotonic clock of last successful read
    error: str | None = "never read"


class SignalTable:
    def __init__(self, cfg: MachineConfig, stale_factor: float = 3.0, min_stale_s: float = 2.0) -> None:
        self.cfg = cfg
        self.values: dict[str, SignalValue] = {s.name: SignalValue() for s in cfg.signals}
        self.stale_after = {
            s.name: max(min_stale_s, stale_factor * cfg.poll_ms[s.poll] / 1000.0) for s in cfg.signals
        }

    def update(self, name: str, value: Any, wall: float | None = None, mono: float | None = None) -> None:
        sv = self.values[name]
        sv.value = value
        sv.quality = GOOD
        sv.ts = wall if wall is not None else time.time()
        sv.mono = mono if mono is not None else time.monotonic()
        sv.error = None

    def mark_bad(self, names: list[str] | None, error: str) -> None:
        for n in names if names is not None else list(self.values):
            sv = self.values[n]
            sv.quality = BAD
            sv.error = error

    def snapshot(self, mono_now: float | None = None) -> dict[str, SignalValue]:
        """Copy of all signals with staleness applied."""
        now = mono_now if mono_now is not None else time.monotonic()
        out: dict[str, SignalValue] = {}
        for n, sv in self.values.items():
            q = sv.quality
            if q == GOOD and (sv.mono is None or now - sv.mono > self.stale_after[n]):
                q = STALE
            out[n] = SignalValue(sv.value, q, sv.ts, sv.mono, sv.error)
        return out


@dataclass
class NormValue:
    value: Any
    quality: str
    unit: str | None = None


@dataclass
class Evaluation:
    state: str
    state_quality: str
    reason: str
    normalized: dict[str, NormValue] = field(default_factory=dict)
    state_inputs: dict[str, Any] = field(default_factory=dict)


def _signal_quality(cfg: MachineConfig, name: str, sv: SignalValue) -> str:
    if sv.quality != GOOD:
        return sv.quality
    return GOOD if cfg.signal(name).verified else UNCERTAIN


def eval_condition(cond: dict[str, Any], values: dict[str, Any]) -> bool:
    if "all" in cond:
        return all(eval_condition(c, values) for c in cond["all"])
    if "any" in cond:
        return any(eval_condition(c, values) for c in cond["any"])
    if "not" in cond:
        return not eval_condition(cond["not"], values)
    v = values[cond["signal"]]
    if "equals" in cond:
        return v == cond["equals"]
    if "not_equals" in cond:
        return v != cond["not_equals"]
    if "gt" in cond:
        return v is not None and v > cond["gt"]
    if "lt" in cond:
        return v is not None and v < cond["lt"]
    raise ValueError(f"invalid condition {cond!r}")


def evaluate(cfg: MachineConfig, snap: dict[str, SignalValue]) -> Evaluation:
    normalized: dict[str, NormValue] = {}
    for name, mp in cfg.mappings.items():
        sv = snap[mp.signal]
        q = _signal_quality(cfg, mp.signal, sv)
        value = sv.value if q in (GOOD, UNCERTAIN) else None
        if value is not None:
            if mp.invert:
                value = not value
            if mp.scale is not None:
                value = value * mp.scale
        normalized[name] = NormValue(value, q, mp.unit or cfg.signal(mp.signal).unit)

    if not cfg.state_rules:
        return Evaluation(UNKNOWN, BAD, "no state rules configured", normalized)

    needed = sorted(cfg.state_signals())
    qualities = {n: _signal_quality(cfg, n, snap[n]) for n in needed}
    inputs = {n: snap[n].value for n in needed}
    unavailable = [n for n, q in qualities.items() if q not in (GOOD, UNCERTAIN)]
    if unavailable:
        worst = BAD if any(qualities[n] == BAD for n in unavailable) else STALE
        return Evaluation(UNKNOWN, worst, f"inputs unavailable ({worst}): {', '.join(unavailable)}",
                          normalized, inputs)

    state_q = UNCERTAIN if UNCERTAIN in qualities.values() else GOOD
    for rule in cfg.state_rules:
        if eval_condition(rule.when, inputs):
            return Evaluation(rule.state, state_q, rule.reason or f"rule {rule.state}", normalized, inputs)
    return Evaluation(cfg.state_default or UNKNOWN, state_q, "no rule matched (default)", normalized, inputs)
