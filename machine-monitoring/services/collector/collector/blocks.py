"""Group signals into contiguous read blocks and decode raw words.

Reading one block of 20 words is far cheaper for a PLC than 20 single
reads, so signals in the same poll group and device are merged into
contiguous ranges (allowing small gaps).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Callable

from .config import Signal

MAX_GAP = 16          # merge ranges separated by at most this many unused points
MAX_BLOCK_WORDS = 480  # stay well below SLMP's 960-word batch read limit
MAX_BLOCK_BITS = 3584


@dataclass(frozen=True)
class Block:
    device: str
    bits: bool
    start: int
    count: int
    signals: tuple[Signal, ...]


def plan_blocks(signals: list[Signal], index_of: Callable[[str, str], int]) -> list[Block]:
    """Plan read blocks for one poll group."""
    by_key: dict[tuple[str, bool], list[tuple[int, Signal]]] = {}
    for s in signals:
        by_key.setdefault((s.device, s.is_bit), []).append((index_of(s.device, s.number), s))

    blocks: list[Block] = []
    for (device, bits), items in sorted(by_key.items()):
        items.sort(key=lambda x: x[0])
        limit = MAX_BLOCK_BITS if bits else MAX_BLOCK_WORDS
        cur_start, cur_end, cur = None, None, []  # cur_end is exclusive
        for idx, sig in items:
            width = 1 if bits else sig.words
            if cur_start is not None and idx - cur_end <= MAX_GAP and (idx + width - cur_start) <= limit:
                cur_end = max(cur_end, idx + width)
                cur.append(sig)
                continue
            if cur_start is not None:
                blocks.append(Block(device, bits, cur_start, cur_end - cur_start, tuple(cur)))
            cur_start, cur_end, cur = idx, idx + width, [sig]
        if cur_start is not None:
            blocks.append(Block(device, bits, cur_start, cur_end - cur_start, tuple(cur)))
    return blocks


def decode(sig: Signal, raw: list[Any], offset: int) -> float | int | bool:
    """Decode one signal from a block result. Multi-word values are low word first
    (Mitsubishi convention)."""
    if sig.type == "bool":
        return bool(raw[offset])
    words = raw[offset:offset + sig.words]
    if sig.type == "uint16":
        value: float | int = words[0] & 0xFFFF
    elif sig.type == "int16":
        value = struct.unpack("<h", struct.pack("<H", words[0] & 0xFFFF))[0]
    else:
        packed = struct.pack("<HH", words[0] & 0xFFFF, words[1] & 0xFFFF)
        value = struct.unpack({"uint32": "<I", "int32": "<i", "float32": "<f"}[sig.type], packed)[0]
    if sig.scale is not None or sig.offset is not None:
        value = value * (sig.scale if sig.scale is not None else 1) + (sig.offset or 0)
    return value


def decode_block(block: Block, raw: list[Any], index_of: Callable[[str, str], int]) -> dict[str, Any]:
    return {s.name: decode(s, raw, index_of(s.device, s.number) - block.start) for s in block.signals}
