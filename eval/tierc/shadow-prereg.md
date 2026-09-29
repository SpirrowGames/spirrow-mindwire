# Tier-C production shadow — pre-registration (tierc-v2)

Thread: `T-decider-conductor-hook`, step 2d. Decisions: Bohr msg-4634 (2d-1 / 2d-2), msg-4636 (2d-3), msg-4639 (2d-4 / 2d-5 / 2d-6), msg-4641 (2d-7), msg-4643 (2d-8), msg-4646 (2d-9 / 2d-10), msg-4648 + msg-4650 (2d-11), msg-4652, msg-4654 (2d-12), msg-4656 (2d-13); each approved by Einstein.

This file is written **before** any counted shadow row exists. It ships in the same PR as the hook change that adds the point-in-time columns. So any row that carries those columns was written after this registration (2d-3).

**Status: one value is still `PENDING`:** `rules_sha256` (§2). The PR stays a draft until it is filled in.

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
| `RUBRIC-v2.md` sha256 | `c99ce23c7bad069e0d419d9bd18d2764959f037b61f3f7907ac99fba3f65376c` | `eval/tierc/RUBRIC-v2.md` at this commit (2d-10) |
| `label_prompt-v2.md` sha256 | `48cda10df37f2050599b4262f875d44b268cbb0f362dfff1c8d5b62238d0fe72` | `eval/tierc/label_prompt-v2.md` at this commit (2d-10) |
| `rules_sha256` | `PENDING`: the operator copies it from the production daemon's start-up log line `decider tierc-v2 rules loaded from … (rules_sha256=…)` (`decider_lexora.py:457`, msg-4639) | production |

The report recomputes every verdict from `raw_answers["should_ask_human"]` using the registered thresholds. A different `[decider].tierc_v2_*` value in the production config therefore does not change what is measured.

**`--as-of` is not a registered value** (msg-4646). The rule for choosing it is registered instead: it is the time of the single export run just before labelling, and that time must satisfy §7. Its value is recorded in `export.json` (2d-9) and printed at the top of the report (2d-11).

`RUBRIC-v2.md:7-12` asks that the rubric and prompt hashes be recorded before any v2 row goes to Jev. For the shadow, this commit is that point: counted rows can only be written after it (msg-4636).

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

1. **Export once**, with `--as-of` set to the time of the run (timezone-aware ISO 8601, not in the future; §2 gives the rule):
   ```
   uv run python scripts/export_shadow_eval_set.py --log <conductor log> [--log …] \
     --rules-sha256 <§2> --project <project> [--project …] --as-of <ISO 8601> \
     --out-dir eval/tierc/shadow-<date> --replay-out <path outside that directory>
   ```
   - The exporter drops every message and row after `--as-of` before doing anything else (2d-8).
   - Jev's side goes to `--replay-out`. It never sits in the labellers' directory (2d-2).
   - The exporter also copies `eval/tierc/RUBRIC-v2.md` and `eval/tierc/label_prompt-v2.md` into the directory under the same names (2d-13).
   - It writes `export.json` with `as_of`, the counts, the sha256 of `materials.jsonl`, `fixture.jsonl` and the replay file, and the name + sha256 of the rubric and prompt (2d-9, 2d-13).
2. **Check and commit.**
   - Check that `export.json`'s `rubric.sha256` and `label_prompt.sha256` equal §2.
   - Commit the directory, including `export.json`, before any labelling (2d-9).
3. **Label** with the replay's labellers, reading `materials.jsonl` only, under the v2 rubric and prompt:
   ```
   uv run python scripts/label_eval_set.py --dir eval/tierc/shadow-<date> \
     --prompt-file label_prompt-v2.md --rubric-file RUBRIC-v2.md --labeller naysayer-tier
   uv run python scripts/label_eval_set.py --dir eval/tierc/shadow-<date> \
     --prompt-file label_prompt-v2.md --rubric-file RUBRIC-v2.md --labeller frontier-tier
   ```
   `--prompt-file` and `--rubric-file` take bare file names inside `--dir` (2d-12).
4. **Lock** after labelling (msg-4646: the order is label → lock):
   ```
   uv run python scripts/label_eval_set.py --dir eval/tierc/shadow-<date> \
     --prompt-file label_prompt-v2.md --rubric-file RUBRIC-v2.md --lock
   ```
   The manifest records the sha256 of the v2 rubric and prompt (under those names), the materials, the fixture and both label files.
5. **Measure** without `--corrections`, with the export lock (2d-2 (d), 2d-11):
   ```
   uv run python scripts/tierc_eval_report.py --replay <replay-out> \
     --fixture eval/tierc/shadow-<date>/fixture.jsonl \
     --labels naysayer-tier=eval/tierc/shadow-<date>/labels.naysayer-tier.jsonl \
     --labels frontier-tier=eval/tierc/shadow-<date>/labels.frontier-tier.jsonl \
     --export-manifest eval/tierc/shadow-<date>/export.json
   ```
   The report refuses the run if the replay, fixture or materials no longer match `export.json`. It also refuses if the records mix versions.

## 6. Exploration (never a result on this data)

- The `SWEEP_V2` table in `tierc_eval_report.py`: `(0.50, 0.33)`, `(0.60, 0.40)`, `(0.70, 0.47)`, `(0.80, 0.53)`. These are constants, not computed values (msg-4641).
- The AUC of `should_ask_human`, genuine* vs spurious.

A pair picked from the sweep only becomes the primary hypothesis of the **next** shadow period. It is never confirmed on the data it was picked from (msg-4634 2d-1, msg-4376 §2-2).

## 7. When to evaluate

At the **later** of:
- 2 weeks after this registration reaches `main`;
- the day the counted rows reach **40** (2d-1).

Before that, running the exporter to watch the counts is fine. Only the single export in §5 step 1 is labelled and locked.
