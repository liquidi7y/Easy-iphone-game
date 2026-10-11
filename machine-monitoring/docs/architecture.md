# Architecture (v0.1)

## 1. Principles

1. **Read-only by default.** No PLC writes, START/STOP, output forcing, register
   changes or program changes. Enforced in three places: the driver base class
   (no write methods can be defined), the machine schema (`read_only: true` is a
   constant), and the database (`CHECK (read_only)` on controllers and devices).
2. **Machine independence.** The platform only ever *reads*. If Proxmox, Docker,
   the collector, the database, the uplink or the ERP fail, the machine runs on.
3. **Network isolation** (Phase 13): OT VLAN, default-deny firewall, collector → PLC
   only on the protocol port.
4. **No PLC logic in the core.** Addresses live in `machine-config/*.yaml`.
5. **Normalized model.** ERP and dashboards see `machine.running`, `counter.total`,
   `RUNNING`… never `M471` or `DB10.DBX2.0`.
6. **Raw + normalized.** Raw device values are kept (30 days) for troubleshooting.
   Normalized values (400 days), events and counters (indefinitely) are kept longer.
7. **Configuration as code.** Infrastructure, machine maps, migrations, dashboards in Git.
8. **Idempotent deployment.** Re-running bootstrap/provision never destroys data.
9. **Secrets outside Git.** Generated into `deploy/docker/.env` (mode 600).
10. **Backups before cleverness.** Nightly `pg_dump`, a backup before every deploy that
    touches an existing database, and a scripted restore test.

## 2. Data flow

```
 Machine PLC / controller                 (unchanged; never written to)
        │  vendor protocol, read-only
        ▼
 ┌───────────────────────── collector ──────────────────────────┐
 │ driver (simulator | mitsubishi_slmp | …)                      │
 │   └─ poll groups fast/normal/slow ─ contiguous block reads    │
 │ decode (bool/int16/uint16/int32/uint32/float32, scale)        │
 │ SignalTable: value + quality (good/stale/bad) per signal      │
 │ evaluate(): normalized fields + canonical state (rules)       │
 │ EventTracker: state changes, RUN/FAULT/ESTOP events, counters,│
 │               downtime & fault periods (idempotent ops)       │
 └───────┬───────────────────────────────┬──────────────────────┘
         │ bounded queue, batch writer   │ paho (own thread)
         ▼                               ▼
 PostgreSQL + TimescaleDB          Mosquitto  factory/{site}/{machine}/…
         │
         ├── API (FastAPI, /api/v1, OpenAPI)  ── ERP (order sessions; never PLC)
         └── Grafana (read-only DB role)
```

## 3. Collector

**Drivers** implement `connect / health / read_bits / read_words / read_block /
disconnect`. Defining any `write*`, `force*`, `set_*`, `start_*`, `stop_*`,
`upload*` or `download*` method raises `TypeError` at import time. Radix rules
(e.g. Mitsubishi X/Y hexadecimal on iQ-R, octal on iQ-F) belong to the driver.

| Driver | Status |
|---|---|
| `simulator` | done: HTTP read API of `services/simulator` |
| `mitsubishi_slmp` | placeholder (M4): 3E binary batch read 0x0401 only |
| `modbus_tcp`, `opcua`, `siemens_s7` | later, with the machines that need them |

**Poll groups.** `fast` (default 500 ms), `normal` (1 s), `slow` (10 s), set per
machine. Signals in the same group and device are merged into contiguous blocks
(gaps ≤ 16 points, ≤ 480 words). Each group runs on its own schedule. Overruns
do not trigger catch-up bursts. Three consecutive failed cycles reset the
connection. Reconnects back off from 1 s up to 30 s.

**Quality.** Each signal is `good`, `stale` (last good read older than
max(2 s, 3 × poll interval)) or `bad` (communication failure / never read). A
normalized value derived from an `unverified` signal is `uncertain`.

**State machine.** Canonical states: `OFF READY RUNNING IDLE STOPPED FAULT
E_STOP UNKNOWN`. Each machine defines ordered rules (first match wins) and a
default. If *any* signal referenced by *any* rule is not fresh, the state is
`UNKNOWN`. For example, an unknown E-stop bit means the platform cannot claim
`RUNNING`. Without rules, the state is always `UNKNOWN`.

**Events** (v0.1 in `collector/events.py`, designed to move to `event-engine`):
`RUN_START`, `RUN_STOP`, `FAULT_START`, `FAULT_CLEAR`, `FAULT_CODE_CHANGE`,
`ESTOP_START`, `ESTOP_CLEAR`, `COUNTER_RESET`, `MONITORING_LOST`,
`MONITORING_RESTORED`. A downtime period opens on any known non-RUNNING state
and closes on RUNNING. `UNKNOWN` (a monitoring gap) neither opens nor closes
downtime. Downtime and fault writes are *ensure/close* operations, so a
collector restart in the middle of a fault does not split it. Counter deltas
survive restarts (the last value is loaded from the DB) and resets are
detected (`new < old`).

**Storage.** Raw samples are written on change plus a 60 s heartbeat.
Normalized samples follow the same rule. `machine_current_state` is upserted
every second, and a `collector_health` row is written every 10 s. All writes go
through one bounded queue (200 k items). A database outage buffers and retries;
it never blocks polling.

