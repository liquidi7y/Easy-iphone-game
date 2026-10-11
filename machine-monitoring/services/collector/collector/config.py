"""Machine configuration loading and validation.

Machine YAML files are validated against machine-config/schema/machine.schema.json,
then environment variables are expanded and cross-references (signal names
used by mappings and state rules) are checked. Any error aborts loading of
that machine: a half-understood device map must never be polled.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

DEFAULT_POLL_MS = {"fast": 500, "normal": 1000, "slow": 10000}
WORD_COUNT = {"bool": 0, "int16": 1, "uint16": 1, "int32": 2, "uint32": 2, "float32": 2}

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    pass


def expand_env(value: Any, env: dict[str, str] | None = None) -> Any:
    """Expand ${VAR} / ${VAR:-default} in strings, recursively."""
    env = os.environ if env is None else env
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in env and env[name] != "":
                return env[name]
            if default is not None:
                return default
            raise ConfigError(f"environment variable {name} is not set and has no default")
        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [expand_env(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v, env) for k, v in value.items()}
    return value


def split_address(address: str) -> tuple[str, str]:
    """'M461' -> ('M', '461'), 'D9002' -> ('D', '9002'). Radix is the driver's concern."""
    # Device codes are letters; the numeric part may contain A-F for hex devices
    # (X1F). Take the longest leading run of letters that leaves a valid number.
    for i in range(min(3, len(address) - 1), 0, -1):
        dev, num = address[:i], address[i:]
        if dev.isalpha() and re.fullmatch(r"[0-9A-F]+", num) and num[0].isdigit():
            return dev, num
    raise ConfigError(f"invalid device address {address!r}")


@dataclass(frozen=True)
class Signal:
    address: str
    name: str
    type: str
    poll: str
    status: str
    device: str
    number: str
    scale: float | None = None
    offset: float | None = None
    unit: str | None = None
    sim_role: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def is_bit(self) -> bool:
        return self.type == "bool"

    @property
    def words(self) -> int:
        return WORD_COUNT[self.type]

    @property
    def verified(self) -> bool:
        return self.status == "verified"


@dataclass(frozen=True)
class Mapping:
    name: str
    signal: str
    invert: bool = False
    scale: float | None = None
    unit: str | None = None


@dataclass(frozen=True)
class StateRule:
    state: str
    when: dict[str, Any]
    reason: str | None = None


@dataclass
class MachineConfig:
    code: str
    name: str
    site: str
    enabled: bool
    simulated: bool
    vendor: str | None
    model: str | None
    driver: str
    host: str
    port: int | None
    timeout_ms: int
    options: dict[str, Any]
    poll_ms: dict[str, int]
    signals: list[Signal]
    mappings: dict[str, Mapping]
    state_rules: list[StateRule]
    state_default: str | None
    source_file: str
    config_hash: str
    document: dict[str, Any]

    @property
    def topic_prefix(self) -> str:
        return f"factory/{self.site}/{self.code.lower()}"

    def signal(self, name: str) -> Signal:
        for s in self.signals:
            if s.name == name:
                return s
        raise KeyError(name)

    def state_signals(self) -> set[str]:
        out: set[str] = set()
        for r in self.state_rules:
            out |= condition_signals(r.when)
        return out


def condition_signals(cond: dict[str, Any]) -> set[str]:
    if "signal" in cond:
        return {cond["signal"]}
    if "all" in cond:
        return set().union(*(condition_signals(c) for c in cond["all"]))
    if "any" in cond:
        return set().union(*(condition_signals(c) for c in cond["any"]))
    if "not" in cond:
        return condition_signals(cond["not"])
    raise ConfigError(f"invalid condition {cond!r}")


def load_schema(schema_path: Path) -> dict[str, Any]:
    return json.loads(schema_path.read_text())


