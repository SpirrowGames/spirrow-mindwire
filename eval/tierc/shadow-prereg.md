# Tier-C production shadow — pre-registration (tierc-v2)

Thread: `T-decider-conductor-hook`, step 2d. Decisions: Bohr msg-4634 (2d-1 / 2d-2), msg-4636 (2d-3), msg-4639 (2d-4 / 2d-5 / 2d-6), msg-4641 (2d-7), msg-4643 (2d-8); each approved by Einstein.

This file is written **before** any counted shadow row exists. It ships in the same PR as the hook change that adds the point-in-time columns. So any row that carries those columns was written after this registration (2d-3).

**Status: incomplete.** Two values below are not filled in yet. They are marked `PENDING`, and the PR stays a draft until they are.

## 1. What is counted

A `decider_decision` row from the conductor log is **counted** when all of the following hold. They are applied by `scripts/export_shadow_eval_set.py`.

0. **Time cut:** after the `--as-of` cut (§5), the row's `logged_at <= as_of` (2d-8).
1. **Point-in-time columns:** the row carries `state_wire`, `latest_msg_id` and `logged_at` (2d-3, 2d-8).
2. **Routing:** `routed == "stop"` (2d-1). `forced_naysayer` and `spawn_blocked` rows are only counted by number, in a table of their own.
3. **Questions version:** `questions_version == "tierc-v2"` (2d-4).
4. **Rules file:** `rules_sha256` equals the registered value (§2).
5. **Following messages:** the thread has 3 messages after `latest_msg_id`, or fewer and one of these holds (2d-7):
   - the thread's last message is `NEXT: none`;
   - the last message is at least 72 hours before `as_of`.
   A row that meets neither is **held**: counted by number only, not evaluated.

A row that fails any condition appears only as a count, per reason, in `export.json`.

## 2. Registered values

| Item | Value | Source |
|---|---|---|
| Questions version | `tierc-v2` | `config.py:528` (live default) |
| `ask_min` | **0.60** | `verdict.py:371` `DEFAULT_V2_ASK_MIN`. Fixed before the replay; cited here, not chosen here (2d-4) |
| `not_ask_max` | **0.40** | `verdict.py:374` `DEFAULT_V2_NOT_ASK_MAX`, same |
| `rules_sha256` | `PENDING`: the operator copies it from the production daemon's start-up log line `decider tierc-v2 rules loaded from … (rules_sha256=…)` (`decider_lexora.py:457`, msg-4639) | production |
| `--as-of` | `PENDING`: written here and in the manifest when the single export before the lock is run (§5, msg-4641) | export run |

The report recomputes every verdict from `raw_answers["should_ask_human"]` using the registered thresholds. A different `[decider].tierc_v2_*` value in the production config therefore does not change what is measured.

## 3. Primary hypothesis (one)

Under tierc-v2 with `ask_min = 0.60` and `not_ask_max = 0.40`, Jev's verdict:
- `p >= 0.60` → CONFIRMED
- `p < 0.40` → LIKELY_NOT
- otherwise UNSURE

The hypothesis is that this verdict **never keeps a genuine escalation from the human** and **moves some spurious escalations to LIKELY_NOT**.

## 4. Decision criteria (fixed now)

Apply these in order. `tierc_eval_report.py` prints the inputs to each one.

1. **Invalid:** if any counted row has no v2 verdict (`NO_VERDICT`), the evaluation is **invalid**. A missing verdict reaches the human, so recall would pass without Jev having answered (2d-4). `MALFORMED` is reported as a count and a share.
2. **Not adopted:** if genuine recall (consensus truth, every genuine class) is below **100%** (2d-1).
3. **Undecided:** if fewer than **12** spurious rows can be evaluated. Shadow continues (2d-1).
4. Otherwise the spurious → LIKELY_NOT **reduction** is reported as a number, with **no pass bar**. Whether to make anything active is Takahito's decision (msg-3404 §6).

Also reported, not criteria:
- the `following_n < 3` rows, counted with their consensus-truth breakdown (2d-7);
- Cohen's κ between the two labellers.

## 5. Procedure

1. **Export once**, with `--as-of` set to the time of the run (timezone-aware ISO 8601, not in the future):
   ```
   uv run python scripts/export_shadow_eval_set.py --log <conductor log> [--log …] \
     --rules-sha256 <§2> --project <project> [--project …] --as-of <ISO 8601> \
     --out-dir eval/tierc/shadow-<date> --replay-out <path outside that directory>
   ```
   - The exporter drops every message and row after `--as-of` before doing anything else (2d-8).
   - Record `--as-of` in §2 and the manifest.
   - Jev's side goes to `--replay-out`. It never sits in the labellers' directory (2d-2).
2. **Copy** `eval/tierc/label_prompt.md` and `eval/tierc/RUBRIC.md` into `eval/tierc/shadow-<date>/` unchanged. The prompt is not edited, so the system-prompt hash is the replay's (2d-6).
3. **Label** with the replay's labellers, reading `materials.jsonl` only:
   ```
   uv run python scripts/label_eval_set.py --dir eval/tierc/shadow-<date> --labeller naysayer-tier
   uv run python scripts/label_eval_set.py --dir eval/tierc/shadow-<date> --labeller frontier-tier
   ```
4. **Lock**:
   ```
   uv run python scripts/label_eval_set.py --dir eval/tierc/shadow-<date> --lock
   ```
   This writes the manifest hashes of the prompt, rubric, materials, fixture and both label files.
   > **Order note for Bohr.** 2d-2 lists "lock, then label". `label_eval_set.py --lock` refuses to run until both label files cover every material row (`write_lock`), so the lock comes after labelling. It still comes **before** any label is joined to Jev's output, which is the separation msg-4224 asks for. Please confirm or amend.
5. **Measure** without `--corrections` (2d-2 (d)):
   ```
   uv run python scripts/tierc_eval_report.py --replay <replay-out> \
     --fixture eval/tierc/shadow-<date>/fixture.jsonl \
     --labels naysayer-tier=eval/tierc/shadow-<date>/labels.naysayer-tier.jsonl \
     --labels frontier-tier=eval/tierc/shadow-<date>/labels.frontier-tier.jsonl
   ```

## 6. Exploration (never a result on this data)

- The `SWEEP_V2` table in `tierc_eval_report.py`: `(0.50, 0.33)`, `(0.60, 0.40)`, `(0.70, 0.47)`, `(0.80, 0.53)`. These are constants, not computed values (msg-4641).
- The AUC of `should_ask_human`, genuine* vs spurious.

A pair picked from the sweep only becomes the primary hypothesis of the **next** shadow period. It is never confirmed on the data it was picked from (msg-4634 2d-1, msg-4376 §2-2).

## 7. When to evaluate

At the **later** of:
- 2 weeks after this registration reaches `main`;
- the day the counted rows reach **40** (2d-1).

Before that, running the exporter to watch the counts is fine. Only the single export in §5 step 1 is labelled and locked.
