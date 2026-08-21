# S3 — Claude Code backend for the decision-request composer

**Status**: implementation SOT (single source of truth). This file — not a chatroom
message — is what `OBL-READBACK-ENTRY` reads before touching S3 code. If a later
Tier-C decision in the chatroom changes S3, that decision has no force until this
file is updated in the same PR. Rationale in §Provenance.

**Scope**: adds the `claude-code` backend to
`spirrow_mindwire.decision_request` — one adapter behind the port shape defined in
S1. Does not change the port, the on-disk envelope, or the PowerShell wrapper's
public seams. S1 (subpackage skeleton + stub + CLI + value objects), S2 (PS
wrapper wiring + truncation ladder), and S4 (parked-humans digest, D-32 rebuild)
are merged in PR #168 and are treated as immutable ground.

---

## Provenance

Assembled from what remains readable in-window:

- **msg-1370** (thread head, founding Tier-C) — port contract §2, I-1…I-5,
  A-1…A-5, F-1/F-2, non-goals §7. Full text available.
- **msg-1391 §13** — S1→S2→S4→S3→S5'→S6' order and the D-32 rebuild rule.
- **msg-1396 §14** — D-33 (stdout ASCII discipline). S3 inherits the same
  principle for the NEW subprocess boundary it introduces (D-43 below).
- **msg-1403 §17** — S3 is unblocked; live-read-back of `next_participant`
  is out of scope for S3.
- **msg-1405 §19** — this file's precursor: §19.4 is the S3 spec, §19.3 is the
  rule that puts it in the repo, §19.5 lists what could not be verified from
  in-window context (§12.5 and D-19…D-25/D-22 unreadable).
- **msg-1406** — Einstein's naysayer response requiring the file be SOT (not a
  mirror). Adopted here.
- **msg-1370 §0 Tier-C `NEXT: human` continuation**: Takahito's msg-1400 §16
  and msg-1403 §17 accepted these as binding. Tier-C msg (parent thread head).

What is **not** available:

- msg-1387 §12.5 (the original S3 spec), msg-1379..msg-1386 (D-19..D-31 body
  text). The subpackage code cites D-22 from msg-1384 as "output structure
  intent" but the body is unreadable. This file is a fresh spec assembled from
  msg-1370 alone; it does not claim to reproduce §12.5.

Conflict rule: **file wins**. If a subsequent chatroom message contradicts
this file, an implementer must update this file in the same PR (or stop and
report the contradiction) — otherwise there is no binding S3 spec change.

---

## D-35 — form of the backend: headless subprocess, single shot

One `claude` process per stop. No persistent daemon, no long-lived SDK session,
no re-use across stops. I-3 already bounds invocation to one per
`reason:last_msg_id` signature; a daemon would give nothing back for that
constraint and complicates the failure modes (a stuck daemon, a leaked session,
a state race between two stops).

Concretely: `subprocess.run(..., timeout=…)`. The 30-second wall clock ceiling
is inherited from S2 (`$DecisionComposerTimeoutSeconds`) — no new constant.

## D-36 — the child gets no tools

`--allowedTools ""`, `--disallowedTools "*"`, no MCP configuration. This is
not a performance choice; it is how I-1 ("the composer does not decide") and
I-5 ("neutral identity") are enforced by execution environment rather than by
prompt convention. A tool-less child cannot post to a chatroom, cannot merge a
PR, cannot query anything — the "does not decide" guarantee is structural.

## D-37 — the child does not inherit role context