def parse_machine(document: dict[str, Any], schema: dict[str, Any], source: str = "<memory>",
                  env: dict[str, str] | None = None) -> MachineConfig:
    try:
        jsonschema.validate(document, schema)
    except jsonschema.ValidationError as e:
        path = "/".join(str(p) for p in e.absolute_path) or "<root>"
        raise ConfigError(f"{source}: schema violation at {path}: {e.message}") from None

    # read_only is enforced by the schema (const true); assert again so a schema
    # edit can never silently enable writes.
    if document["connection"].get("read_only") is not True:
        raise ConfigError(f"{source}: connection.read_only must be true")

    m = document["machine"]
    conn = expand_env(document["connection"], env)

    signals: list[Signal] = []
    names: set[str] = set()
    addresses: set[str] = set()
    for s in document.get("signals", []):
        if s["name"] in names:
            raise ConfigError(f"{source}: duplicate signal name {s['name']}")
        if s["address"] in addresses:
            raise ConfigError(f"{source}: duplicate address {s['address']}")
        names.add(s["name"])
        addresses.add(s["address"])
        dev, num = split_address(s["address"])
        signals.append(Signal(
            address=s["address"], name=s["name"], type=s["type"], poll=s["poll"],
            status=s["status"], device=dev, number=num, scale=s.get("scale"),
            offset=s.get("offset"), unit=s.get("unit"), sim_role=s.get("sim_role"), raw=s,
        ))
    by_name = {s.name: s for s in signals}

    mappings: dict[str, Mapping] = {}
    for norm_name, mp in (document.get("normalized") or {}).items():
        if mp["signal"] not in by_name:
            raise ConfigError(f"{source}: normalized.{norm_name} references unknown signal {mp['signal']}")
        if mp.get("invert") and by_name[mp["signal"]].type != "bool":
            raise ConfigError(f"{source}: normalized.{norm_name}: invert is only valid for bool signals")
        mappings[norm_name] = Mapping(norm_name, mp["signal"], bool(mp.get("invert", False)),
                                      mp.get("scale"), mp.get("unit"))

    rules: list[StateRule] = []
    default = None
    if document.get("state"):
        for r in document["state"]["rules"]:
            for sig in condition_signals(r["when"]):
                if sig not in by_name:
                    raise ConfigError(f"{source}: state rule {r['state']} references unknown signal {sig}")
            rules.append(StateRule(r["state"], r["when"], r.get("reason")))
        default = document["state"]["default"]

    port = conn.get("port")
    if port is not None:
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ConfigError(f"{source}: connection.port is not an integer: {port!r}") from None

    poll_ms = {**DEFAULT_POLL_MS, **(document.get("poll_groups") or {})}
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))

    return MachineConfig(
        code=m["code"], name=m["name"], site=m["site"], enabled=m.get("enabled", True),
        simulated=m.get("simulated", False), vendor=m.get("vendor"), model=m.get("model"),
        driver=conn["driver"], host=conn["host"], port=port,
        timeout_ms=conn.get("timeout_ms", 1000), options=conn.get("options") or {},
        poll_ms=poll_ms, signals=signals, mappings=mappings, state_rules=rules,
        state_default=default, source_file=source,
        config_hash=hashlib.sha256(canonical.encode()).hexdigest()[:16], document=document,
    )


def load_machine_file(path: Path, schema: dict[str, Any], env: dict[str, str] | None = None) -> MachineConfig:
    try:
        document = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from None
    if not isinstance(document, dict):
        raise ConfigError(f"{path}: expected a mapping at top level")
    return parse_machine(document, schema, str(path), env)


def load_machine_dir(config_dir: Path, schema_path: Path | None = None,
                     env: dict[str, str] | None = None) -> list[MachineConfig]:
    schema = load_schema(schema_path or config_dir / "schema" / "machine.schema.json")
    machines = [load_machine_file(p, schema, env) for p in sorted(config_dir.glob("*.yaml"))]
    codes = [m.code for m in machines]
    dupes = {c for c in codes if codes.count(c) > 1}
    if dupes:
        raise ConfigError(f"duplicate machine codes: {sorted(dupes)}")
    return machines
