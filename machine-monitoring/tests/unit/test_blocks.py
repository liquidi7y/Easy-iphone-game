import struct

from collector.blocks import decode, decode_block, plan_blocks
from collector.config import load_machine_dir
from conftest import CONFIG_DIR, SCHEMA


def idx(dev, num):
    return int(num)


def sim():
    return next(m for m in load_machine_dir(CONFIG_DIR, SCHEMA, env={}) if m.code == "SIM01")


def test_blocks_merge_contiguous_and_respect_word_width():
    cfg = sim()
    fast = [s for s in cfg.signals if s.poll == "fast"]
    blocks = plan_blocks(fast, idx)
    by = {(b.device, b.bits): b for b in blocks}
    m = by[("M", True)]
    assert (m.start, m.count) == (9000, 6)
    d = by[("D", False)]
    assert (d.start, d.count) == (9002, 5)  # uint32 counter (2 words) + gap + fault code


def test_gap_splits_blocks():
    cfg = sim()
    blocks = plan_blocks(cfg.signals, idx)
    d = [b for b in blocks if b.device == "D"]
    assert len(d) == 1 and d[0].start == 9000 and d[0].count == 11  # 9000..9010 within MAX_GAP


def test_decode_types():
    cfg = sim()
    total = cfg.signal("sim_counter_total")
    lo, hi = struct.unpack("<HH", struct.pack("<I", 123456))
    assert decode(total, [lo, hi], 0) == 123456
    temp = cfg.signal("sim_motor_temp")  # int16, scale 0.1
    assert decode(temp, [struct.unpack("<H", struct.pack("<h", -125))[0]], 0) == -12.5


def test_decode_block_offsets():
    cfg = sim()
    blk = [b for b in plan_blocks([s for s in cfg.signals if s.poll == "fast"], idx) if b.bits][0]
    vals = decode_block(blk, [1, 0, 0, 1, 0, 0], idx)
    assert vals["sim_ready"] is True and vals["sim_estop"] is True and vals["sim_running"] is False
