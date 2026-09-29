# Tier-C replay evaluation — Jev

- corrections: **applied** — `eval\tierc\corrections\2026-09-28-roster-selection.json` (sha256 `91c4ea7845d6ec592409fd219b1b77ad6f84e1a113a0d27ee328be741e68f42f`, selection code `61fb1b243169608708ef177e42e53838200550ab`); rows counted: **66 / 209** replayed. The population line below is not corrected.

## (A) / (B) — read this first (msg-4219 §0, msg-4221)

- msg-3630's 179 rows cannot be reproduced (no per-row list was published); this evaluation is not compared with that breakdown (msg-4229 §1).
- harvested population (gate only, no Jev call): 862 rows; **in the grey zone: 0**
- rows replayed: body 52, supplement 14
- **(B) grey zone (ADMIT_UNSURE / second_time_force_admit): 0/66 = 0.0% of replayed rows** — under today's D18 gating only these reach Jev's verdict; (B) = this share x (A).
- rows the live hook's entry check would admit (proposer-authored `NEXT: human`): 52/52 = 100.0%; of those in the grey zone: 0
- The gate is **today's** `compute_gate_result` (allowed labels, label migration), not the rules in force when each message was posted (msg-4226).
- Changing D18 or the allowed labels would be an amendment grounded in ADR-2026-05-23-07 (msg-4224); that decision is out of scope here.
- rosters from today's config (`current_fallback`): 0
- thresholds (fixed in advance, RUBRIC): genuine_min=0.6 genuine_max=0.4 spurious_min=0.6

## (A) Jev's discrimination — body set (headline)

### truth = consensus
- reduction (spurious → LIKELY_NOT): 0/12 = 0.0%
-   of which fired_reason=answerable_from_thread (§4.5 bounce): 0/12 = 0.0%
- recall genuine (reaches human): 9/9 = 100.0%
- recall genuine-merge (reaches human): 12/12 = 100.0%
- recall genuine-action (reaches human): 5/5 = 100.0%
- ambiguous (excluded): 14
- unlabelled (excluded): 0

### truth = naysayer-tier
- reduction (spurious → LIKELY_NOT): 0/16 = 0.0%
-   of which fired_reason=answerable_from_thread (§4.5 bounce): 0/16 = 0.0%
- recall genuine (reaches human): 15/15 = 100.0%
- recall genuine-merge (reaches human): 16/16 = 100.0%
- recall genuine-action (reaches human): 5/5 = 100.0%
- ambiguous (excluded): 0
- unlabelled (excluded): 0

### truth = frontier-tier
- reduction (spurious → LIKELY_NOT): 0/16 = 0.0%
-   of which fired_reason=answerable_from_thread (§4.5 bounce): 0/16 = 0.0%
- recall genuine (reaches human): 11/11 = 100.0%
- recall genuine-merge (reaches human): 14/14 = 100.0%
- recall genuine-action (reaches human): 10/10 = 100.0%
- ambiguous (excluded): 1
- unlabelled (excluded): 0

- Cohen's κ (naysayer-tier vs frontier-tier): 0.648 over 66

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
| genuine | 9 | 0 | 0 | 0 |
| genuine-merge | 12 | 0 | 0 | 0 |
| genuine-action | 4 | 1 | 0 | 0 |
| spurious | 12 | 0 | 0 | 0 |
| ambiguous | 13 | 1 | 0 | 0 |

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
| genuine | 7 | 0 | 0 | 0 |
| ambiguous | 5 | 1 | 0 | 0 |

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

### IMPL [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| spurious | 4 | 0 | 0 | 0 |
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

### ROUTING_ARTIFACT [body]

| truth \ Jev | CONFIRMED | UNSURE | LIKELY_NOT | NO_VERDICT |
|---|---|---|---|---|
| spurious | 7 | 0 | 0 | 0 |
| ambiguous | 3 | 0 | 0 | 0 |

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
| genuine | 6 | 0 | 0 | 0 |
| genuine-merge | 7 | 0 | 0 | 0 |
| genuine-action | 4 | 1 | 0 | 0 |
| spurious | 1 | 0 | 0 | 0 |
| ambiguous | 6 | 1 | 0 | 0 |

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
| [0.4,0.6) | 34 | 8/34 = 23.5% |
| [0.6,0.8) | 14 | 4/14 = 28.6% |
| [0.8,1.0] | 1 | 0/1 = 0.0% |

- AUC of genuine-sum (genuine* vs spurious): 0.705

### Threshold sweep — reference only, never a headline (RUBRIC), body set

