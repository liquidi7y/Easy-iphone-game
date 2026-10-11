"""Collector entry point.

Environment:
  MACHINE_CONFIG_DIR   directory with *.yaml machine files (default /config)
  DATABASE_URL         postgres://... (required)
  MQTT_HOST/PORT/USER/PASSWORD   broker (MQTT_HOST empty disables MQTT)
  COLLECTOR_ID         identifier in health records (default hostname)
  METRICS_PORT         Prometheus endpoint (default 9108)
  MACHINES             optional comma-separated machine codes to run
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
from pathlib import Path

from prometheus_client import start_http_server

from .config import ConfigError, load_machine_dir
from .drivers import create_driver
from .metrics import COLLECTOR_UP
from .mqtt import NullPublisher, Publisher
from .runner import MachineRunner
from .storage import Storage

log = logging.getLogger("collector")


async def amain() -> int:
    config_dir = Path(os.environ.get("MACHINE_CONFIG_DIR", "/config"))
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        log.error("DATABASE_URL is required")
        return 2
    collector_id = os.environ.get("COLLECTOR_ID") or socket.gethostname()
    only = {c.strip().upper() for c in os.environ.get("MACHINES", "").split(",") if c.strip()}

    try:
        machines = load_machine_dir(config_dir)
    except ConfigError as e:
        log.error("machine configuration invalid: %s", e)
        return 2
    log.info("loaded %d machine configs: %s", len(machines),
             ", ".join(f"{m.code}({'on' if m.enabled else 'off'})" for m in machines))

    start_http_server(int(os.environ.get("METRICS_PORT", "9108")))
    COLLECTOR_UP.set(1)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    storage = Storage(dsn)
    await storage.connect(stop)
    if stop.is_set():
        return 0

    mqtt_host = os.environ.get("MQTT_HOST", "")
    publisher = (Publisher(mqtt_host, int(os.environ.get("MQTT_PORT", "1883")), os.environ.get("MQTT_USER"),
                           os.environ.get("MQTT_PASSWORD"), f"collector-{collector_id}")
                 if mqtt_host else NullPublisher())
    publisher.start()

    runners: list[MachineRunner] = []
    for cfg in machines:
        mid = await storage.sync_machine(cfg)
        if not cfg.enabled or (only and cfg.code not in only):
            log.info("[%s] registered, not polled (%s)", cfg.code, "disabled" if not cfg.enabled else "filtered")
            continue
        driver = create_driver(cfg.driver, cfg.host, cfg.port, cfg.timeout_ms, cfg.options)
        runners.append(MachineRunner(cfg, mid, driver, storage, publisher, collector_id,
                                     await storage.last_counters(mid)))

    writer = asyncio.create_task(storage.writer(stop))
    await asyncio.gather(*(r.run(stop) for r in runners), stop.wait())
    log.info("stopping")
    await writer
    publisher.stop()
    await storage.close()
    COLLECTOR_UP.set(0)
    return 0


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per PLC read is noise
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
