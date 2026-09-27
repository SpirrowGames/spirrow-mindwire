# Tier-C replay evaluation — labelling rubric

Status: **DRAFT — transcribed by Heisenberg from msg-4219 §1 and msg-4224; the proposer (Bohr)
confirms or amends it before it is locked.** The rubric is committed and its hash fixed **before**
either labeller starts, and both label files are committed and hashed **before any evaluation-set
row is sent to Jev** (msg-4224). The 2-row contract check (`contract.jsonl`) is not part of the
evaluation set and was allowed to run in parallel (msg-4224, endorsed msg-4225).

## Unit and key

One row = one message the conductor resolves to `NEXT: human`. Key = `(thread_id, round_index)`,
where `round_index` is the message's 0-based position in its thread. The label file carries the
full body (`body`) for reading; the fixture sent to Jev never carries a label.

## Fields to fill (per row, per labeller)

- `label` — one of `genuine` / `genuine-merge` / `genuine-action` / `spurious` / `ambiguous`
- `category` — the original class: `GOAL` / `COST` / `IRREVERSIBLE` / `MERGE` /
  `HUMAN_ONLY_ACTION` / `IMPL` / `ROUTING_ARTIFACT` (or `OTHER`)
- `rationale` — one line: why

## Label rules (msg-4219 §1 table)

| category | label | note |
|---|---|---|
| GOAL / COST / IRREVERSIBLE | `genuine` | headline recall |
| MERGE | `genuine-merge` when the loop-control state **at that time** required a human approval (protected branch etc.); `spurious` when the loop could have merged itself | D9 removed merge from the Decider's vocabulary, so `genuine-merge` is reported on its own row, not in the headline recall |
| HUMAN_ONLY_ACTION | `genuine-action` when only a human can do it (credentials, billing console, physical work, account permissions); `spurious` when the agent could have done it itself (e.g. reachability it wrongly assumed missing) | counted on the genuine side (a bounce would stall the thread), reported separately |
| IMPL / ROUTING_ARTIFACT | `spurious` | |
| cannot decide | `ambiguous` | excluded from headline numbers; count and reason reported |

## Independence (msg-4224)

- Bohr and Einstein each label **every** row independently, from a blank template.
- Neither sees the other's labels or any Jev output before both files are committed.
- Agreement → that label is the truth. Disagreement (or a row only one labeller filled) →
  `ambiguous`, listed one by one with both rationales and Jev's verdict.
- The report gives recall / reduction three ways (agreed rows, Bohr's labels, Einstein's labels)
  and Cohen's κ.
