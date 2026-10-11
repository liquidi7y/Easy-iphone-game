"""Prometheus metrics for monitoring the monitor (Phase 14).

Collector metrics describe the *monitoring path*. A stale PLC shows up as
plc_connection_up=0 / old last_successful_read while collector_up stays 1,
which is how monitoring failure is distinguished from machine failure.
"""

from prometheus_client import Counter, Gauge, Histogram

COLLECTOR_UP = Gauge("collector_up", "Collector process is running")
PLC_UP = Gauge("plc_connection_up", "Controller connection is healthy", ["machine"])
POLL_DURATION = Histogram("poll_duration_ms", "Duration of one poll-group cycle", ["machine", "group"],
                          buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000))
POLL_ERRORS = Counter("poll_errors_total", "Failed poll cycles", ["machine", "group"])
LAST_READ = Gauge("last_successful_read", "Unix time of the last successful controller read", ["machine"])
MQTT_CONNECTED = Gauge("mqtt_connected", "Collector is connected to the MQTT broker")
DB_WRITE_LATENCY = Histogram("db_write_latency", "Database batch write latency (ms)",
                             buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 5000))
QUEUE_DEPTH = Gauge("queue_depth", "Items waiting to be written to the database")
DB_DROPPED = Counter("db_dropped_total", "Items dropped because the write queue was full")
MACHINE_STATE = Gauge("machine_state", "1 for the machine's current canonical state", ["machine", "state"])
