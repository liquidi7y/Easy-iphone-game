"""Driver for the Nueva PLC simulator (services/simulator).

The simulator exposes its device memory over a tiny read-only HTTP API:

    GET /api/v1/read?device=M&start=9000&count=6   -> {"values": [0, 1, ...]}

It lets the full platform (collector -> DB/MQTT -> API -> Grafana) run
without a live production PLC.
"""

from __future__ import annotations

import httpx

from .base import Driver, DriverError


class SimulatorDriver(Driver):
    name = "simulator"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._base = f"http://{self.host}:{self.port or 8502}"

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base, timeout=self.timeout)
        try:
            r = await self._client.get("/health")
            r.raise_for_status()
        except httpx.HTTPError as e:
            self._health.connected = False
            self._mark_error(e)
            raise DriverError(f"simulator at {self._base} unreachable: {e}") from e
        self._health.connects += 1
        self._mark_success()

    async def disconnect(self) -> None:
        self._health.connected = False
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _read(self, device: str, start: str, count: int) -> list[int]:
        if self._client is None:
            raise DriverError("not connected")
        try:
            r = await self._client.get(
                "/api/v1/read", params={"device": device, "start": start, "count": count})
            r.raise_for_status()
            values = r.json()["values"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            self._health.connected = False
            self._mark_error(e)
            raise DriverError(f"read {device}{start}x{count} failed: {e}") from e
        if len(values) != count:
            raise DriverError(f"read {device}{start}x{count}: got {len(values)} values")
        self._mark_success()
        return values

    async def read_bits(self, device: str, start: str, count: int) -> list[bool]:
        return [bool(v) for v in await self._read(device, start, count)]

    async def read_words(self, device: str, start: str, count: int) -> list[int]:
        return [int(v) & 0xFFFF for v in await self._read(device, start, count)]
