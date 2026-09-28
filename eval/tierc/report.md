# Tier-C replay evaluation — Jev

## (A) / (B) — read this first (msg-4219 §0, msg-4221)

- msg-3630's 179 rows cannot be reproduced (no per-row list was published); this evaluation is not compared with that breakdown (msg-4229 §1).
- harvested population (gate only, no Jev call): 862 rows; **in the grey zone: 0**
- rows replayed: body 195, supplement 14
- **(B) grey zone (ADMIT_UNSURE / second_time_force_admit): 0/209 = 0.0% of replayed rows** — under today's D18 gating only these reach Jev's verdict; (B) = this share x (A).
- rows the live hook's entry check would admit (proposer-authored `NEXT: human`): 195/195 = 100.0%; of those in the grey zone: 0
- The gate is **today's** `compute_gate_result` (allowed labels, label migration), not the rules in force when each message was posted (msg-4226).
- Changing D18 or the allowed labels would be an amendment grounded in ADR-2026-05-23-07 (msg-4224); that decision is out of scope here.
- rosters from today's config (`current_fallback`): 3
- thresholds (fixed in advance, RUBRIC): genuine_min=0.6 genuine_max=0.4 spurious_min=0.6

## (A) Jev's discrimination — body set (headline)

### truth = consensus
- reduction (spurious → LIKELY_NOT): 0/139 = 0.0%
-   of which fired_reason=answerable_from_thread (§4.5 bounce): 0/139 = 0.0%
- recall genuine (reaches human): 10/10 = 100.0%
- recall genuine-merge (reaches human): 12/12 = 100.0%
- recall genuine-action (reaches human): 5/5 = 100.0%
- ambiguous (excluded): 29
- unlabelled (excluded): 0

### truth = naysayer-tier
- reduction (spurious → LIKELY_NOT): 0/144 = 0.0%
-   of which fired_reason=answerable_from_thread (§4.5 bounce): 0/144 = 0.0%
- recall genuine (reaches human): 17/17 = 100.0%
- recall genuine-merge (reaches human): 16/16 = 100.0%
- recall genuine-action (reaches human): 7/7 = 100.0%
- ambiguous (excluded): 11
- unlabelled (excluded): 0

### truth = frontier-tier
- reduction (spurious → LIKELY_NOT): 0/154 = 0.0%
-   of which fired_reason=answerable_from_thread (§4.5 bounce): 0/154 = 0.0%
- recall genuine (reaches human): 12/12 = 100.0%
- recall genuine-merge (reaches human): 17/17 = 100.0%
- recall genuine-action (reaches human): 10/10 = 100.0%
- ambiguous (excluded): 2
- unlabelled (excluded): 0

- Cohen's κ (naysayer-tier vs frontier-tier): 0.677 over 209

## Supplement set (not in the headline)

- reduction (spurious → LIKELY_NOT): n/a (0)
-   of which fired_reason=answerable_from_thread (§4.5 bounce): n/a (0)
- recall genuine (reaches human): 7/7 = 100.0%
- recall genuine-merge (reaches human): 4/4 = 100.0%
- recall genuine-action (reaches human): n/a (0)
- ambiguous (excluded): 3
- unlabelled (excluded): 0

### Confusion — body (consensus truth)

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 10 | 0 | 0 | 0 |
| genuine-merge | 12 | 0 | 0 | 0 |
| genuine-action | 4 | 1 | 0 | 0 |
| spurious | 135 | 4 | 0 | 0 |
| ambiguous | 28 | 1 | 0 | 0 |

### Confusion — supplement (consensus truth)

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 7 | 0 | 0 | 0 |
| genuine-merge | 4 | 0 | 0 | 0 |
| ambiguous | 3 | 0 | 0 | 0 |

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
| genuine | 8 | 0 | 0 | 0 |
| ambiguous | 6 | 1 | 0 | 0 |

### GOAL [supplement]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 7 | 0 | 0 | 0 |
| ambiguous | 1 | 0 | 0 | 0 |

### HUMAN_ONLY_ACTION [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine-action | 4 | 1 | 0 | 0 |
| spurious | 1 | 0 | 0 | 0 |
| ambiguous | 2 | 0 | 0 | 0 |

### IMPL [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| spurious | 43 | 0 | 0 | 0 |
| ambiguous | 1 | 0 | 0 | 0 |

### IRREVERSIBLE [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 1 | 0 | 0 | 0 |

### MERGE [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine-merge | 12 | 0 | 0 | 0 |
| ambiguous | 4 | 0 | 0 | 0 |