| genuine_min | spurious_min | reduction | recall genuine |
|---|---|---|---|
| 0.6 | 0.4 | 0/12 = 0.0% | 9/9 = 100.0% |
| 0.6 | 0.5 | 0/12 = 0.0% | 9/9 = 100.0% |
| 0.6 | 0.6 | 0/12 = 0.0% | 9/9 = 100.0% |
| 0.6 | 0.7 | 0/12 = 0.0% | 9/9 = 100.0% |
| 0.6 | 0.8 | 0/12 = 0.0% | 9/9 = 100.0% |
| 0.9 | 0.4 | 0/12 = 0.0% | 9/9 = 100.0% |
| 0.9 | 0.5 | 0/12 = 0.0% | 9/9 = 100.0% |
| 0.9 | 0.6 | 0/12 = 0.0% | 9/9 = 100.0% |
| 0.9 | 0.7 | 0/12 = 0.0% | 9/9 = 100.0% |
| 0.9 | 0.8 | 0/12 = 0.0% | 9/9 = 100.0% |
| 1.2 | 0.4 | 5/12 = 41.7% | 7/9 = 77.8% |
| 1.2 | 0.5 | 3/12 = 25.0% | 8/9 = 88.9% |
| 1.2 | 0.6 | 2/12 = 16.7% | 9/9 = 100.0% |
| 1.2 | 0.7 | 0/12 = 0.0% | 9/9 = 100.0% |
| 1.2 | 0.8 | 0/12 = 0.0% | 9/9 = 100.0% |
| 1.5 | 0.4 | 10/12 = 83.3% | 5/9 = 55.6% |
| 1.5 | 0.5 | 8/12 = 66.7% | 6/9 = 66.7% |
| 1.5 | 0.6 | 4/12 = 33.3% | 7/9 = 77.8% |
| 1.5 | 0.7 | 0/12 = 0.0% | 9/9 = 100.0% |
| 1.5 | 0.8 | 0/12 = 0.0% | 9/9 = 100.0% |
| 1.8 | 0.4 | 11/12 = 91.7% | 2/9 = 22.2% |
| 1.8 | 0.5 | 8/12 = 66.7% | 5/9 = 55.6% |
| 1.8 | 0.6 | 4/12 = 33.3% | 6/9 = 66.7% |
| 1.8 | 0.7 | 0/12 = 0.0% | 9/9 = 100.0% |
| 1.8 | 0.8 | 0/12 = 0.0% | 9/9 = 100.0% |

## Grey zone only (n = 0 of replayed rows)

- no rows — nothing to measure; see (B) above.

## Operations

- outcome: {'evaluated': 66}
- error rate: 0/66 = 0.0%
- latency ms p50=263 p95=332
- mindwire-side verdict (``adapters/decider_lexora.py``, not the server) is scope=out_of_gate (UNSURE by construction) on 66 rows — every non-grey-zone gate result, valid-label ADMIT and BOUNCE alike; the recomputed verdict above is what (A) reads.
- cost per call: not in the replay record — read from Lexora usage.


---

<!-- Everything above this rule is the verbatim output of:
     uv run python scripts/tierc_eval_report.py --replay eval/tierc/replay.jsonl --fixture eval/tierc/fixture.jsonl --labels naysayer-tier=eval/tierc/labels.naysayer-tier.jsonl --labels frontier-tier=eval/tierc/labels.frontier-tier.jsonl --population eval/tierc/population.jsonl --corrections eval/tierc/corrections/2026-09-28-roster-selection.json --out <file>
     Everything below is hand-written and is not produced or checked by the script. -->

## Conclusion (hand-written, msg-4376 §2; replaces the msg-4273 §4 draft)

- **Effect as deployed: none.** Under the pre-registered thresholds (0.60 / 0.40 / 0.60), Jev moved **0 of the 12** spurious escalations to LIKELY_NOT. It sent every genuine escalation to the human (100%, no misses).
- **Separation: some separation, not established.** AUC of the genuine-score sum on the 66 real escalations is **0.705**. The earlier **0.495 is withdrawn**: it came from a set in which 143 of 209 rows were not escalations at all (next section). The AUC rests on 37 genuine* vs 12 spurious rows (consensus truth, ambiguous excluded), so it is very uncertain: a Hanley–McNeil approximation gives a 95% interval of roughly ±0.16 (about 0.55–0.86).
- **Labeller agreement:** Cohen's κ = **0.648** (naysayer-tier vs frontier-tier, 66 rows), reported as a measure of how well the two labelling tiers agree.
- **(B) = 0 stands.** No replayed row and no harvested row was in the grey zone, so under today's D18 gating Jev's verdict is never acted on.

### Next steps (in this order)
1. **Do not make the Conductor hook active on the basis of this evaluation.** Nothing here supports it. The decision stays with Takahito (msg-3404 §6).
2. **Any new threshold must be tested on data it was not tuned on.** Tuning thresholds on these 66 rows and scoring them on the same 66 would be a result fitted after the fact. At most, a candidate threshold may be recorded before any new data arrives. The threshold sweep above is reference only.
3. **That new data is the production shadow from `T-decider-conductor-hook`:** Jev's own verdict on real `NEXT: human` escalations, with no replay involved. `tierc_eval_report.py` can run on it with no corrections file.
4. **Class note.** The hand classification (msg-3630) had about 60 ROUTING_ARTIFACT rows. Most of them were not escalations at all and are among the 143 excluded rows. The room for Jev to cut escalations is therefore smaller than first thought (12 of the 66 in this set), and the reduction measure should be read against that smaller base.
5. **A fuller-input evaluation** (carried over from msg-4289 / msg-4298 §5) should make sure the handoff line is inside the excerpt Jev sees, and should include rows sent with `is_grey_zone: true`.
6. **Whether to collect enough real escalations and evaluate again** involves Jev calls, so it is a billing decision: it goes to Takahito with an expected count and cost (Tier-C, msg-4298 §5).

