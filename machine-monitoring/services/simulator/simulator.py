"""Nueva PLC simulator - a fake stacker for development and CI.

Runs a simple RDC-53-like process model (stopped -> ready -> running with
counter/speed, idle starvation, faults, occasional E-stop) and exposes the
resulting device memory over a small HTTP API, using the address map from
a machine YAML file (signals carrying `sim_role`).

Read path (used by the collector's `simulator` driver):
    GET  /health
    GET  /api/v1/read?device=D&start=9000&count=6   -> {"values": [...]}
Debug/test controls (the SIMULATOR's own state - there is no real machine here):
    GET  /api/v1/state
    POST /api/v1/control   {"offline": true} | {"force": "FAULT"} | {"speedup": 10}

Environment: SIM_MACHINE_CONFIG, SIM_PORT (8502), SIM_SEED, SIM_SPEEDUP (1.0)
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

log = logging.getLogger("simulator")

ROLES = {"ready", "running", "fault", "estop", "feeder", "stack_cycle", "speed",
         "counter_total", "counter_reject", "fault_code", "motor_temp"}
FAULT_CODES = (101, 205, 317, 402)
MAX_READ = 960


class Model:
    """Process model in engineering units. step(dt) advances simulated time."""

    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed)
        self.phase = "STOPPED"
        self.remaining = 5.0
        self.v: dict[str, Any] = {
            "ready": False, "running": False, "fault": False, "estop": False, "feeder": False,
            "stack_cycle": False, "speed": 0, "counter_total": 0, "counter_reject": 0,
            "fault_code": 0, "motor_temp": 24.0,
        }
        self._sheet_acc = 0.0
        self._stack_pulse = 0.0
        self.target_speed = 42

    def _enter(self, phase: str) -> None:
        r = self.rng
        self.phase = phase
        v = self.v
        if phase == "STOPPED":
            self.remaining = r.uniform(10, 30)
            v.update(ready=False, running=False, feeder=False, speed=0, fault=False, estop=False, fault_code=0)
        elif phase == "READY":
            self.remaining = r.uniform(5, 15)
            v.update(ready=True, running=False, feeder=False, speed=0, fault=False, estop=False, fault_code=0)
        elif phase == "RUNNING":
            self.remaining = r.uniform(60, 300)
            self.target_speed = r.randint(36, 48)
            v.update(ready=True, running=True, feeder=True, fault=False, estop=False)
        elif phase == "IDLE":
            self.remaining = r.uniform(10, 40)
            v.update(running=True, feeder=False, speed=0)
        elif phase == "FAULT":
            self.remaining = r.uniform(15, 60)
            v.update(running=False, feeder=False, speed=0, fault=True, fault_code=r.choice(FAULT_CODES))
        elif phase == "E_STOP":
            self.remaining = r.uniform(15, 30)
            v.update(ready=False, running=False, feeder=False, speed=0, estop=True)
        else:
            raise ValueError(phase)

    def force(self, phase: str) -> None:
        self._enter(phase)

    def _next(self) -> str:
        r = self.rng.random()
        return {
            "STOPPED": lambda: "READY",
            "READY": lambda: "RUNNING",
            "RUNNING": lambda: "IDLE" if r < 0.65 else "FAULT" if r < 0.87 else "STOPPED" if r < 0.97 else "E_STOP",
            "IDLE": lambda: "RUNNING" if r < 0.9 else "STOPPED",
            "FAULT": lambda: "READY",
            "E_STOP": lambda: "STOPPED",
        }[self.phase]()

    def step(self, dt: float) -> None:
        self.remaining -= dt
        if self.remaining <= 0:
            self._enter(self._next())
        v = self.v
        if self.phase == "RUNNING":
            v["speed"] = max(1, round(self.target_speed + self.rng.gauss(0, 1.2)))
            self._sheet_acc += v["speed"] / 60.0 * dt
            while self._sheet_acc >= 1:
                self._sheet_acc -= 1
                v["counter_total"] = (v["counter_total"] + 1) & 0xFFFFFFFF
                if self.rng.random() < 0.012:
                    v["counter_reject"] = (v["counter_reject"] + 1) & 0xFFFFFFFF
                if v["counter_total"] % 25 == 0:
                    self._stack_pulse = 1.0
        self._stack_pulse = max(0.0, self._stack_pulse - dt)
        v["stack_cycle"] = self._stack_pulse > 0
        target_temp = 55.0 if v["running"] and v["speed"] else 30.0
        v["motor_temp"] += (target_temp - v["motor_temp"]) * min(1.0, dt / 120.0) + self.rng.gauss(0, 0.05)


def _split(address: str) -> tuple[str, int]:
    i = 0
    while i < len(address) and address[i].isalpha():
        i += 1
    return address[:i], int(address[i:])


class Memory:
    """Device memory built from the machine YAML: {device: {index: int}}."""

    def __init__(self, machine_doc: dict[str, Any]) -> None:
        self.bindings: list[tuple[str, str, int, str, float]] = []  # role, device, index, type, scale
        self.data: dict[str, dict[int, int]] = {}
        for s in machine_doc.get("signals", []):
            role = s.get("sim_role")
            if not role:
                continue
            if role not in ROLES:
                raise ValueError(f"unknown sim_role {role!r} on {s['address']}")
            dev, idx = _split(s["address"])
            self.bindings.append((role, dev, idx, s["type"], s.get("scale") or 1.0))
            self.data.setdefault(dev, {})

    def load(self, values: dict[str, Any]) -> None:
        for role, dev, idx, typ, scale in self.bindings:
            v = values[role]
            mem = self.data[dev]
            if typ == "bool":
                mem[idx] = 1 if v else 0
                continue
            raw = v / scale if scale != 1.0 else v
            if typ in ("uint16", "int16"):
                mem[idx] = struct.unpack("<H", struct.pack("<h" if typ == "int16" else "<H", int(round(raw))))[0]
            else:
                fmt = {"uint32": "<I", "int32": "<i", "float32": "<f"}[typ]
                packed = struct.pack(fmt, float(raw) if typ == "float32" else int(round(raw)))
                lo, hi = struct.unpack("<HH", packed)
                mem[idx], mem[idx + 1] = lo, hi

    def read(self, device: str, start: int, count: int) -> list[int]:
        mem = self.data.get(device, {})
        return [mem.get(i, 0) for i in range(start, start + count)]


class Simulator:
    def __init__(self, machine_doc: dict[str, Any], seed: int | None = None, speedup: float = 1.0) -> None:
        self.model = Model(seed)
        self.memory = Memory(machine_doc)
        self.code = machine_doc["machine"]["code"]
        self.speedup = speedup
        self.offline = False
        self.lock = threading.Lock()
        self.memory.load(self.model.v)
        self._stop = threading.Event()

    def tick(self, dt: float) -> None:
        with self.lock:
            self.model.step(dt * self.speedup)
            self.memory.load(self.model.v)

    def run(self, period: float = 0.05) -> None:
        last = time.monotonic()
        while not self._stop.wait(period):
            now = time.monotonic()
            self.tick(now - last)
            last = now

    def stop(self) -> None:
        self._stop.set()

    def read(self, device: str, start: int, count: int) -> list[int]:
        with self.lock:
            return self.memory.read(device, start, count)

    def state(self) -> dict[str, Any]:
        with self.lock:
            return {"machine": self.code, "phase": self.model.phase, "remaining_s": round(self.model.remaining, 1),
                    "offline": self.offline, "speedup": self.speedup, "values": dict(self.model.v)}

    def control(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if "offline" in body:
                self.offline = bool(body["offline"])
            if "speedup" in body:
                self.speedup = max(0.1, min(100.0, float(body["speedup"])))
            if "force" in body:
                self.model.force(str(body["force"]).upper())
                self.memory.load(self.model.v)
        return self.state()


def make_handler(sim: Simulator) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "nueva-plc-simulator/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet access log
            log.debug(fmt, *args)

        def _json(self, code: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/health":
                if sim.offline:
                    return self._json(503, {"status": "offline"})
                return self._json(200, {"status": "ok", "machine": sim.code})
            if url.path == "/api/v1/state":
                return self._json(200, sim.state())
            if url.path == "/api/v1/read":
                if sim.offline:
                    return self._json(503, {"error": "simulated PLC offline"})
                q = parse_qs(url.query)
                try:
                    device = q["device"][0]
                    start, count = int(q["start"][0]), int(q["count"][0])
                except (KeyError, ValueError):
                    return self._json(400, {"error": "device, start, count required"})
                if not 1 <= count <= MAX_READ or start < 0:
                    return self._json(400, {"error": f"count must be 1..{MAX_READ}"})
                return self._json(200, {"device": device, "start": start, "values": sim.read(device, start, count)})
            return self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/api/v1/control":
                return self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                return self._json(200, sim.control(body))
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": str(e)})

    return Handler


def serve(sim: Simulator, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(sim))
    threading.Thread(target=sim.run, daemon=True, name="model").start()
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    return httpd


def _raise_interrupt(*_: Any) -> None:
    raise KeyboardInterrupt


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = os.environ.get("SIM_MACHINE_CONFIG", "/config/sim-rdc53.yaml")
    with open(path) as f:
        doc = yaml.safe_load(f)
    seed = os.environ.get("SIM_SEED")
    sim = Simulator(doc, int(seed) if seed else None, float(os.environ.get("SIM_SPEEDUP", "1.0")))
    port = int(os.environ.get("SIM_PORT", "8502"))
    httpd = serve(sim, port)
    log.info("simulating %s from %s on :%d", sim.code, path, port)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    last_phase = None
    try:
        while True:
            time.sleep(1)
            if sim.model.phase != last_phase:
                last_phase = sim.model.phase
                log.info("phase -> %s (total=%s)", last_phase, sim.model.v["counter_total"])
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        sim.stop()


if __name__ == "__main__":
    main()
