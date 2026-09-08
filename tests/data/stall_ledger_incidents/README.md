# stall_ledger incident fixtures — R-2b backtest corpus

Per Bohr msg-2692 §4-5 and the monotonic-fixture obligation
(`spec/process/obligations.yaml` → `OBL-STALL-DETECTOR-MONOTONIC-FIXTURES`).

Each JSON file in this directory records ONE incident observed by operator
lane on `T-stalled-pr-has-no-detector`. The msg-2354 §1 opening measurement
seeded this corpus with four incidents (M-1 through M-4); every subsequent
operator-observed stall that could not be diagnosed by the detector at the
time must be added here as a new file.

## File shape

```jsonc
{
  "id": "M-1",                           // stable, incident-specific slug
  "observed_by": "operator",             // "operator" or "detector"
  "observed_at": "2026-08-30T07:59Z",    // ISO-8601, UTC
  "spec_thread": "T-stalled-pr-has-no-detector",
  "spec_msg_ids": ["msg-2354"],          // where the incident was recorded
  "provenance": {
    "source": "conductor log",           // where the ground-truth was captured
    "captured_at": "2026-09-08T00:00Z",
    "captured_by": "operator lane, msg-2354 §1"
  },
  "predicate_input": { /* the shape the detector's ingest was expected to see */ },
  "expected_verdict": "stall",           // "stall" | "not-stall" | "not-representable"
  "not_representable_reason": null,      // required when expected_verdict == "not-representable"
  "notes": "..."
}
```

`expected_verdict = "not-representable"` is a first-class outcome. Some
incidents (e.g. M-3: CI runs are never generated because the PR is dirty)
require input shapes the current heartbeat schema does not carry. That
"cannot be represented" IS the finding, and is preserved here rather than
silently dropped — it is msg-2692 §4-5's "fixture 化できない incident は §3-1
に対する発見として記録" clause made mechanical.

## What the backtest asserts

The backtest lives in `tests/test_stall_ledger_incident_backtest.py`. It
loads every JSON in this directory and asserts one of:

  * `expected_verdict == "stall"` → the classifier / predicate returns a
    stall for `predicate_input`.
  * `expected_verdict == "not-stall"` → the classifier returns not-stall.
  * `expected_verdict == "not-representable"` → the reason is recorded and
    the test asserts nothing about the classifier's output (the fixture is
    a documentation artifact for the residual, not a mechanical check).

The rule for adding a new fixture: capture what you actually saw, not what
you think the detector should have seen. A fixture assembled from memory
carries the same failure mode this thread was created to eliminate.
