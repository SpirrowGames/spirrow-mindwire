# Tier-C replay evaluation — Jev

## (A) / (B) — read this first (msg-4219 §0, msg-4221)

- msg-3630's 179 rows cannot be reproduced (no per-row list was published); this evaluation is not compared with that breakdown (msg-4229 §1).
- harvested population (gate only, no Jev call): 0 rows; **in the grey zone: 0**
- rows replayed: body 18, supplement 2
- **(B) grey zone (ADMIT_UNSURE / second_time_force_admit): 0/20 = 0.0% of replayed rows** — under today's D18 gating only these reach Jev's verdict; (B) = this share x (A).
- rows the live hook's entry check would admit (proposer-authored `NEXT: human`): 18/18 = 100.0%; of those in the grey zone: 0
- The gate is **today's** `compute_gate_result` (allowed labels, label migration), not the rules in force when each message was posted (msg-4226).
- Changing D18 or the allowed labels would be an amendment grounded in ADR-2026-05-23-07 (msg-4224); that decision is out of scope here.
- rosters from today's config (`current_fallback`): 1
- thresholds (fixed in advance, RUBRIC): genuine_min=0.6 genuine_max=0.4 spurious_min=0.6

## (A) Jev's discrimination — body set (headline)

### truth = consensus
- reduction (spurious → LIKELY_NOT): 0/10 = 0.0%
-   of which fired_reason=answerable_from_thread (§4.5 bounce): 0/10 = 0.0%
- recall genuine (reaches human): 4/4 = 100.0%
- recall genuine-merge (reaches human): 2/2 = 100.0%
- recall genuine-action (reaches human): 2/2 = 100.0%
- ambiguous (excluded): 0
- unlabelled (excluded): 0

### truth = naysayer-tier
- reduction (spurious → LIKELY_NOT): 0/10 = 0.0%
-   of which fired_reason=answerable_from_thread (§4.5 bounce): 0/10 = 0.0%
- recall genuine (reaches human): 4/4 = 100.0%
- recall genuine-merge (reaches human): 2/2 = 100.0%
- recall genuine-action (reaches human): 2/2 = 100.0%
- ambiguous (excluded): 0
- unlabelled (excluded): 0

### truth = frontier-tier
- reduction (spurious → LIKELY_NOT): 0/10 = 0.0%
-   of which fired_reason=answerable_from_thread (§4.5 bounce): 0/10 = 0.0%
- recall genuine (reaches human): 4/4 = 100.0%
- recall genuine-merge (reaches human): 2/2 = 100.0%
- recall genuine-action (reaches human): 2/2 = 100.0%
- ambiguous (excluded): 0
- unlabelled (excluded): 0

- Cohen's κ (naysayer-tier vs frontier-tier): 0.677 over 209

## Supplement set (not in the headline)

- reduction (spurious → LIKELY_NOT): n/a (0)
-   of which fired_reason=answerable_from_thread (§4.5 bounce): n/a (0)
- recall genuine (reaches human): 2/2 = 100.0%
- recall genuine-merge (reaches human): n/a (0)
- recall genuine-action (reaches human): n/a (0)
- ambiguous (excluded): 0
- unlabelled (excluded): 0

### Confusion — body (consensus truth)

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 4 | 0 | 0 | 0 |
| genuine-merge | 2 | 0 | 0 | 0 |
| genuine-action | 1 | 1 | 0 | 0 |
| spurious | 9 | 1 | 0 | 0 |

### Confusion — supplement (consensus truth)

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 2 | 0 | 0 | 0 |

## Misses — genuine judged LIKELY_NOT (one by one, body and supplement)

- none

## Breakdown by labelled category (consensus truth)

### COST [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 1 | 0 | 0 | 0 |

### GOAL [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 2 | 0 | 0 | 0 |

### GOAL [supplement]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 2 | 0 | 0 | 0 |

### HUMAN_ONLY_ACTION [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine-action | 1 | 1 | 0 | 0 |

### IMPL [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| spurious | 5 | 0 | 0 | 0 |

### IRREVERSIBLE [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 1 | 0 | 0 | 0 |

### MERGE [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine-merge | 2 | 0 | 0 | 0 |

### ROUTING_ARTIFACT [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| spurious | 4 | 1 | 0 | 0 |

## Breakdown by gate result

### ADMIT(goal)

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 2 | 0 | 0 | 0 |

### ADMIT(merge-protected)

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine-merge | 1 | 0 | 0 | 0 |

### BOUNCED(no-label)

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 4 | 0 | 0 | 0 |
| genuine-merge | 1 | 0 | 0 | 0 |
| genuine-action | 1 | 1 | 0 | 0 |
| spurious | 8 | 1 | 0 | 0 |

