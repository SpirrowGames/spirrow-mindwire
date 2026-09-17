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

`expected_verdict = "not-representable"` is a first-class outcome. If a
future operator-observed incident carries a shape the current schema cannot
express (a purely hypothetical example: an incident whose entire signal is
"the sweep never ran on this repo in the last N ticks", a signal `PrState`
/ `QuarantineState` / `ThreadState` do not currently carry), the fixture
records `"expected_verdict": "not-representable"` and a `not_representable_
reason` naming the missing schema field. That "cannot be represented" IS the
finding, and is preserved here rather than silently dropped — it is msg-2692
§4-5's "fixture 化できない incident は §3-1 に対する発見として記録" clause
made mechanical.

**None of the four seed fixtures (M-1..M-4) are currently not-representable.**
All four are shipped with `"expected_verdict": "stall"` and fire against the
production `stalled()` predicate. M-3 in particular — the CONFLICTING PR
with no CI run — is representable via `verdict_recorded_as_indefinite_input:
true` + `merge_state_is_executable: false`; the "no CI run" input shape is
captured indirectly through the disposition it forces (see the fixture's
`notes` field). A prior draft of this README used M-3 as the not-representable
example, which contradicted the fixture on disk and misled readers about the
schema's capabilities — corrected in PR-gate round 6 (msg-2708).

**M-5 / M-6 / M-7 are the corpus's first not-representable fixtures**
(added under Bohr msg-2833 §3 D-12″). Their `not_representable_reason`
fields carry the three-layer residual (schema / granularity / consumer)
established by Einstein E-9 / E-10 in msg-2749, and their absence from
predicate coverage is declared machine-readably in each fixture's own
`residual` object (`receipt_thread` + `missing_layers`). The backtest holds
the biconditional "a fixture carries a `residual` IFF its `expected_verdict`
is `not-representable`", so the record cannot be omitted when the fixture is
added, and must be deleted the day the fixture becomes representable.

An earlier form declared this in a per-KIND `_KNOWN_UNCOVERED_KINDS`
allowlist in the backtest suite. That was replaced (Bohr msg-2853 §2) because
M-5 and M-6 share `"kind": "thread"`: a single entry served both, described
only M-5, and routed M-6 to the wrong receipt thread. Worse, making M-5
representable forced the deletion of that shared entry, silently absorbing
M-6's still-open gap while the suite went green. Keyed per incident, that is
unrepresentable — the two records are separate objects in separate files.

Each fixture is also paired with a running "collision pin" test that
constructs the incident and its valid-wait counterexample and asserts the v1
schema cannot distinguish them. The pin's trigger is the SCHEMA (the
`ThreadState` field set for M-5 / M-6, the `UnitKind` registry for M-7), not
the equivalence of two constructed instances — an instance comparison is
blind to an optional field with a default, which is how a trigger claimed by
two docstrings measured green across the whole suite at e1de998 (PR-gate
msg-2851 objection 1).

Fixture reason fields are restricted to **structural / mechanical residual
descriptions** (Einstein msg-2832 ADVISORY, Bohr msg-2833 §2). Project-
management state such as "no design thread owns this fix yet" is NEVER
written into a fixture — it belongs on the chatroom design thread only,
where it does not go stale when the code has not changed. The `residual`
object follows the same rule: it names the receipt thread a schema fix would
arrive on (a mechanical routing dependency), not the ownership state of the
fix itself.

## What the backtest asserts

The backtest lives in `tests/test_stall_ledger_incident_backtest.py`. It
loads every JSON in this directory and asserts one of:

  * `expected_verdict == "stall"` → the classifier / predicate returns a
    stall for `predicate_input`.
  * `expected_verdict == "not-stall"` → the classifier returns not-stall.
  * `expected_verdict == "not-representable"` → the backtest asserts nothing
    about the classifier's output (there is no input for it to consume), but
    the fixture is NOT merely documentation: it must carry a
    `not_representable_reason` and a well-formed `residual`, and its paired
    collision pin runs on every suite invocation and reds when the schema
    grows the layer the fixture is waiting on.

The rule for adding a new fixture: capture what you actually saw, not what
you think the detector should have seen. A fixture assembled from memory
carries the same failure mode this thread was created to eliminate.
