"""Collector runner against a live simulator HTTP server (no database/MQTT).

Exercises driver -> block reads -> decoding -> state evaluation -> events,
plus the PLC-offline path (state must become UNKNOWN, never STOPPED).
"""

import asyncio
import socket

import pytest
import yaml

from collector import events as ev
from collector.config import load_machine_file, load_schema
from collector.drivers import create_driver
from collector.mqtt import NullPublisher
from collector.runner import MachineRunner
from collector.storage import CurrentState, OpItem, RawSample
from conftest import CONFIG_DIR, SCHEMA
from simulator import Simulator, serve


class ListSink:
    def __init__(self):
        self.items = []

    def enqueue(self, item):
        self.items.append(item)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def sim_server():
    doc = yaml.safe_load((CONFIG_DIR / "sim-rdc53.yaml").read_text())
    sim = Simulator(doc, seed=7, speedup=1.0)
    sim.control({"force": "RUNNING"})
    sim.model.remaining = 1e9
    port = free_port()
    httpd = serve(sim, port, host="127.0.0.1")
    yield sim, port
    httpd.shutdown()
    sim.stop()


async def wait_for(pred, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.1)
    return False


async def test_end_to_end_with_simulator(sim_server):
    sim, port = sim_server
    cfg = load_machine_file(CONFIG_DIR / "sim-rdc53.yaml", load_schema(SCHEMA),
                            env={"SIMULATOR_HOST": "127.0.0.1", "SIMULATOR_PORT": str(port)})
    driver = create_driver(cfg.driver, cfg.host, cfg.port, cfg.timeout_ms, cfg.options)
    sink = ListSink()
    runner = MachineRunner(cfg, 1, driver, sink, NullPublisher(), "test")
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop))
    try:
        assert await wait_for(lambda: runner.state == "RUNNING"), runner.last_eval
        assert runner.connected
        start = runner.last_eval.normalized["counter.total"].value
        assert await wait_for(lambda: runner.last_eval.normalized["counter.total"].value > start, 6)

        sim.control({"force": "FAULT"})
        assert await wait_for(lambda: runner.state == "FAULT")

        sim.control({"offline": True})
        assert await wait_for(lambda: runner.state == "UNKNOWN", 15), "offline PLC must give UNKNOWN"
        assert await wait_for(lambda: not runner.connected, 10), "connection reset after consecutive failures"

        sim.control({"offline": False, "force": "RUNNING"})
        assert await wait_for(lambda: runner.state == "RUNNING", 15)
    finally:
        stop.set()
        await task

    ops = [i.op for i in sink.items if isinstance(i, OpItem)]
    event_types = [o.type for o in ops if isinstance(o, ev.MachineEvent)]
    for t in ("RUN_START", "FAULT_START", "MONITORING_LOST", "MONITORING_RESTORED"):
        assert t in event_types, (t, event_types)
    assert "STOPPED" not in [o.new for o in ops if isinstance(o, ev.StateChange)]
    assert any(isinstance(o, ev.CounterSample) and o.delta for o in ops)
    assert any(isinstance(i, RawSample) and i.quality == "bad" for i in sink.items)
    assert any(isinstance(i, CurrentState) for i in sink.items)
