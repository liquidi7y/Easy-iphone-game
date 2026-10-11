import copy

import pytest
import yaml

from collector.config import ConfigError, expand_env, load_machine_dir, load_schema, parse_machine, split_address
from conftest import CONFIG_DIR, SCHEMA


@pytest.fixture(scope="module")
def schema():
    return load_schema(SCHEMA)


@pytest.fixture
def sim_doc():
    return yaml.safe_load((CONFIG_DIR / "sim-rdc53.yaml").read_text())


def test_all_committed_machine_files_are_valid():
    machines = load_machine_dir(CONFIG_DIR, SCHEMA, env={})
    codes = {m.code for m in machines}
    assert {"RDC53", "SIM01"} <= codes


def test_rdc53_is_safe_by_default():
    rdc = next(m for m in load_machine_dir(CONFIG_DIR, SCHEMA, env={}) if m.code == "RDC53")
    assert rdc.enabled is False, "RDC53 must stay disabled until the M4 driver and Phase 0 survey are done"
    assert all(not s.verified for s in rdc.signals), "no RDC53 address is verified yet"
    assert rdc.state_rules == [], "no state rules before RUN/STOP/FAULT signals are verified"


def test_read_only_cannot_be_disabled(schema, sim_doc):
    sim_doc["connection"]["read_only"] = False
    with pytest.raises(ConfigError, match="read_only"):
        parse_machine(sim_doc, schema, env={})


def test_unknown_signal_in_state_rule_rejected(schema, sim_doc):
    sim_doc["state"]["rules"][0]["when"] = {"signal": "does_not_exist", "equals": True}
    with pytest.raises(ConfigError, match="unknown signal"):
        parse_machine(sim_doc, schema, env={})


def test_unknown_signal_in_mapping_rejected(schema, sim_doc):
    sim_doc["normalized"]["machine.speed"] = {"signal": "nope"}
    with pytest.raises(ConfigError, match="unknown signal"):
        parse_machine(sim_doc, schema, env={})


def test_unknown_normalized_name_rejected(schema, sim_doc):
    sim_doc["normalized"]["machine.banana"] = {"signal": "sim_speed"}
    with pytest.raises(ConfigError, match="schema"):
        parse_machine(sim_doc, schema, env={})


def test_unknown_state_rejected(schema, sim_doc):
    sim_doc["state"]["rules"][0]["state"] = "UNKNOWN"  # reserved: derived, not configurable
    with pytest.raises(ConfigError, match="schema"):
        parse_machine(sim_doc, schema, env={})


def test_duplicate_address_rejected(schema, sim_doc):
    dup = copy.deepcopy(sim_doc["signals"][0])
    dup["name"] = "another"
    sim_doc["signals"].append(dup)
    with pytest.raises(ConfigError, match="duplicate address"):
        parse_machine(sim_doc, schema, env={})


def test_env_expansion():
    assert expand_env("${A}", {"A": "1"}) == "1"
    assert expand_env("${A:-x}", {}) == "x"
    assert expand_env({"k": ["${A:-x}"]}, {"A": "y"}) == {"k": ["y"]}
    with pytest.raises(ConfigError):
        expand_env("${MISSING}", {})


def test_port_from_env(schema, sim_doc):
    cfg = parse_machine(sim_doc, schema, env={"SIMULATOR_PORT": "9999"})
    assert cfg.port == 9999 and cfg.host == "simulator"


@pytest.mark.parametrize("addr,expected", [
    ("M461", ("M", "461")), ("D9002", ("D", "9002")), ("X1F", ("X", "1F")),
    ("SM400", ("SM", "400")), ("ZR100", ("ZR", "100")),
])
def test_split_address(addr, expected):
    assert split_address(addr) == expected