## The target population was wrong (hand-written, msg-4297 / msg-4298)

The first run (#347 `357b310`) replayed 209 rows. **143 of them were not escalations.** The fixture builder resolved each handoff against an empty roster `{}`, so an ordinary role handoff (for example `NEXT: Heisenberg`) came out as HUMAN / FIELD_UNRESOLVABLE. Against the conductor roster recorded in `harvest.json` they are plain role handoffs. The fix is in #350 (merged as `61fb1b2`); the corrections file above is that code's output.

- **Breakdown of the 143:** all 143 have the same exclusion reason, `not_an_escalation` (see `corrections/2026-09-28-roster-selection.json`, `exclude[].detail`).
- **Why the labellers did not notice:** for 117 of the 143 the material body was cut at 8,000 characters, and for **102** of those the cut body contains no `NEXT:` line at all, so the labellers never saw the handoff line (counted from `materials.jsonl` against the corrections file).
- **Byte-for-byte check (msg-4298 §3, msg-4304):** with the fixed code and the harvest's own roster, all 66 kept rows rebuild byte-identical to the locked `fixture.jsonl`; 0 rows moved between body and gate_only; 0 new escalations appeared. Regenerating the corrections file on `61fb1b2` gave the same keep 66 / exclude 143 / added 0.
- **Population:** 525 of the 862 harvested population rows were not escalations either (msg-4297; this count is not re-derived by the script). The (B) = 0 conclusion is unaffected: there were 0 grey-zone rows before removing them.

The withdrawn 209-row numbers, kept for the record (consensus truth, body set, same thresholds):

| Measure | 209 rows (withdrawn) | 66 real escalations |
|---|---|---|
| reduction (spurious → LIKELY_NOT) | 0/139 | 0/12 |
| recall genuine / genuine-merge / genuine-action | 10/10 · 12/12 · 5/5 | 9/9 · 12/12 · 5/5 |
| AUC of genuine-sum | 0.495 | 0.705 |
| calibration: actually spurious at spurious-max [0.4,0.6) · [0.6,0.8) | 127/154 = 82.5% · 12/22 = 54.5% | 8/34 = 23.5% · 4/14 = 28.6% |
| Cohen's κ | 0.677 (209) | 0.648 (66) |

## Limits of this result (hand-written, msg-4273 / msg-4275 / msg-4298 §4; not produced by `tierc_eval_report.py`)

- **The narrowing to 66 rows was done after Jev's results were seen.** The criterion is mechanical (would the conductor, with the same roster, resolve the message to HUMAN) and comes from a code bug fix that never looked at Jev's answers or the labels. But it was not pre-registered.
- **The sample is small.** 66 rows; 12 spurious by consensus truth (16 by each single labeller). 0/12 has a one-sided 95% upper bound of about 22%.
- **(a) Jev saw the live-shaped excerpt only.** The Turn is built by the live hook's functions (msg-4219 §2), so `head_summary` and every `recent_events[].body_head` are the first `BODY_HEAD_M` = 500 characters of each message (`decider/hook.py`). The labellers read the body up to 8,000 characters plus the 3 following messages that Jev never sees (msg-4229 §2). This result therefore measures **Jev on live-shaped input**. It does not say whether Jev could separate the classes given more of the text.
- **(b) The `scope` field is not in the request, but the gate result is.** `build_decide_request` (`decider/wire.py`), the one builder that live and replay share, sends `state`, `questions`, `policy` and `questions_version`, and nothing named `scope`. `scope` and the UNSURE verdict are attached on the mindwire side after the answer returns. However, `state` carries `gate_result`, including `is_grey_zone`. All replayed rows went out with `is_grey_zone: false`. Whether Lexora or Jev uses that field was not checked (the available spirrow-lexora checkout predates `/v1/decide`, msg-4275). Jev's answers on grey-zone rows, the only rows whose verdict live would act on, are an **untested boundary**.
- **Jev's cost was not measured.** `/v1/decide` writes no row to Lexora's `/stats/costs/recent`, so the per-call measurement in msg-4219 §4-2 could not be done.
- **Frontier-tier labelling cost is in tokens only.** Lexora's cost rows for `claude-fable-5-1` carry `pricing_known=0`. The run used 1,427,667 input and 65,106 output tokens over 14 batches (all 209 rows). The naysayer-tier labelling cost $3.74 (ledger).
- `RUBRIC.md` still names the Claude-side labeller `claude`. The labeller that actually ran is `frontier-tier` (msg-4245). RUBRIC is part of the hash-locked system prompt (sha `28433e43…`), so it is deliberately left unedited.
- **Hash-lock chronology** is in #347's branch history: the lock commit `2b80b53` (03:40:13Z) precedes the first Jev call on an evaluation row (03:40:35Z), the pilot `adac556` and the full run `357b310`. PR-B is merged with a merge commit so that this order survives on `main` (msg-4291 §1).