### MERGE [supplement]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine-merge | 4 | 0 | 0 | 0 |
| ambiguous | 2 | 0 | 0 | 0 |

### OTHER [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| ambiguous | 11 | 0 | 0 | 0 |

### ROUTING_ARTIFACT [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| spurious | 91 | 4 | 0 | 0 |
| ambiguous | 4 | 0 | 0 | 0 |

## Breakdown by gate result

### ADMIT(goal)

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 10 | 0 | 0 | 0 |
| ambiguous | 1 | 0 | 0 | 0 |

### ADMIT(merge-protected)

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine-merge | 9 | 0 | 0 | 0 |
| ambiguous | 2 | 0 | 0 | 0 |

### BOUNCED(no-label)

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| genuine | 7 | 0 | 0 | 0 |
| genuine-merge | 7 | 0 | 0 | 0 |
| genuine-action | 4 | 1 | 0 | 0 |
| spurious | 124 | 4 | 0 | 0 |
| ambiguous | 21 | 1 | 0 | 0 |

### BOUNCED(other-not-admitted)

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| spurious | 11 | 0 | 0 | 0 |
| ambiguous | 7 | 0 | 0 | 0 |

## Calibration (body + supplement; genuine rows are too few in body alone)

| spurious max | n | actually spurious |
|---|---|---|
| [0.0,0.2) | 0 | n/a (0) |
| [0.2,0.4) | 0 | n/a (0) |
| [0.4,0.6) | 154 | 127/154 = 82.5% |
| [0.6,0.8) | 22 | 12/22 = 54.5% |
| [0.8,1.0] | 1 | 0/1 = 0.0% |

- AUC of genuine-sum (genuine* vs spurious): 0.495

### Threshold sweep — reference only, never a headline (RUBRIC), body set

| genuine_min | spurious_min | reduction | recall genuine |
|---|---|---|---|
| 0.6 | 0.4 | 0/139 = 0.0% | 10/10 = 100.0% |
| 0.6 | 0.5 | 0/139 = 0.0% | 10/10 = 100.0% |
| 0.6 | 0.6 | 0/139 = 0.0% | 10/10 = 100.0% |
| 0.6 | 0.7 | 0/139 = 0.0% | 10/10 = 100.0% |
| 0.6 | 0.8 | 0/139 = 0.0% | 10/10 = 100.0% |
| 0.9 | 0.4 | 4/139 = 2.9% | 10/10 = 100.0% |
| 0.9 | 0.5 | 4/139 = 2.9% | 10/10 = 100.0% |
| 0.9 | 0.6 | 2/139 = 1.4% | 10/10 = 100.0% |
| 0.9 | 0.7 | 1/139 = 0.7% | 10/10 = 100.0% |
| 0.9 | 0.8 | 0/139 = 0.0% | 10/10 = 100.0% |
| 1.2 | 0.4 | 21/139 = 15.1% | 8/10 = 80.0% |
| 1.2 | 0.5 | 17/139 = 12.2% | 9/10 = 90.0% |
| 1.2 | 0.6 | 6/139 = 4.3% | 10/10 = 100.0% |
| 1.2 | 0.7 | 1/139 = 0.7% | 10/10 = 100.0% |
| 1.2 | 0.8 | 0/139 = 0.0% | 10/10 = 100.0% |
| 1.5 | 0.4 | 78/139 = 56.1% | 6/10 = 60.0% |
| 1.5 | 0.5 | 60/139 = 43.2% | 7/10 = 70.0% |
| 1.5 | 0.6 | 9/139 = 6.5% | 8/10 = 80.0% |
| 1.5 | 0.7 | 1/139 = 0.7% | 10/10 = 100.0% |
| 1.5 | 0.8 | 0/139 = 0.0% | 10/10 = 100.0% |
| 1.8 | 0.4 | 117/139 = 84.2% | 2/10 = 20.0% |
| 1.8 | 0.5 | 86/139 = 61.9% | 6/10 = 60.0% |
| 1.8 | 0.6 | 9/139 = 6.5% | 7/10 = 70.0% |
| 1.8 | 0.7 | 1/139 = 0.7% | 10/10 = 100.0% |
| 1.8 | 0.8 | 0/139 = 0.0% | 10/10 = 100.0% |

## Grey zone only (n = 0 of replayed rows)

- no rows — nothing to measure; see (B) above.

## Operations

- outcome: {'evaluated': 209}
- error rate: 0/209 = 0.0%
- latency ms p50=254 p95=332
- server verdict is scope=out_of_gate (UNSURE by construction) on 209 rows; the recomputed verdict above is what (A) reads.
- cost per call: not in the replay record — read from Lexora usage.

