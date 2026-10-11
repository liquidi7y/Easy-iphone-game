-- 0001_init.sql - core schema for the Nueva machine monitoring platform.
-- Applied by scripts/migrate.sh inside a single transaction.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------------
-- Registry (written by the collector from machine-config/*.yaml)
-- ---------------------------------------------------------------------------
CREATE TABLE sites (
    id          serial PRIMARY KEY,
    code        text NOT NULL UNIQUE,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE machines (
    id           serial PRIMARY KEY,
    site_id      integer NOT NULL REFERENCES sites(id),
    code         text NOT NULL UNIQUE,
    name         text NOT NULL,
    vendor       text,
    model        text,
    enabled      boolean NOT NULL DEFAULT true,
    simulated    boolean NOT NULL DEFAULT false,
    config_hash  text,
    config       jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE controllers (
    id          serial PRIMARY KEY,
    machine_id  integer NOT NULL UNIQUE REFERENCES machines(id) ON DELETE CASCADE,
    vendor      text,
    model       text,
    driver      text NOT NULL,
    host        text,
    port        integer,
    -- Monitoring v1 is read-only; the database refuses anything else.
    read_only   boolean NOT NULL DEFAULT true CHECK (read_only),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE machine_devices (
    id            serial PRIMARY KEY,
    machine_id    integer NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    address       text NOT NULL,
    name          text NOT NULL,
    protocol      text NOT NULL,
    data_type     text NOT NULL,
    poll_group    text NOT NULL,
    read_only     boolean NOT NULL DEFAULT true CHECK (read_only),
    status        text NOT NULL CHECK (status IN ('verified', 'unverified')),
    confidence    text,
    normal_state  text,
    unit          text,
    description   text,
    evidence      text,
    active        boolean NOT NULL DEFAULT true,  -- false once removed from the YAML (history kept)
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (machine_id, address),
    UNIQUE (machine_id, name)
);

CREATE TABLE device_mappings (
    id               serial PRIMARY KEY,
    machine_id       integer NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    normalized_name  text NOT NULL,
    source_signal    text NOT NULL,
    source_address   text NOT NULL,
    transform        jsonb NOT NULL DEFAULT '{}'::jsonb,
    unit             text,
    normal_state     text,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (machine_id, normalized_name)
);

-- ---------------------------------------------------------------------------
-- Time series
-- ---------------------------------------------------------------------------
-- Raw device observations: written on change + periodic heartbeat. Short retention.
CREATE TABLE raw_samples (
    time        timestamptz NOT NULL,
    machine_id  integer NOT NULL,
    signal      text NOT NULL,
    address     text NOT NULL,
    value       double precision,
    quality     text NOT NULL
);
SELECT create_hypertable('raw_samples', 'time', chunk_time_interval => interval '1 day');
CREATE INDEX raw_samples_machine_signal_time ON raw_samples (machine_id, signal, time DESC);
SELECT add_retention_policy('raw_samples', interval '30 days');

-- Normalized values (machine.speed, counter.total, ...): vendor independent.
CREATE TABLE normalized_samples (
    time        timestamptz NOT NULL,
    machine_id  integer NOT NULL,
    name        text NOT NULL,
    value       double precision,
    quality     text NOT NULL
);
SELECT create_hypertable('normalized_samples', 'time', chunk_time_interval => interval '7 days');
CREATE INDEX normalized_samples_machine_name_time ON normalized_samples (machine_id, name, time DESC);
SELECT add_retention_policy('normalized_samples', interval '400 days');

-- Counter changes. Kept indefinitely (production record).
CREATE TABLE production_counters (
    time        timestamptz NOT NULL,
    machine_id  integer NOT NULL,
    counter     text NOT NULL,
    value       bigint NOT NULL,
    delta       bigint          -- NULL when the previous value is unknown
);
SELECT create_hypertable('production_counters', 'time', chunk_time_interval => interval '30 days');
CREATE INDEX production_counters_machine_counter_time ON production_counters (machine_id, counter, time DESC);

CREATE TABLE collector_health (
    time                  timestamptz NOT NULL,
    machine_id            integer NOT NULL,
    collector_id          text NOT NULL,
    plc_connected         boolean NOT NULL,
    poll_duration_ms      double precision,
    poll_errors_total     bigint NOT NULL DEFAULT 0,
    last_successful_read  timestamptz,
    details               jsonb
);
SELECT create_hypertable('collector_health', 'time', chunk_time_interval => interval '1 day');
CREATE INDEX collector_health_machine_time ON collector_health (machine_id, time DESC);
SELECT add_retention_policy('collector_health', interval '30 days');

-- ---------------------------------------------------------------------------
-- Events
-- ---------------------------------------------------------------------------
CREATE TABLE machine_state_events (
    id               bigserial PRIMARY KEY,
    machine_id       integer NOT NULL REFERENCES machines(id),
    time             timestamptz NOT NULL,
    previous_state   text,
    new_state        text NOT NULL CHECK (new_state IN ('OFF','READY','RUNNING','IDLE','STOPPED','FAULT','E_STOP','UNKNOWN')),
    reason           text,
    quality          text NOT NULL,
    source_snapshot  jsonb
);
CREATE INDEX machine_state_events_machine_time ON machine_state_events (machine_id, time DESC);

CREATE TABLE machine_events (
    id          bigserial PRIMARY KEY,
    machine_id  integer NOT NULL REFERENCES machines(id),
    time        timestamptz NOT NULL,
    event_type  text NOT NULL,
    data        jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX machine_events_machine_time ON machine_events (machine_id, time DESC);

CREATE TABLE downtime_events (
    id                bigserial PRIMARY KEY,
    machine_id        integer NOT NULL REFERENCES machines(id),
    started_at        timestamptz NOT NULL,
    ended_at          timestamptz,
    state             text NOT NULL,
    reason            text,
    operator_reason   text,          -- filled in later by operators / ERP
    order_session_id  integer
);
CREATE UNIQUE INDEX downtime_one_open_per_machine ON downtime_events (machine_id) WHERE ended_at IS NULL;
CREATE INDEX downtime_events_machine_started ON downtime_events (machine_id, started_at DESC);

CREATE TABLE fault_events (
    id           bigserial PRIMARY KEY,
    machine_id   integer NOT NULL REFERENCES machines(id),
    started_at   timestamptz NOT NULL,
    ended_at     timestamptz,
    fault_code   integer,
    description  text
);
CREATE UNIQUE INDEX fault_one_open_per_machine ON fault_events (machine_id) WHERE ended_at IS NULL;
CREATE INDEX fault_events_machine_started ON fault_events (machine_id, started_at DESC);

-- Latest snapshot per machine (one row each), for the API and dashboards.
CREATE TABLE machine_current_state (
    machine_id            integer PRIMARY KEY REFERENCES machines(id) ON DELETE CASCADE,
    state                 text NOT NULL,
    state_since           timestamptz NOT NULL,
    quality               text NOT NULL,
    reason                text,
    plc_connected         boolean NOT NULL DEFAULT false,
    last_successful_read  timestamptz,
    normalized            jsonb NOT NULL DEFAULT '{}'::jsonb,
    signals               jsonb NOT NULL DEFAULT '{}'::jsonb,
    collector_id          text,
    updated_at            timestamptz NOT NULL
);

-- ---------------------------------------------------------------------------
-- ERP linkage (monitoring database only - never forwarded to a PLC)
-- ---------------------------------------------------------------------------
CREATE TABLE orders (
    id               serial PRIMARY KEY,
    order_number     text NOT NULL UNIQUE,
    product          text,
    target_quantity  integer CHECK (target_quantity IS NULL OR target_quantity >= 0),
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE machine_order_sessions (
    id             serial PRIMARY KEY,
    machine_id     integer NOT NULL REFERENCES machines(id),
    order_id       integer NOT NULL REFERENCES orders(id),
    operator       text,
    shift          text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    ended_at       timestamptz,
    start_counter  bigint,
    end_counter    bigint,
    produced       bigint,
    notes          text
);
CREATE UNIQUE INDEX order_session_one_open_per_machine ON machine_order_sessions (machine_id) WHERE ended_at IS NULL;

ALTER TABLE downtime_events
    ADD CONSTRAINT downtime_order_session_fk FOREIGN KEY (order_session_id) REFERENCES machine_order_sessions(id);
