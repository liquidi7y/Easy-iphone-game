# event-engine

In v0.1 the event logic (RUN_START, RUN_STOP, FAULT_START/CLEAR, ESTOP,
COUNTER_RESET, MONITORING_LOST/RESTORED, downtime and fault periods) runs
inside the collector: `services/collector/collector/events.py`.

It is a pure function of normalized evaluations and emits idempotent
operations, so it can move here unchanged when a separate consumer is
needed (e.g. several collectors, or event rules that span machines). The
service would then subscribe to `factory/+/+/state` and
`factory/+/+/signals` instead of being called in-process.
