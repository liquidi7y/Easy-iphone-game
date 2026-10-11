import time

import pytest

from collector import events as ev
from collector.config import load_machine_dir
from collector.model import BAD, GOOD, STALE, UNCERTAIN, UNKNOWN, SignalTable, evaluate
from conftest import CONFIG_DIR, SCHEMA

BASE = dict(sim_ready=True, sim_running=True, sim_fault=False, sim_estop=False, sim_feeder_active=True,
            sim_stack_cycle=False, sim_speed=40, sim_counter_total=100, sim_counter_reject=1,
            sim_fault_code=0, sim_motor_temp=40.0)


@pytest.fixture
def cfg():
    return next(m for m in load_machine_dir(CONFIG_DIR, SCHEMA, env={}) if m.code == "SIM01")


def table_with(cfg, **over):
    t = SignalTable(cfg)
    now = time.monotonic()
    for k, v in {**BASE, **over}.items():
        t.update(k, v, time.time(), now)
    return t


@pytest.mark.parametrize("over,state", [
    ({}, "RUNNING"),
    ({"sim_speed": 0}, "IDLE"),
    ({"sim_running": False}, "READY"),
    ({"sim_running": False, "sim_ready": False}, "STOPPED"),
    ({"sim_fault": True}, "FAULT"),
    ({"sim_fault": True, "sim_estop": True}, "E_STOP"),
])
def test_state_rules(cfg, over, state):
    e = evaluate(cfg, table_with(cfg, **over).snapshot())
    assert e.state == state and e.state_quality == GOOD


def test_never_read_is_unknown_not_stopped(cfg):
    e = evaluate(cfg, SignalTable(cfg).snapshot())
    assert e.state == UNKNOWN and e.state_quality == BAD


def test_comm_failure_is_unknown(cfg):
    t = table_with(cfg)
    t.mark_bad(None, "timeout")
    e = evaluate(cfg, t.snapshot())
    assert e.state == UNKNOWN
    assert all(v.value is None for v in e.normalized.values())


def test_stale_inputs_are_unknown(cfg):
    t = table_with(cfg)
    e = evaluate(cfg, t.snapshot(time.monotonic() + 3600))
    assert e.state == UNKNOWN and e.state_quality == STALE


def test_partial_staleness_is_unknown(cfg):
    t = table_with(cfg)
    t.mark_bad(["sim_estop"], "timeout")  # E-stop unknown -> cannot claim RUNNING
    assert evaluate(cfg, t.snapshot()).state == UNKNOWN


def test_unverified_signal_yields_uncertain(cfg):
    sig = cfg.signal("sim_speed")
    idx = cfg.signals.index(sig)
    cfg.signals[idx] = type(sig)(**{**sig.__dict__, "status": "unverified"})
    e = evaluate(cfg, table_with(cfg).snapshot())
    assert e.state == "RUNNING" and e.state_quality == UNCERTAIN
    assert e.normalized["machine.speed"].quality == UNCERTAIN


def run(tracker, cfg, **over):
    return tracker.process(evaluate(cfg, table_with(cfg, **over).snapshot()), time.time())


def types(ops):
    return [o.type for o in ops if isinstance(o, ev.MachineEvent)]


def test_events_lifecycle(cfg):
    tr = ev.EventTracker()
    ops = run(tr, cfg)
    assert isinstance(ops[0], ev.StateChange) and ops[0].previous is None and ops[0].new == "RUNNING"
    assert "RUN_START" in types(ops)
    assert any(isinstance(o, ev.DowntimeClose) for o in ops)
    assert any(isinstance(o, ev.CounterSample) and o.delta is None for o in ops)

    ops = run(tr, cfg, sim_counter_total=105)
    cs = [o for o in ops if isinstance(o, ev.CounterSample)]
    assert cs and cs[0].delta == 5 and not any(isinstance(o, ev.StateChange) for o in ops)

    ops = run(tr, cfg, sim_counter_total=105, sim_fault=True, sim_fault_code=205, sim_running=False)
    assert {"RUN_STOP", "FAULT_START"} <= set(types(ops))
    assert any(isinstance(o, ev.DowntimeEnsure) and o.state == "FAULT" for o in ops)
    assert any(isinstance(o, ev.FaultEnsure) and o.code == 205 for o in ops)

    ops = run(tr, cfg, sim_counter_total=105)
    assert {"FAULT_CLEAR", "RUN_START"} <= set(types(ops))
    assert any(isinstance(o, ev.FaultClose) for o in ops)


def test_monitoring_loss_does_not_touch_downtime(cfg):
    tr = ev.EventTracker()
    run(tr, cfg)
    t = table_with(cfg)
    t.mark_bad(None, "link down")
    ops = tr.process(evaluate(cfg, t.snapshot()), time.time())
    assert "MONITORING_LOST" in types(ops)
    assert not any(isinstance(o, (ev.DowntimeEnsure, ev.DowntimeClose, ev.FaultClose)) for o in ops)
    ops = run(tr, cfg)
    assert "MONITORING_RESTORED" in types(ops)


def test_counter_reset_and_seeded_counters(cfg):
    tr = ev.EventTracker({"counter.total": 90})
    ops = run(tr, cfg)  # 90 -> 100 across a collector restart
    assert next(o for o in ops if isinstance(o, ev.CounterSample) and o.counter == "counter.total").delta == 10
    ops = run(tr, cfg, sim_counter_total=3)
    assert "COUNTER_RESET" in types(ops)
    assert next(o for o in ops if isinstance(o, ev.CounterSample) and o.counter == "counter.total").delta == 3


def test_late_fault_code_fills_the_same_fault(cfg):
    tr = ev.EventTracker()
    run(tr, cfg)
    ops = run(tr, cfg, sim_fault=True, sim_fault_code=0, sim_running=False)
    assert types(ops).count("FAULT_START") == 1
    assert [o.code for o in ops if isinstance(o, ev.FaultEnsure)] == [None]
    ops = run(tr, cfg, sim_fault=True, sim_fault_code=317, sim_running=False)
    assert "FAULT_START" not in types(ops) and "FAULT_CODE_CHANGE" not in types(ops)
    assert [o.code for o in ops if isinstance(o, ev.FaultEnsure)] == [317]
    ops = run(tr, cfg, sim_fault=True, sim_fault_code=402, sim_running=False)
    assert "FAULT_CODE_CHANGE" in types(ops)
    ops = run(tr, cfg, sim_fault=False, sim_fault_code=0)
    assert "FAULT_CLEAR" in types(ops)
