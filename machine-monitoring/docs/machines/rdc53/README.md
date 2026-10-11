# RDC-53 / STACKER - device discovery

Source PLC project: **assumed unavailable**. The device map in
`machine-config/rdc53.yaml` is built from evidence, never guessed.

## Evidence sources (in order of weight)

1. Electrical diagrams (I/O lists, terminal numbers, page references).
2. Live monitoring in GX Works3 (online, **monitor mode only**, no writes, no program download).
3. Controlled observation of normal production by an operator.
4. Read-only sampling by the collector (once M4 exists): raw samples with timestamps.
5. Timestamped operator annotations.

## Test procedure

Run each sequence with the machine's own operator, in normal production conditions.
Record wall-clock time (synced clock) at each step.

| Step | Action | What to look for |
|---|---|---|
| T0 | machine stopped | baseline of all candidate devices |
| T1 | press START | bits that rise on start command |
| T2 | feeder begins | feeder drive / sensor bits |
| T3 | normal production | counters incrementing, speed words |
| T4 | stack cycle | stacker cycle bits / counters |
| T5 | normal STOP | bits that fall, stop reason words |
| T6 | known non-dangerous interruption (e.g. paper out) | fault/warning bits, fault code words |

Never provoke an E-stop or a safety function to "see what changes". Never
infer a safety function from correlation alone. Safety-related points are
identified from the diagrams only, and `safety_related: true` is informational.

## Recording a point

Every discovered point gets all of these before it may become `verified`:

```yaml
- address: D120
  name: stack_counter
  type: uint16
  poll: fast
  status: unverified          # -> verified after review
  physical_function: sheets placed on the current stack
  evidence: "diagram p.14; GX Works3 2026-xx-xx T3 14:02:11-14:05:40 increments 1:1 with sheets"
  confidence: medium
  normal_state: null
  unit: sheets
  notes: "resets at T4 (stack change)"
```

A point becomes `verified` only when at least two independent sources agree
(e.g. a diagram plus an observation) and a second person has reviewed it.

## Initial candidates (from the specification, all unverified)

| Address | Name | Evidence |
|---|---|---|
| M461 | beater_forward_start | specification only |
| M462 | beater_reverse_start | specification only |
| M471 | belts1_forward_start | specification only |

## Exit criteria for M5

* PLC IP confirmed, first value read by the collector equals the GX Works3 value.
* 20–30 signals mapped with evidence.
* RUNNING / STOPPED / FAULT derivable from verified signals → `state` rules added.
* Production counter identified and checked against a manual count.
* One full shift recorded without collector errors affecting the machine.
