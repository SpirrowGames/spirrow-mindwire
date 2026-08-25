# Reader inventory for `identity.independence_class` + detector query spec

- **thread**: `T-role-null-must-become-impossible` (spirrow-mindwire chatroom)
- **spec source**: msg-1487 §5, msg-1489 §1 (readers must be enumerated before any `null` is written),
  msg-1493 §5 (detector = "登録済み ∪ 理由付き保留 = scope, 無説明残余 0")
- **scope**: this file is the **read half's** DoD #8″ deliverable — enumerate every reader of the
  `identity.independence_class` field in this repo so the write half can decide whether any
  reader would crash when that field is nullified for a machine identity.

## What msg-1489 §1 asks for

The revised posture, verbatim from Bohr:

> null で壊れる読み手は**読み手の側の誤り**であり、domain model はそれに合わせて曲げない。
> ... 規則を書き直す: **列挙 → 読み手を直す → null を書く。Phase 1 内の順序であって、Phase 1 の中止ではない。**

Enumeration is unconditional; fixing broken readers is required before writing; the domain model
does not defer to a brittle reader.

## Enumeration — result: **zero readers in this repo**

Method: `grep -r 'independence_class' src/` (only source; excluding tests, docs, spec).

```
$ grep -r 'independence_class' src/
(no matches)
```

Widened to case-insensitive `independence` across `src/`:

```
$ grep -ri 'independence' src/
src/spirrow_mindwire/adapters/claude_code_sdk.py:281 (docstring)
src/spirrow_mindwire/adapters/naysayer_sdk.py:370   (error string)
src/spirrow_mindwire/adapters/naysayer_sdk.py:516   (comment)
src/spirrow_mindwire/adapters/naysayer_lexora.py:6  (module docstring)
src/spirrow_mindwire/conductor/core.py:547          (docstring)
src/spirrow_mindwire/dispatcher/registry.py:5       (module docstring)
src/spirrow_mindwire/dispatcher/registry.py:23      (comment)
src/spirrow_mindwire/dispatcher/registry.py:75      (docstring)
src/spirrow_mindwire/loop_runner.py:257             (error message)
src/spirrow_mindwire/loop_runner.py:318             (docstring)
src/spirrow_mindwire/ports.py:160                   (docstring)
src/spirrow_mindwire/value_objects.py:48            (docstring)
```

Every occurrence is prose (docstring, comment, or error message) about the *concept* of naysayer
independence, not a read of the `identity.independence_class` field.

The mechanism this repo uses to keep the naysayer independent is the `NAYSAYER_QUALIFIED`
capability on adapters (`value_objects.Capability.NAYSAYER_QUALIFIED`,
`dispatcher/registry.py::qualified_for`) — capability-gated at adapter registration time, not
per-post via a query on the identity store. So no code path here would notice a null
`independence_class` on a machinery identity: none of it queries the field.

## What this DOES NOT enumerate

- **Readers outside this repo.** Any downstream service that reads Prismind identity records
  directly (magickit backend query paths, dashboards, metric scripts, third-party analytics) is
  outside this file's grep scope. Under `OBL-DECLARE-UNREADABLE`, I cannot enumerate readers I
  cannot read. The write-half implementer MUST reproduce this enumeration on the identity-store
  repo(s) before enabling the guard.
- **The magickit chatroom server's own use of the field.** The docstring in
  `orchestrator.py:302-311` describes magickit as validating `role` against `allowed_roles`
  before writing; it does not describe how / whether magickit reads `independence_class`. That
  read may or may not exist and must be verified on the magickit side.

## Detector query spec — `message.role × identity.allowed_roles` join

From msg-1484 §3, the 2×2 (plus one) truth table this detector reports counts for:

| identity                          | `message.role`         | judgement          |
| --------------------------------- | ---------------------- | ------------------ |
| `allowed_roles ≠ ∅` (participant) | `null`                 | **defect** (supply gap) |
| `allowed_roles ≠ ∅`               | non-null               | **normal** (verified)   |
| `allowed_roles = ∅` (machine)     | `null`                 | **normal**              |
| `allowed_roles = ∅`               | non-null               | **defect** (fabrication)|
| identity record not present       | any                    | **undetermined**        |

### Where this join runs

Not in this repo. The `identity` table (Prismind) is not held here; this repo only writes chatroom
messages via the magickit MCP. The detector is therefore a **spec that a query script runs against
the identity store**, either in the Prismind/magickit repo (native) or by pulling both sides
through magickit MCP read tools (if such a tool exists for the identity table — a magickit-side
check).

### What THIS repo's read half CAN produce

`scripts/identity_findings.py` produces **half of the detector's inputs**: the per-identity
observed role set. It does this by iterating `chatroom_list_threads` → `chatroom_get_thread`
across a caller-supplied project list, since a caller-supplied cutoff msg-id / timestamp, and
counting `(author, role)` pairs on every message.

That half plus the classification file (`spec/identity/legitimate_roles.yaml`) plus a caller-side
read of the identity store's `allowed_roles` is enough to compute the five-way judgement above,
so the read half here is not blocking on the identity-store read tool: whoever runs the detector
combines the two halves.

### Scope-bounded cutoff (msg-1484 §4)

Bohr's msg-1484 §4 pinned the range:

> **範囲は「deploy 以降に post した author」に限る**（過去に存在した全 identity ではない。
> **測れる集合に閉じる**）。

`deploy` here = PR #153's merge commit `13618e9` (2026-08-17). The script's `--since` defaults
to that timestamp; overrideable so a re-measurement after a later fix can re-scope. Anything
before the cutoff is ignored — the historical `null`s stay honest (msg-1179 §6 point 2, msg-1487
§7's "履歴 backfill は依然禁止" carry-forward).

## Requirement-vs-artifact table

| Spec requirement (msg-id, ¶)                                                                     | Reflected here?                                                                                        |
| ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| msg-1489 §1: enumerate readers of `independence_class` in this repo                             | Yes — zero readers (enumeration + method)                                                              |
| msg-1489 §1: fix broken readers before writing null, do NOT let a brittle reader veto the model | Vacuous here (no readers). The rule is restated so a future reader gets added to a repo bound by it.   |
| msg-1491 §3: readiness lock = "null-非対応の読み手が 0 件"                                       | Vacuously satisfied for this repo. Not satisfied by definition for external repos not read here.      |
| msg-1484 §3: detector reports the 5-way judgement                                               | Spec here + inputs half via `scripts/identity_findings.py`; the join runs where identity records live |
| msg-1484 §4: scope = "deploy 以降に post した author", not "全 identity"                         | Yes — the script defaults `--since` to PR #153's merge commit; live-corpus scope-bounded              |
