"""Common, read-only driver interface.

Every protocol adapter implements exactly these operations:

    connect()      open the connection (idempotent)
    health()       report connection health
    read_bits()    read `count` consecutive bit devices
    read_words()   read `count` consecutive 16-bit word devices
    read_block()   convenience dispatcher for one contiguous block
    disconnect()   close the connection (idempotent)

There is deliberately NO write method in monitoring v1. The base class
refuses to define subclasses that add one (see __init_subclass__), and a
unit test asserts the same, so a write path cannot creep in unnoticed.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any

_FORBIDDEN_PREFIXES = ("write", "force", "set_", "put", "run_", "stop_", "start_", "upload", "download")


class DriverError(Exception):
    """Communication with the controller failed. The runner reconnects with backoff."""


class DriverNotImplemented(DriverError):
    """The driver exists as a placeholder only (later milestone)."""


@dataclass
class DriverHealth:
    connected: bool = False
    last_error: str | None = None
    last_success: float | None = None
    connects: int = 0
    errors: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "last_error": self.last_error,
            "last_success": self.last_success,
            "connects": self.connects,
            "errors": self.errors,
            **self.details,
        }


class Driver(abc.ABC):
    """Base class for read-only protocol drivers."""

    name: str = "base"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for attr in vars(cls):
            if attr.lower().startswith(_FORBIDDEN_PREFIXES):
                raise TypeError(
                    f"driver {cls.__name__} defines {attr!r}: monitoring drivers are read-only "
                    "and may not expose write/control operations"
                )

    def __init__(self, host: str, port: int | None, timeout_ms: int = 1000,
                 options: dict[str, Any] | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout_ms / 1000.0
        self.options = options or {}
        self._health = DriverHealth()

    # -- lifecycle -----------------------------------------------------------
    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    def health(self) -> DriverHealth:
        return self._health

    # -- reads ---------------------------------------------------------------
    @abc.abstractmethod
    async def read_bits(self, device: str, start: str, count: int) -> list[bool]: ...

    @abc.abstractmethod
    async def read_words(self, device: str, start: str, count: int) -> list[int]:
        """Return unsigned 16-bit values."""

    async def read_block(self, device: str, start: str, count: int, bits: bool) -> list[Any]:
        if bits:
            return await self.read_bits(device, start, count)
        return await self.read_words(device, start, count)

    # -- address helpers -----------------------------------------------------
    def address_index(self, device: str, number: str) -> int:
        """Convert the numeric part of an address to an integer index.

        Default: decimal. Drivers override for vendor radix rules
        (e.g. Mitsubishi X/Y are hexadecimal on iQ-R, octal on iQ-F).
        """
        return int(number, 10)

    def format_number(self, device: str, index: int) -> str:
        return str(index)

    # -- bookkeeping for subclasses -----------------------------------------
    def _mark_success(self) -> None:
        self._health.connected = True
        self._health.last_success = time.time()

    def _mark_error(self, err: BaseException | str) -> None:
        self._health.errors += 1
        self._health.last_error = str(err)
