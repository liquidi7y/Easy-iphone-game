"""Mitsubishi SLMP / MC protocol (3E frame, binary) - READ ONLY.

Placeholder for milestone M4. Per the implementation order, the platform is
built and proven end-to-end against the simulator before any Mitsubishi
code talks to a real PLC.

Scope when implemented (and nothing more):
  * batch read, word units   (command 0x0401, subcommand 0x0000)
  * batch read, bit units    (command 0x0401, subcommand 0x0001)
  * connection health via a harmless read of a configured word
  * X/Y radix per CPU family: hexadecimal on iQ-R/Q/L, octal on iQ-F
Explicitly out of scope: batch write (0x1401), random write, remote
RUN/STOP/PAUSE (0x1001-0x1006), file/program commands, loopback-based
"discovery" that touches PLC parameters.
"""

from __future__ import annotations

from .base import Driver, DriverNotImplemented

_HEX_DEVICES = {"X", "Y", "B", "W", "SB", "SW", "DX", "DY"}


class MitsubishiSlmpDriver(Driver):
    name = "mitsubishi_slmp"

    def address_index(self, device: str, number: str) -> int:
        if device in _HEX_DEVICES:
            if device in {"X", "Y"} and self.options.get("xy_radix") == "octal":
                return int(number, 8)
            return int(number, 16)
        return int(number, 10)

    def format_number(self, device: str, index: int) -> str:
        if device in _HEX_DEVICES:
            if device in {"X", "Y"} and self.options.get("xy_radix") == "octal":
                return format(index, "o")
            return format(index, "X")
        return str(index)

    async def connect(self) -> None:
        raise DriverNotImplemented("mitsubishi_slmp driver is planned for milestone M4")

    async def disconnect(self) -> None:
        return None

    async def read_bits(self, device: str, start: str, count: int) -> list[bool]:
        raise DriverNotImplemented("mitsubishi_slmp driver is planned for milestone M4")

    async def read_words(self, device: str, start: str, count: int) -> list[int]:
        raise DriverNotImplemented("mitsubishi_slmp driver is planned for milestone M4")
