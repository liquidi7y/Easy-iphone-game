import struct

import yaml

from conftest import CONFIG_DIR
from simulator import Model, Simulator


def doc():
    return yaml.safe_load((CONFIG_DIR / "sim-rdc53.yaml").read_text())


def test_model_produces_while_running():
    m = Model(seed=1)
    m.force("RUNNING")
    m.remaining = 1e9
    for _ in range(600):
        m.step(0.1)
    assert m.v["counter_total"] > 30 and m.v["speed"] > 0 and m.v["running"]


def test_model_visits_all_phases():
    m = Model(seed=3)
    seen = set()
    for _ in range(200_000):
        m.step(0.5)
        seen.add(m.phase)
    assert seen == {"STOPPED", "READY", "RUNNING", "IDLE", "FAULT", "E_STOP"}


def test_memory_layout_matches_machine_yaml():
    sim = Simulator(doc(), seed=1)
    sim.model.v.update(counter_total=70000, ready=True, motor_temp=-1.5)
    sim.memory.load(sim.model.v)
    lo, hi = sim.read("D", 9002, 2)
    assert struct.unpack("<I", struct.pack("<HH", lo, hi))[0] == 70000
    assert sim.read("M", 9000, 1) == [1]
    assert struct.unpack("<h", struct.pack("<H", sim.read("D", 9010, 1)[0]))[0] == -15


def test_offline_control():
    sim = Simulator(doc(), seed=1)
    assert sim.control({"offline": True})["offline"] is True
    assert sim.control({"force": "fault"})["phase"] == "FAULT"