### BOUNCED(other-not-admitted)

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| spurious | 1 | 0 | 0 | 0 |

## Calibration (body + supplement; genuine rows are too few in body alone)

| spurious max | n | actually spurious |
|---|---|---|
| [0.0,0.2) | 0 | n/a (0) |
| [0.2,0.4) | 0 | n/a (0) |
| [0.4,0.6) | 19 | 10/19 = 52.6% |
| [0.6,0.8) | 1 | 0/1 = 0.0% |
| [0.8,1.0] | 0 | n/a (0) |

- AUC of genuine-sum (genuine* vs spurious): 0.595

### Threshold sweep — reference only, never a headline (RUBRIC), body set

| genuine_min | spurious_min | reduction | recall genuine |
|---|---|---|---|
| 0.6 | 0.4 | 0/10 = 0.0% | 4/4 = 100.0% |
| 0.6 | 0.5 | 0/10 = 0.0% | 4/4 = 100.0% |
| 0.6 | 0.6 | 0/10 = 0.0% | 4/4 = 100.0% |
| 0.6 | 0.7 | 0/10 = 0.0% | 4/4 = 100.0% |
| 0.6 | 0.8 | 0/10 = 0.0% | 4/4 = 100.0% |
| 0.9 | 0.4 | 1/10 = 10.0% | 4/4 = 100.0% |
| 0.9 | 0.5 | 1/10 = 10.0% | 4/4 = 100.0% |
| 0.9 | 0.6 | 0/10 = 0.0% | 4/4 = 100.0% |
| 0.9 | 0.7 | 0/10 = 0.0% | 4/4 = 100.0% |
| 0.9 | 0.8 | 0/10 = 0.0% | 4/4 = 100.0% |
| 1.2 | 0.4 | 2/10 = 20.0% | 4/4 = 100.0% |
| 1.2 | 0.5 | 2/10 = 20.0% | 4/4 = 100.0% |
| 1.2 | 0.6 | 0/10 = 0.0% | 4/4 = 100.0% |
| 1.2 | 0.7 | 0/10 = 0.0% | 4/4 = 100.0% |
| 1.2 | 0.8 | 0/10 = 0.0% | 4/4 = 100.0% |
| 1.5 | 0.4 | 7/10 = 70.0% | 3/4 = 75.0% |
| 1.5 | 0.5 | 5/10 = 50.0% | 3/4 = 75.0% |
| 1.5 | 0.6 | 0/10 = 0.0% | 3/4 = 75.0% |
| 1.5 | 0.7 | 0/10 = 0.0% | 4/4 = 100.0% |
| 1.5 | 0.8 | 0/10 = 0.0% | 4/4 = 100.0% |
| 1.8 | 0.4 | 9/10 = 90.0% | 1/4 = 25.0% |
| 1.8 | 0.5 | 6/10 = 60.0% | 3/4 = 75.0% |
| 1.8 | 0.6 | 0/10 = 0.0% | 3/4 = 75.0% |
| 1.8 | 0.7 | 0/10 = 0.0% | 4/4 = 100.0% |
| 1.8 | 0.8 | 0/10 = 0.0% | 4/4 = 100.0% |

## Grey zone only (n = 0 of replayed rows)

- no rows — nothing to measure; see (B) above.

## Operations

- outcome: {'evaluated': 20}
- error rate: 0/20 = 0.0%
- latency ms p50=259 p95=301
- server verdict is scope=out_of_gate (UNSURE by construction) on 20 rows; the recomputed verdict above is what (A) reads.
- cost per call: not in the replay record — read from Lexora usage.


---

## Limit: only 10 of these 20 rows are real escalations (hand-written, msg-4300 §1 / msg-4376 §1-3)

The pilot rows were drawn from the same 209-row fixture whose population turned out to be wrong (see `report.md`, "The target population was wrong"). Checked against `corrections/2026-09-28-roster-selection.json`, **10 of the 20** pilot rows are real escalations; the other 10 are ordinary role handoffs.

- **Still holds:** what the pilot was run to establish about the call itself: the request/response contract, 0/20 errors, complete `raw_answers`, and latency (p50 259 ms / p95 301 ms). These depend on the call, not on which rows were sent.
- **Does not hold:** the verdict tendencies above (reduction, recall, breakdowns, calibration). They were measured on a mixed set; read `report.md` (66 real escalations) instead.
- **Per-call cost was never measured**, in the pilot or later: `/v1/decide` writes no row to Lexora's cost ledger (`report.md`, Limits). The line "cost per call: … read from Lexora usage" above describes where the figure was meant to come from, not a figure that exists.
- The Operations line above says "server verdict"; the verdict it describes is attached on the mindwire side (`adapters/decider_lexora.py`), as `report.md` now says. This file was generated by the older script and is left as generated.
