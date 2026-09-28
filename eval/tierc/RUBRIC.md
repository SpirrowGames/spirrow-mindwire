# Tier-C replay evaluation — labelling rubric

This rubric is taken from Bohr's messages msg-4219 §1, msg-4224, msg-4229 §1–§3, msg-4231 and msg-4233. Einstein approved it in the reply that followed msg-4233.

Its sha256 is recorded in `manifest.json` together with `label_prompt.md` and both label files. No evaluation row is sent to Jev before that record exists.

## Unit, key and evaluation set

- **One row** is one message that the conductor resolves to `NEXT: human`.
- **Key** is `(thread_id, round_index)`. `round_index` is the message's 0-based position in its thread.
- **Headline set (`body`).** Messages posted from 2026-09-01 until msg-3630 was posted, restricted to those that pass the live Tier-C entry check. That check requires all of the following:
  - the author holds the `proposer` role;
  - the author wrote `NEXT: human` themself;
  - the stop is `human`.
- **`supplement` set.** The retrospective candidates, plus every row the admission gate ADMITs that is not already in `body`. These rows are reported on their own lines and never mixed into the headline numbers.
- **Everything else** gets a gate result only. It is not sent to Jev and not labelled.
- The 179 rows from msg-3630 cannot be reproduced, because no per-row list of them was ever published. This evaluation does not compare against that breakdown.

## What to give for each row

- `label`: one of `genuine`, `genuine-merge`, `genuine-action`, `spurious`, `ambiguous`.
- `category`: one of `GOAL`, `COST`, `IRREVERSIBLE`, `MERGE`, `HUMAN_ONLY_ACTION`, `IMPL`, `ROUTING_ARTIFACT`, `OTHER`.
- `rationale`: one line saying why.

## Label rules

| category | label | note |
|---|---|---|
| GOAL / COST / IRREVERSIBLE | `genuine` | These rows make up the headline recall. |
| MERGE | `genuine-merge` if the loop-control state at that time needed a human's approval (for example a protected branch). `spurious` if the loop could have merged on its own. | Merge is outside the Decider's vocabulary, so `genuine-merge` gets its own row and is not part of the headline recall. |
| HUMAN_ONLY_ACTION | `genuine-action` if only a human can do it (credentials, a billing console, physical work, account permissions). `spurious` if the agent could have done it itself. | Counted on the genuine side and reported separately. |
| IMPL / ROUTING_ARTIFACT | `spurious` | |
| Cannot decide | `ambiguous` | |

- **`OTHER`.** A row whose `category` is `OTHER` counts as `ambiguous`, whatever its `label` says.
- **Not enough context.** If the material you were given is not enough to decide, label the row `ambiguous` and start the rationale with `insufficient-context`. No labeller is ever given more material than the other.

## Material (the same for both labellers)

For each row you get:

- a header: project, thread, author, time;
- the escalation message in full, up to 8,000 characters (if it was cut, the material says so);
- the first 500 characters of each of the **5 messages before** it;
- the first 500 characters of each of the **3 messages after** it.

The 3 later messages exist **for labelling only**. They show what the human actually did next. They are never put in the fixture and never sent to Jev.

## Independence and procedure

- **The script.** `scripts/label_eval_set.py` labels every row twice.
  - The two labellers are two model families behind the same Lexora client:
    - `naysayer-tier`: the Lexora tier `naysayer`;
    - `claude`: the Lexora tier `frontier`, an Anthropic model reached through the raw API with no harness prompt.
  - Both labellers get the same system prompt (`label_prompt.md` followed by this rubric), the same material and the same batches.
  - Neither labeller ever sees the other's labels or any Jev output.
  - The naysayer preamble is not included.
- **Batches.** Rows are sent in batches of 15. The answer must be a JSONL code block with exactly one line per row sent, and no other rows.
- **Transport failure** (timeout, 5xx, lost connection). The batch is retried up to 3 times with a backoff between tries. If it still fails, the script stops.
- **Format failure** (invalid JSON, a missing, extra or wrong key, or a label or category that is not on the lists above). The batch is retried once with the same prompt. If it fails again, the script stops with a non-zero exit code and:
  - saves the raw responses under `failures/`;
  - writes nothing from that batch to the label file.
- **After a stop**, the only permitted fixes are:
  - Re-run with smaller batches and the same prompt. The re-batching is recorded in the manifest.
  - Change the prompt. Because the prompt's hash then changes, both labellers relabel every row from scratch, and the earlier output is moved to `archive/`.
- **No one ever edits or fills in a label by hand.**
- **Truth.**
  - If the two labellers agree, their label is the truth.
  - If they disagree, the row counts as `ambiguous` and is listed one by one.
  - Recall and reduction are reported three ways: on the rows where they agreed, with each labeller's labels, plus Cohen's κ.

## Thresholds are fixed before any result is seen

- The headline numbers use the live defaults: `genuine_min=0.60`, `genuine_max=0.40`, `spurious_min=0.60`.
- The threshold sweep is reference only. A threshold picked after seeing results never becomes a headline number.
- If the defaults turn out to send almost every row to the human, that fact is itself reported as a result.
