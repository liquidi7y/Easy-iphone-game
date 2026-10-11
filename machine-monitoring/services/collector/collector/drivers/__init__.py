"""Driver registry. modbus_tcp, opcua and siemens_s7 arrive with later machines."""

from __future__ import annotations

from .base import Driver, DriverError, DriverNotImplemented
from .mitsubishi_slmp import MitsubishiSlmpDriver
from .simulator import SimulatorDriver

DRIVERS: dict[str, type[Driver]] = {
    SimulatorDriver.name: SimulatorDriver,
    MitsubishiSlmpDriver.name: MitsubishiSlmpDriver,
}


def create_driver(name: str, host: str, port: int | None, timeout_ms: int, options: dict) -> Driver:
    try:
        cls = DRIVERS[name]
    except KeyError:
        raise DriverNotImplemented(f"driver {name!r} is not implemented yet") from None
    return cls(host, port, timeout_ms, options)


__all__ = ["Driver", "DriverError", "DriverNotImplemented", "DRIVERS", "create_driver"]
