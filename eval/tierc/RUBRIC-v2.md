# Tier-C replay evaluation v2 — labelling rubric (五ヶ条)

This rubric is taken from thread T-decider-tierc-v2-all-escalations: Fermi msg-4360, msg-4361 and msg-4362 (Takahito's decisions of 2026-09-28), and Bohr msg-4380 Δ6, amended by msg-4382 and msg-4384. Einstein approved it in msg-4381, msg-4383 and the review after msg-4384.

The v1 files (`RUBRIC.md` and `label_prompt.md`) are hash-locked and stay unchanged. This file and `label_prompt-v2.md` replace them for v2 only.

Before any v2 evaluation row is sent to Jev, the eval run's manifest must record:

- the sha256 of this file;
- the sha256 of `label_prompt-v2.md`;
- the sha256 of the label file;
- the `rules_sha256` of the rules file used.

## Why relabel

The v1 truth labels were made against the old definition: goal / cost / irreversible, with merge counted separately. The 五ヶ条 change that definition in three ways:

- merging to main is no longer a decision point;
- rules 4 and 5 are new.

Scoring v2 against the v1 labels would therefore compare two different definitions. Every row is labelled again under this rubric (msg-4361), and v1 and v2 are both scored against these new labels. The v1 numbers against the old labels are also reported, alongside.

## Unit, key and evaluation sets

- **One row** is one message that the conductor resolves to `NEXT: human`.
- **Key** is `(thread_id, round_index)`. `round_index` is the message's 0-based position in its thread.
- **Entry.** A row is in scope when all of the following hold (msg-4360, msg-4361, msg-4382):
  - the author's roster role is `proposer`, `implementer` or `naysayer`;
  - the author wrote `NEXT: human` themself;
  - the stop is `human`.
- **Excluded authors.** Off-roster authors are excluded. For example, `pr-gate-relay` writes APPROVE → `NEXT: human` as a merge-wait **notification**. That is not a question to the human.
- **Table A** is the existing 66 rows (`eval/tierc/`, after corrections). All of them are sent to Jev under v2, including the ADMIT rows that v1 did not send (D18 is withdrawn).
- **Table B** is the rows the widened entry adds: the implementer's and the naysayer's own `NEXT: human`. They are extracted from the existing harvest and reported in their own table, never mixed into Table A.

## What to give for each row

- `label`: one of `ask`, `no-ask`, `ambiguous`.
- `rule`: one of `rule_1`, `rule_2`, `rule_3`, `rule_4`, `rule_5`, `none`. Give the rule the handoff falls under. If it falls under more than one, give the one it fits best. If it falls under none, give `none`.
- `rationale`: one line saying why.

## The 五ヶ条 — what is to be asked of the human

These are the rule texts as Takahito fixed them (msg-4361). The rules file that the Jev run uses must hold the same texts, and its `rules_sha256` is recorded next to the results.

- `rule_1`: 金銭的コストの増加が見込まれる変更、アイデアの承認
- `rule_2`: 対象となるプロジェクトに対して決めた仕様の追加、削除、変更を伴う変更の承認
- `rule_3`: 取り消せない操作や外部への公開（データ・リポジトリの削除、force push などの履歴書き換え、ストア / SNS など外部への公開）
- `rule_4`: 人間にしかできない作業の依頼（PAT 等の資格情報の更新、課金・クレジット追加、Takahito の PC での操作、Takahito の手元にしかない情報の提供）
- `rule_5`: AI 同士で決着がつかない対立の裁定（proposer と naysayer が合意に至らないとき）
  - Guideline: the naysayer has sent the same design back, and the proposer has revised it, 3 or more times in a row, with no `VERDICT: APPROVE` in between.

## Label rules

| the handoff… | label | rule |
|---|---|---|
| falls under at least one of the 五ヶ条 | `ask` | the rule it fits best |
| falls under none of them | `no-ask` | `none` |
| cannot be decided | `ambiguous` | the best guess, or `none` |

- **Merging to main is not a rule** (msg-4361). The main-merge escalation already happens when the PR is opened. A handoff that only waits for a merge or a PR approval is therefore `no-ask`, unless it also falls under one of the 五ヶ条.
- **Not enough context.** If the material you were given is not enough to decide, label the row `ambiguous` and start the rationale with `insufficient-context`.
- **`rule` does not change `label`.** A row that falls under a rule is `ask`, and a row that falls under none is `no-ask`. The only exception is `ambiguous`, which is allowed with either.

## Material

The material is the same as in v1. For each row you get:

- a header: project, thread, author, time;
- the escalation message in full, up to 8,000 characters (if it was cut, the material says so);
- the first 500 characters of each of the **5 messages before** it;
- the first 500 characters of each of the **3 messages after** it.

The 3 later messages are **for labelling only**. They are never put in the fixture and never sent to Jev.

## Labeller and procedure

- **One labeller.** Only the `naysayer` Lexora tier (Gemini) labels, per msg-4361. The `frontier` tier is not used in SpirrowGames (Takahito's policy). The cost is approved (msg-4362).
- **No κ.** With one labeller there is no Cohen's κ and no agreement-based truth. This is reported as a stated weakness of the v2 tables.
- **Sanity check instead.** For the Table A rows, report how often the v1 categories agree with the v2 rules:
  - `GOAL` against `rule_2`;
  - `COST` against `rule_1`;
  - `IRREVERSIBLE` against `rule_3`.
- **Truth.** The single labeller's label is the truth. `ambiguous` rows are listed one by one and left out of the recall and reduction figures.
- **Batches, retries, failures, fixes and hand edits** follow `RUBRIC.md` §"Independence and procedure", read with one labeller instead of two. No one ever edits or fills in a label by hand.

## Thresholds are fixed before any result is seen

- v2's headline numbers use `ask_min=0.60` and `not_ask_max=0.40` (msg-4380 Δ4). These are the v1 values, fixed here before any v2 result exists.
- v1's headline numbers use its live defaults: `genuine_min=0.60`, `genuine_max=0.40`, `spurious_min=0.60`.
- **Table columns.** Each table has three columns: v1 scored against the old labels, v1 scored against the new labels, and v2 scored against the new labels.
- **Metrics in each column.** Each column reports:
  - AUC;
  - reduction at the fixed thresholds;
  - misses (the number of `ask` rows a verdict of `LIKELY_NOT` would have kept from the human).
  Each metric is given at both body lengths, 500 and 8,000 characters.
- **Reduction denominator.** The denominator of the reduction figure is the number of `no-ask` rows under the new labels. The old denominator of 12 is used only in the "v1, old labels" column.
- **Threshold sweep.** A threshold sweep is for reference only and never becomes a headline number.