- `settings-source` disabled (or its equivalent flag on the CLI in use — the
  concrete flag name is the composer implementation's concern; the property
  is "the child does not read `CLAUDE.md` and does not load persona settings
  from the repo").
- `cwd` set OUTSIDE the repo (system temp dir; no writing — just a launch
  directory). Rationale: `claude` walks upward from `cwd` looking for
  `CLAUDE.md` and any settings; launching inside the repo would pull in every
  role's persona instructions, which erases the one reason case B was picked
  (an independent, neutral author).
- `env` scrubbed to a minimal PATH — no `MINDWIRE_*` variables and no
  `ANTHROPIC_*` toggles are propagated blindly. Membership check against
  the allowlist is **case-insensitive on the key** because some
  environments (non-CPython Pythons, WSL bridges) yield `os.environ`
  keys in their OS-native case (`Path`, `SystemRoot`, `AppData` on
  Windows); a case-sensitive check against an uppercase allowlist would
  silently strip those and the child would fail to spawn or lose
  Windows' own CreateProcess subsystems. The allowed KEYS are kept in
  their original OS case for the child, so a native tool sees the same
  environment shape it always does.
- **Proxy environment (D-44 — Tier-C msg §24)**: `HTTP_PROXY`,
  `HTTPS_PROXY`, and `NO_PROXY` are on the allowlist. On the
  sg-ai-server-01 deploy host, the ONLY route out of the box to
  `api.anthropic.com` is through a squid proxy exported via these three
  variables. Without them the child `claude -p` fails INSIDE the CLI
  with `terminal_reason:"api_error"` and `duration_api_ms:0` (the API
  call never leaves the machine); that failure fails-open through I-2
  to the raw ping, so nothing screams and the composer silently
  produces no questions in production while CI stays green (§14-shape
  bug: stub-only tests cannot see it). D-37's intent is to strip ROLE
  CONTEXT and TOOLS — a proxy is neither role context nor a tool, it
  is an egress route, so preserving it does not weaken the neutrality
  argument. Case-insensitive membership picks up the POSIX lowercase
  forms (`http_proxy` / `https_proxy` / `no_proxy`) as well.
- `argv_digest` recorded to `envelope.extras.argv_digest`
  (`sha256(" ".join(argv))[:16]`), so an operator can retroactively confirm
  "yes, that stop ran under the neutral setup, not with role context leaked
  in". The separator is a literal space so the digest is reproducible by
  hand: `echo -n "$argv" | sha256sum | head -c 16`.

## D-38 — tail is fetched Python-side and passed to the child as text

The child never talks to a chatroom (D-36). The Python parent fetches the
tail via `chatroom_get_thread` — the same tool S4's
`scripts/parked_humans.py` already uses — and renders it into the child's
user prompt as text.

- **Tail count is chosen by the wrapper**, plumbed as `--tail N`.
  `$DecisionComposerTailLimit` (currently 5) lives in one place — the PS
  wrapper — and is not duplicated in Python.
- **Per-body character cap**: 4000 chars, applied to each individual message
  body before assembly. Rationale: 5 × 4000 = 20 000 char worst case, which
  is well below any prompt ceiling and still fits an entire long naysayer
  message. Bodies dropped by this cap end with `… (省略)` markers.
- Truncated bodies are counted first; the body cap fires **before** any
  count reduction. Reducing the count silently would break F-1's "was N
  enough?" diagnostic.
- The wrapper records to `envelope.extras`:
  - `tail_count`: how many messages were actually included
  - `tail_chars`: sum of character lengths after per-body capping
  - `tail_truncated`: `"true"` iff any body was cut by the per-body cap
    (`"false"` otherwise — always present so a diff is readable)
- Tail contents include, per message: `msg_id`, `author`, `body`. Thread
  title and the parked-message author are always in scope for the model
  because F-1's "the question does not name the thread's subject" rubric
  cannot be satisfied without them.

## D-39 — prompt = one system + one user, no few-shot

No examples. Examples pull the question's shape toward the example's shape,
which is exactly the disease case A (the parked role writes the question) was
rejected for on the other end — a shape bias from the WRITER's perspective.
Same disease, different vector.

`prompt_version` recorded to `envelope.extras.prompt_version` so a future
retrospective can bind output quality to a prompt revision without archaeology.

**System-prompt properties** (the property list is normative; the concrete
wording is the implementation's):

1. You do not decide. You phrase.
2. At least 2 options, each with `id`, `label`, `gain`, `loss`.
3. Exactly one recommendation (or none). If given, its reason MUST cite a
   concrete fact from the tail (a msg-id, a number, a quoted phrase). General
   platitudes ("A is safer overall") violate the rule.
4. Unknowns are declared as unknown. Do not fill.
5. Output is JSON only, matching the specified schema. Prose outside JSON
   is a violation.

**User-prompt shape**:

```
project: <project>
thread: <thread_title> (<thread_id>)
last message: <last_msg_id>, author=<parked_author>, stop_reason=<stop_reason>
rounds: <rounds>

tail (last <tail_count> of <total_messages> messages; body cap=<cap>):

--- msg-<id> by <author> ---
<body (capped)>

--- msg-<id> by <author> ---
<body (capped)>

...

Task: read the tail and produce the decision-request JSON described in the
system prompt.
```

## D-40 — identity_name stays "Composer" (default), unregistered in magickit

`DecisionComposerIdentity = 'Composer'` continues to be the wrapper default
(env-overridable). The composer does NOT post to any chatroom in S3 — the
envelope is written to `pending-decisions.json` and the Discord message is
rendered by the PS wrapper. Registering "Composer" in magickit's identity
registry now would create the expectation that it can post, which is false
until (at earliest) S5'/S6' add a posting path.

I-5's requirement (composer identity is not one of `Bohr` / `Heisenberg` /
`Einstein`) is satisfied by the default value and not additionally enforced
in code — the wrapper's operator config is the enforcement surface.

## D-41 — all failure modes fail-open (I-2)

Every failure of the child process is mapped to `ComposerStatus.EMPTY` or
`ComposerStatus.ERROR` (never `ok`) with a non-empty `error` string. The CLI
still exits 0 and the wrapper falls back to the raw ping. Specifically:

| Failure mode                                                   | Envelope status           |
|----------------------------------------------------------------|---------------------------|
| `subprocess.TimeoutExpired` at the 30 s ceiling                | `TIMEOUT`                 |
| Non-zero `returncode`                                          | `ERROR`                   |
| `FileNotFoundError` (claude CLI missing)                       | `ERROR`                   |
| stdout not valid JSON                                          | `ERROR`                   |
| stdout JSON missing the model text                             | `ERROR`                   |
| Model text not valid JSON matching the schema                  | `ERROR`                   |
| Parsed JSON: `question` empty                                  | `EMPTY`                   |
| Parsed JSON: fewer than 2 options                              | `EMPTY`                   |
| Parsed JSON: `recommendation` not in `options[].id`            | `ERROR` (`from_json` raises) |

The `EMPTY` category is reserved for "the composer ran but had nothing to
say". `ERROR` is "the composer broke". Both cause the wrapper's raw-ping
path (I-2).

## D-42 — cost / latency recorded; no automatic tripwire

The child's structured output (via `--output-format json`) carries a
`duration_ms`, `total_cost_usd`, `num_turns`, and `model` field (or the
CLI's equivalents; keys are normalised on the way into extras). These land
verbatim in `envelope.extras`:

- `duration_ms`, `total_cost_usd`, `num_turns`, `model`

**The model name is never hard-coded in Python** (msg-1370 §2's port
neutrality) — it comes from what the child reported it used.

**F-2's cost tripwire is not implemented in S3.** Setting a threshold
before any real measurements exist would be speculation, and speculation
close to §7's non-goals list. The extras are the data feed a later
tripwire could use.

## D-43 — child stdout decoded explicitly as UTF-8

S3 introduces a NEW subprocess boundary (Python → `claude` CLI). §14's
cp932 mojibake attack surface reappears here, in the OPPOSITE direction:
if the parent (Python) uses `text=True` or `encoding=None` on
`subprocess.run`, the platform default (cp932 on the Windows deploy host)
will silently corrupt any Japanese in the child's output. §14's fix (D-33)
closed the same hole on the Python → PowerShell boundary; D-43 closes it
on the Python → `claude` boundary. Same principle: **structural, not
env-dependent**.

Concretely:

```python
result = subprocess.run(
    argv,
    input=user_prompt.encode("utf-8"),
    capture_output=True,          # returns bytes
    cwd=self._cwd,
    timeout=self.timeout_seconds,
    env=self._make_child_env(),
    # DELIBERATELY NOT setting text=True / encoding= .
)
stdout_text = result.stdout.decode("utf-8", errors="replace")
stderr_text = result.stderr.decode("utf-8", errors="replace")
```

The regression test lives in `tests/test_claude_code_composer.py` and
follows §14.3's rule: **no `capsys`**. It hands a fixed byte sequence
containing multi-byte UTF-8 (the same Japanese phrase §14 used to trigger
the bug) directly to a fake runner and asserts the composer round-trips
the string byte-identically.

## Extras (envelope) — the full list S3 populates

Everything is a string (envelope `extras` is `dict[str, str]`). Absent
values are simply not written; a wrapper reader must tolerate missing
keys.

| key                   | example                                    | source              |
|-----------------------|--------------------------------------------|---------------------|
| `backend`             | `"claude-code"`                            | ClaudeCodeComposer  |
| `model`               | `"claude-3-5-sonnet-20241022"`             | child's JSON        |
| `duration_ms`         | `"14320"`                                  | child's JSON        |
| `total_cost_usd`      | `"0.0287"`                                 | child's JSON        |
| `num_turns`           | `"1"`                                      | child's JSON        |
| `argv_digest`         | `"1f2c…" (16 hex)`                         | ClaudeCodeComposer  |
| `prompt_version`      | `"1"` (bumped per prompt revision)         | ClaudeCodeComposer  |
| `cwd`                 | `"/tmp"` or platform equivalent            | ClaudeCodeComposer  |
| `tail_count`          | `"5"`                                      | CLI (post-fetch)    |
| `tail_chars`          | `"18932"`                                  | CLI (post-fetch)    |
| `tail_truncated`      | `"true"` or `"false"`                      | CLI (post-fetch)    |

**Extras carrier**: since S3's own instruction says "port も value object
も PS 側も形を変えない" (do not change the port or value objects), the
composer instance exposes its per-call extras via a `last_extras: dict[str, str]`
attribute (populated in `compose()`). `compose_once` in the CLI reads
`getattr(composer, "last_extras", {})` and copies it into the envelope.
Backends that do not care about extras (`StubComposer`) do not need the
attribute.

## CLI additions (mindwire-compose-decision)

Additive only:

- `--backend claude-code` — selects `ClaudeCodeComposer`.
- `--tail N` — if `N > 0`, the CLI fetches `N` tail messages from
  `chatroom_get_thread` before invoking the composer, overriding the
  `tail` array in the input JSON. If `N = 0` (default), the payload's
  `tail` is used as-is (stub/tests keep working; S2 wiring keeps working).
- `--body-cap C` — per-body character cap for D-38's per-message trim.
  Default 4000.
- `--claude-cli PATH` — override for the `claude` executable path. Default
  `"claude"`. Used by tests and by deployments that pin a specific binary.
- `--cwd PATH` — override for the child's cwd (D-37). Default: system
  temp dir. Tests pass a dedicated path.

## PS wrapper additions

Also additive:

- `Get-DecisionEnvelope` now passes `--tail $DecisionComposerTailLimit` to
  the CLI when the backend is `claude-code`. The stub path is unchanged
  (still `--tail 0`, so the stub keeps its S2 behaviour).
- No new PS-side tail fetching. The wrapper does not learn a new tool.

## Acceptance (§5 subset applicable to S3)

- **A-1** (from §5 of msg-1370): a real `NEXT: human` stop produces a
  Discord message with a self-standing question and options that pass
  the F-1 rubric (see below). Recorded post-merge; not gated in CI.
- **A-2** (from §5): with `MINDWIRE_DECISION_COMPOSER_BACKEND=claude-code`
  AND the `claude` CLI intentionally broken (e.g. path set to a missing
  binary), the raw ping still fires. Covered by
  `Test-DecisionComposerWiring.ps1` (Invoke-ComposerCli returns
  `ok=$false` → cache stays empty → Format-DecisionMessage returns
  `$null` → wrapper uses `RawFallback`). No PS change needed.
- **A-3** (from §5): unchanged from S2. Signature dedup fires **before**
  the CLI is invoked; S3 does not touch that layer. Covered by
  `Test-DecisionComposerWiring.ps1` at the existing "one CLI invocation
  across two same-signature reads" check.
- **A-4**: unchanged from S4.
- **A-5**: `bash .mindwire-gate` green.
- **A-18** (Tier-C msg §24 — added post-#169-review): a real chatroom
  parked-`NEXT: human` thread is fed to `--backend claude-code` on the
  deploy host, and the resulting envelope carries `composer_status=ok`,
  a non-empty `question`, and `options.length >= 2`. **Stub envelopes
  do not substitute.** This exists because §14 (Windows cp932 mojibake)
  and D-44 (proxy scrub) are both bugs the CI-stub path is structurally
  blind to; A-18 is the acceptance gate that runs the actual backend
  end-to-end. Additionally, `envelope.extras.duration_ms` is recorded
  so the 30-second `DEFAULT_TIMEOUT_SECONDS` ceiling has real data
  behind it — Tier-C msg §24.5 explicitly reserved raising the ceiling
  to itself, so an implementer who sees the real run brush against 30 s
  reports it and does NOT bump the constant unilaterally.

**A-18 execution constraint**: A-18 requires the `claude` CLI and network
egress to `api.anthropic.com`. It CANNOT be run from a mindwire-impl
implementer session (no `claude` binary, no chatroom API access). It is
run on the deploy host, from the parked scheduler, and its result is
posted to the thread and pasted into the PR body before merge. Not
gated in CI.

**F-1 rubric** (msg-1405 §19.4 restated for future S3-observability post-merge):

1. Question names the thread's specific subject (not a generic
   "confirmation required").
2. At least 2 options, distinguishable labels, per-option gain/loss.
3. Recommendation reason cites a tail-specific fact.
4. Unknowns non-empty OR an explicit "none".

If any fails, do NOT increase N automatically — report what was missing
per msg-1370 F-1. This spec fixes N at whatever the wrapper is currently
set to (5) and treats a wider tail as a separate decision.

## Testing (unit + regression pins)

New test file: `tests/test_claude_code_composer.py`. All tests inject a
fake subprocess runner into `ClaudeCodeComposer` — no test spawns the real
`claude` CLI, none hit the network.

Coverage:

1. Happy path — fake runner returns a well-formed JSON envelope
   containing our composer JSON in `result` → `compose` returns a valid
   `DecisionRequestOutput` and `last_extras` contains `backend`,
   `model`, `duration_ms`, `total_cost_usd`, `num_turns`,
   `prompt_version`, `argv_digest`, `cwd`.
2. D-37 — the runner records the argv, cwd, env it was called with;
   asserts cwd is outside the repo root, argv contains the disable-tools
   flags and the empty settings source, env has no `MINDWIRE_*` keys.
3. D-41 — parameterised over every failure mode listed in the table
   above; each maps to the right exception subclass.
4. D-43 (bytes UTF-8 pin) — fake runner returns a fixed byte sequence
   containing the same Japanese phrase §14 used ("斜面撤去") as the
   composer's question in its embedded JSON; the composer's returned
   `DecisionRequestOutput.question` contains "斜面撤去" character-identical.
   No `capsys` involved (§14.3).
5. D-38 tail rendering — user prompt contains every tail body, capped
   at `body_cap` when a body exceeds it, and both cap markers and the
   `--- msg-<id> by <author> ---` separators appear.
6. Prompt version — `PROMPT_VERSION` constant is a module-level string
   ("1"), and the extras key `prompt_version` reports its value verbatim.
7. `compose_once` propagates `last_extras` to envelope.extras when the
   composer has that attribute (duck-typed); envelopes from
   `StubComposer` remain empty extras.

For the CLI additions:

8. `--backend claude-code` with a mocked runner produces an envelope
   with `composer_status=ok`, `identity_used=Composer` (default), and
   the extras keys above.
9. `--tail N` with a mocked chatroom_get_thread returns N tail
   messages, overriding payload tail. Payload total_messages is
   preserved when the mocked fetch reports it.
10. `--tail 0` (default) uses the payload's tail — S2 behaviour is
    unchanged.

For the PS side:

11. `Test-DecisionComposerWiring.ps1` gets one new check: when the
    backend name is `claude-code`, `--tail 5` is present in the CLI
    argv (assertion on the constructed Arguments string).

## Non-goals (S3 does not do these)

- Register `Composer` in magickit's identity registry (§16.1). S3 does
  not post; registration would misrepresent capability.
- Wire the consumer for `next_participant` field (§16.4). Out of scope
  and gated by live read-back that hasn't cleared.
- Add automated cost limits (D-42 rationale).
- Change the truncation ladder (S2 owns it). S3 verifies only that the
  dashboard-link reservation still fits — if it doesn't, add a single
  reservation constant (~120 chars); do not rewrite the ladder.
- Change the on-disk envelope shape. `extras` was already declared as
  `dict[str, str]` in S1; S3 just populates keys.
