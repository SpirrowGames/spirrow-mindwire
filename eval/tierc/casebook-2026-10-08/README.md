# Tier-C casebook 2026-10-08 — self-negating and false-premise escalations

Thread: `T-tierc-overpass-self-contradicting-escalations`. Operator audit: msg-6653. Design: Bohr msg-6664, revised in msg-6666 (Einstein's objections in msg-6665), summarised in msg-6668. Einstein approved it in msg-6667 / msg-6669 and handed off to implementation. This directory is PR1: measurement only. Nothing in the conductor or the live hook changes.

**Status: prereg §6 exploration. Never pool with shadow.** This data is never part of `eval/tierc/shadow-prereg.md`'s count, labels or threshold decision. Truth here comes from the operator's audit (`truth_source = audit-operator`, `truth.json`), not from the two labellers. Some of these 7 rows may later appear in a shadow export. If so, nothing here affects that export.

## Headline

> **Neither B nor A holds → keep observing (msg-6666 §4-3).** The threshold 0.40 is unchanged.
> - **B (Jev, `tierc-v4-candidate`) cannot hold, and the cause is the input range.** msg-6606's “This escalation is premature.” starts at character 1648 of a 2,167-char body. Jev is given the first 500 characters, so Jev never saw it. The measured scores agree: 0.56 / 0.54 / 0.50, against the < 0.40 the rule needs.
> - **A (deterministic patterns) catches msg-6606, but fails its veto.** It also hits a consensus-genuine row in set (ii): spirrow-voxelworld msg-3116, “**∴ Not Tier-C.**”. That hit is in the author's own prose, not inside a quote or code. So the false positive is in the wording itself, not a Markdown artefact.
> - No PR2.

All the numbers are in `report.md`, which is generated and not edited by hand.

## Files

| file | rows | what |
|---|---|---|
| `truth.json` | 7 | the audit table (msg-6653), transcribed: truth, failure type, evidence messages; msg-6606's negating sentence |
| `casebook.jsonl` | 7 | per row: `msg_id` / `project` / `thread` / `author` / `label` / `jev_input` / `ask_score` / `truth` / `failure_type` / `evidence`, plus `body_sha256` / `body_chars` (the full body is not committed as its own field). `jev_input` is the logged `state_wire` string, copied, not rebuilt; it carries body extracts (see below). msg-6606 also records `negation_offset` / `negation_in_jev_input` |
| `genuine_fulltext.jsonl` | 37 | set (ii): the `fulltext-2026-09-28` rows whose consensus is genuine / genuine-merge / genuine-action: keys + `body_sha256`. Its projects are those of the fulltext fixture, including spirrow-playproof (`build.json` `set_ii_projects`); `--project` does not filter it |
| `next_human.jsonl` | 29 | set (iii): every message by a `[conductor.roster]` identity, 2026-10-03 00:00 JST → `as_of`, whose last `NEXT:` is `human`, over the `--project` list (`set_iii_projects`): keys + `body_sha256`. Bounced ones are included: the set is read from the chatroom, not from the gate |
| `build.json` | — | `since` / `as_of`, `set_ii_projects` / `set_iii_projects`, roster, log files, counts, sha256 of the build outputs |
| `selfneg.jsonl` | 73 | measurement A: per row, every hit with the line before and after and the display-only `looks` column |
| `jev.jsonl` | 42 | measurement B: 3 runs × 7 rows × (`tierc-v3`, `tierc-v4-candidate`), each with `request_sha256` and `decision_id` |
| `report.md` | — | the decision rule applied, and every table |

## Bodies and redaction (public repository)

No message body is committed as its own field, but **message text is committed in two places**:
- `casebook.jsonl` `jev_input`: the logged `state_wire` holds `head_summary` and up to 5 `recent_events[].body_head`, each the first 500 characters of a message (`BODY_HEAD_M`, `src/spirrow_mindwire/decider/hook.py`). A message of 500 characters or fewer is therefore committed in full; a longer one, its first 500 characters. This is what Jev was given, so it is kept verbatim (after `redact_infra`, below).
- `selfneg.jsonl`: the hit lines and the lines around them (next paragraph).

Otherwise the set files carry keys and the sha256 of each original body. `measure-a` fetches every body again from the chatroom (read-only) and stops on any hash mismatch. Only the hit lines and the lines around them reach `selfneg.jsonl`. This also keeps the PR under the PR-gate's review cap.

This repository is public. Before any text is written here, `redact_infra` replaces:
- every IPv4 address with `<ipv4>`;
- the user name in a Windows profile path with `<user>`. `redact_infra` matches either separator, single or doubled (`C:\Users\<name>`, `C:/Users/<name>`, and JSON-escaped `C:\\Users\\<name>`); `build.json` records the rule as `C:/Users/<name>`.

This follows the C-42 convention of `T-real-infra-values-egress-from-agent-context`: report placeholders, not values. Every redacted text keeps the sha256 of its original (`jev_input_sha256`, `body_sha256`).

Measurement A runs on the fetched bodies after redaction. Its hits are identical to an unredacted run, because no pattern involves these values. Measurement B sent the **original** logged `state_wire`: `measure-b` re-reads it from the conductor log and stops unless it matches `jev_input_sha256`. Only that `state_wire` comes from the log. Each `request_sha256` is the sha256 of the request this measurement built and sent: the log-restored `state_wire` plus the variant's question set, built from the registered `tierc_rules.toml`. The log does not contain these requests. The 21 `tierc-v4-candidate` requests were never sent live; the 21 `tierc-v3` requests were sent for this measurement under the replay policy and are not compared with any live request. What reproduces is the recomputation: rebuilding all 42 requests from the log-restored `state_wire` and the two question sets gives every recorded `request_sha256` (checked 2026-10-08: 21/21 `tierc-v3`, 21/21 `tierc-v4-candidate`).

## How it was produced

```
uv run python scripts/tierc_casebook.py build --dir eval/tierc/casebook-2026-10-08 \
  --log <data_dir>/logs/conductor-2026-*.log ... \
  --project spirrow-mindwire --project spirrow-lexora --project spirrow-magickit \
  --project spirrow-prismind --project spirrow-verimend --project spirrow-voxelworld \
  --project spirrow-playproof \
  --since 2026-10-03T00:00:00+09:00 --as-of 2026-10-07T21:00:48+00:00
uv run python scripts/tierc_casebook.py measure-a --dir eval/tierc/casebook-2026-10-08   # read-only chatroom
uv run python scripts/tierc_casebook.py measure-b --dir eval/tierc/casebook-2026-10-08 \
  --rules <data_dir>/config/tierc_rules.toml \
  --log <data_dir>/logs/conductor-2026-*.log ...
# --rules: its sha256 must equal the prereg §2 value; --log: the original state_wire is re-read there
uv run python scripts/tierc_casebook.py report --dir eval/tierc/casebook-2026-10-08
```

The set (iii) projects are the 6 in `sweep.json`, plus spirrow-playproof, which appears in the fulltext fixture. The chatroom has no project-listing tool, so this is every project known to either source. Adding playproof added no rows. `measure-b` uses the default seed (`20261008`) and policy `mindwire.replay.tierc`, the replay policy, so these calls do not mix into the live tally.

## Observations — not decisions

- **The candidate sentence moves genuine rows down too.** On `tierc-v4-candidate`, msg-6288 (genuine) scores 0.41–0.48, and its 0.41 is one hundredth above the cutoff. msg-754 (false premise) moves from about 0.67 to about 0.54. The sentence lowers scores across the board; it does not separate self-negation from other rows. The input-range finding explains why: Jev never saw the sentence the new rule refers to.
- **A set (iii) row worth a look:** spirrow-mindwire msg-6357 (Bohr) contains 「Tier-C ではないので、こちらで決めます」 and still resolves to `NEXT: human`. It is unlabelled, so no decision reads it. Whether it is a second self-negation case is for the operator to judge.
- **msg-754 (false premise)** is out of Jev's scope by design (msg-6664 §3). Its scores are recorded for comparison only.

## Limits

- N is tiny: 1 self-negation row and 1 false-premise row. Nothing here moves a threshold.
- Set (iii) is chosen by `parse_token == human` on roster authors. A `NEXT: human` from a non-roster author is not in it. It starts at 2026-10-03 00:00 JST, a few hours before bounce mode took effect at 11:43 JST, so it is a superset of the bounce period.
- Proposals to widen the live 500-char input are out of this thread's scope (msg-6666 supplement; fulltext README condition (3)).
- **Not read by the implementer:** the Lexora repository's `config/lexora_config.yaml` (msg-754's evidence is taken from the audit as written), and the body of ADR-2026-05-29-13.
