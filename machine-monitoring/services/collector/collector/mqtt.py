"""MQTT publisher (internal event bus).

Topics:  factory/{site}/{machine}/{state|signals|counter|event|health}
Payload: {"timestamp", "machine_id", "sequence", "quality", "source", "data"}

Publishing is fire-and-forget from the polling path's point of view:
paho runs its own network thread and reconnects automatically. If the
broker is down, messages are dropped (the database is the record).
"""

from __future__ import annotations

import itertools
import json
import logging
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt

from .metrics import MQTT_CONNECTED

log = logging.getLogger("collector.mqtt")


class Publisher:
    def __init__(self, host: str, port: int, username: str | None, password: str | None,
                 client_id: str) -> None:
        self.source = client_id
        self._seq = itertools.count(1)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(1, 30)
        self.host, self.port = host, port

    def start(self) -> None:
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.warning("MQTT connect refused: %s", reason_code)
            MQTT_CONNECTED.set(0)
        else:
            log.info("connected to MQTT %s:%s", self.host, self.port)
            MQTT_CONNECTED.set(1)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        MQTT_CONNECTED.set(0)
        if reason_code != 0:
            log.warning("MQTT disconnected: %s", reason_code)

    def publish(self, topic: str, machine_code: str, data: Any, quality: str,
                timestamp: float, retain: bool = False, qos: int = 0) -> None:
        payload = {
            "timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
            "machine_id": machine_code,
            "sequence": next(self._seq),
            "quality": quality,
            "source": self.source,
            "data": data,
        }
        self.client.publish(topic, json.dumps(payload, default=str), qos=qos, retain=retain)


class NullPublisher:
    """Used when MQTT is disabled (tests, local runs)."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def publish(self, *args: Any, **kwargs: Any) -> None: ...