**Metrics** (`:9108/metrics`): `collector_up`, `plc_connection_up`,
`poll_duration_ms`, `poll_errors_total`, `last_successful_read`,
`mqtt_connected`, `db_write_latency`, `queue_depth`, `db_dropped_total`,
`machine_state`.

## 4. Monitoring failure vs. machine failure

| Situation | Machine state | Indicators |
|---|---|---|
| PLC unreachable / reads failing | `UNKNOWN` (quality `bad`) | `plc_connected=false`, Grafana "PLC UNREACHABLE" |
| Reads succeed but too old | `UNKNOWN` (quality `stale`) | `last_successful_read` age grows |
| Collector down | `UNKNOWN` via API/Grafana (no update for > 15 s) | "COLLECTOR DOWN", `collector_reporting=false` |
| Database down | API `503` on `/health`; collector keeps polling and buffering | `queue_depth` grows |
| Machine really stopped | `STOPPED`/`READY`/… from verified signals | link `CONNECTED` |

## 5. Database

Migration `0001_init.sql`. Main tables:

| Table | Content | Retention |
|---|---|---|
| `sites`, `machines`, `controllers`, `machine_devices`, `device_mappings` | registry, synced from YAML at collector start (devices removed from YAML are kept with `active=false`) | — |
| `raw_samples` (hypertable) | vendor-level values + quality | 30 days |
| `normalized_samples` (hypertable) | `machine.speed`, `counter.total`, … | 400 days |
| `production_counters` (hypertable) | counter changes with delta | indefinite |
| `collector_health` (hypertable) | connection, poll time, errors | 30 days |
| `machine_state_events`, `machine_events` | state changes (with input snapshot), events | indefinite |
| `downtime_events`, `fault_events` | periods; one open per machine (partial unique index) | indefinite |
| `machine_current_state` | one row per machine for API/dashboards | — |
| `orders`, `machine_order_sessions` | ERP linkage; one open session per machine | indefinite |

Migrations run once each (`schema_migrations`), one transaction per file, from
the `migrate` service before the collector/API start.

## 6. API (`/api/v1`)

`GET health, machines, machines/{id|code}, …/state, …/signals, …/events,
…/production, …/downtime`; `POST …/order-session`, `…/order-session/close`.
The POST endpoints write to the monitoring database only. `from`/`to` query
parameters (ISO 8601) default to the last 24 h. OpenAPI: `/api/v1/openapi.json`,
UI: `/api/v1/docs`.

## 7. MQTT

`factory/{site}/{machine}/state` (retained, on change + every 5 s), `signals`
(≤ 1/s), `counter` (retained, on change), `event` (QoS 1), `health` (retained,
10 s). Payload: `timestamp`, `machine_id`, `sequence`, `quality`, `source`,
`data`. The broker requires a password and is not published outside Docker.

## 8. Deployment

`deploy/bootstrap.sh` (Proxmox host) runs: preflight (PVE ≥ 8, RAM, storage,
bridge, ID/name conflicts, image reachability) → SSH key → Debian cloud image
(SHA-512 verified, cached) → cloud-init vendor snippet (installs the guest
agent) → VM create/reuse → wait for IP and SSH → copy repo (secrets excluded)
→ `deploy/vm/provision.sh`.

`provision.sh` (VM) handles: data disk (formatted only if blank, mounted at
`/srv`, `nofail`) → security updates → SSH keys-only (only if the admin key is
present) → Docker with data root `/srv/docker` → `.env` (secrets generated once)
→ backup if a database exists → `machine-monitoring.service` (start at boot) +
nightly backup timer → build and start → health check including SIM01
end-to-end.

### What bootstrap must NOT do (and does not)

Discover PLCs or write configuration to them · change PLC IPs · upload or download
PLC programs · force outputs · start/stop machines · disable safety systems · add
routes to PLC networks · overwrite an existing database without a backup · delete
a VM because its ID or name conflicts.

## 9. Adding a machine

1. Network survey (copy `docs/network/rdc53-network.md`).
2. Identify the protocol; implement a driver if it is new (read-only interface, simulator fixture).
3. Read-only connectivity test (`tests/hardware`, never in CI).
4. `machine-config/<code>.yaml`: all signals `unverified` and the machine disabled at first.
5. Device discovery (`docs/machines/rdc53/README.md` procedure) → mark signals verified with evidence.
6. `normalized` mappings and `state` rules.
7. Enable, restart the collector. Dashboards and API pick it up automatically.

If this requires changing the core, the architecture needs fixing. Do not copy the platform.

## 10. Roadmap

| Milestone | Status |
|---|---|
| M0 docs | this document, README, RDC-53 discovery + network templates |
| M1 infrastructure | bootstrap.sh + provision.sh (needs a first run on the real Proxmox host) |
| M2 core platform | done: TimescaleDB, MQTT, API, Grafana, health checks |
| M3 simulator | done: end-to-end in Grafana and the API |
| M4 Mitsubishi collector | next: SLMP 3E binary batch read, radix per CPU family, against a bench PLC first |
| M5 RDC-53 pilot | Phase 0 survey, first read, 20–30 signals, state, counter, one shift |
| M6 ERP bridge | order session API exists; ERP-side integration pending |
| M7 hardening | OT VLAN/firewall, off-host encrypted backups, alerting (Prometheus/Alertmanager), TLS |
| M8 second machine | validates the generic design |
