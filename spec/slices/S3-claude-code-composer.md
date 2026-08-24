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
- **msg-1442 §28** — Bohr's v2 prompt design (D-46 rev2 / D-47 / D-48 rev2 /
  D-49 / D-50 rev2 / D-51 / D-52). Adopted here.
- **msg-1441** — Einstein's naysayer objections (three points on the v2
  draft: rule-3 branch structure, character-count regression, garden-path
  sentences). Response in msg-1442 §28 folds two in full, addresses the third
  by adding D-52 (independent-sentence rule) rather than a schema key.
- **msg-1461** — Tier-C GO on the v2 prompt design and the URL-material
  requirement (§3 of that message, folded into D-53).
- **msg-1462 §29 / msg-1464 §30** — Bohr's D-53 rev2 (URL rule with a
  lexical trigger). msg-1464 §30 supersedes §29 for D-53's specific rule
  text.
- **msg-1463** — Einstein's objection to D-53's original ontological
  trigger; folded into D-53 rev2 (msg-1464 §30).
- **msg-1461** (second reference) — Tier-C GO on D-53 rev2 and freeze.

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

Concretely: `subprocess.run(..., timeout=…)`. The 60-second wall clock ceiling
is inherited from S2 (`$DecisionComposerTimeoutSeconds`) — no new constant.
(D-45 raised the ceiling from 30 s; see the D-45 note below.)

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
wording is the implementation's — the v1 prompt property list, kept for
historical reference; the v2 revision layers D-46 rev2 through D-53 rev2 on
top of these five, described in §D-46..§D-53 below):

1. You do not decide. You phrase.
2. At least 2 options, each with `id`, `label`, `gain`, `loss`.
3. Exactly one recommendation (or none). If given, its reason MUST cite a
   concrete fact from the tail (a msg-id, a number, a quoted phrase). General
   platitudes ("A is safer overall") violate the rule.
4. Unknowns are declared as unknown. Do not fill.
5. Output is JSON only, matching the specified schema. Prose outside JSON
   is a violation.

## D-46 rev2 — internal thread labels are restated on first use (msg-1442 §28.2)

**Problem this closes**: the v1 prompt let the model use thread-internal
labels (`D-0`, `F-1-C`, `CF-1`, phase names, slice ids, gate names) as if
the reader already knew what they meant. The Discord reader has NOT read
the thread; those labels arrive as opaque tokens. msg-1461 §2's before
example — "D-0 調査を候補 A (`FFieldRegularizeParams`) を対象として続行しますか…"
— is the concrete failure.

**Rule (verbatim intent — implementer adapts wording to the existing prompt's
numbering; D-50 rev2 forbids verbatim copy)**: the first time the model
uses a label that only carries meaning inside the thread, it MUST take one
of these two branches, chosen by what the tail actually says:

- **(a)** the tail states what the label refers to → restate it in plain
  words alongside the label.
- **(b)** the tail does NOT state what the label refers to → say only how
  the thread USES the label, note that the thread never defines it, and
  add the label to `unknowns`.

**Why the branch is in the rule, not in a separate safety net (Einstein
msg-1441 #1)**: an affirmative imperative ("explain the label") will
overrule a distant negative constraint ("do not invent"). The uncertainty
allowance MUST live in the same rule as the explanation imperative,
because LLMs prioritise nearby action verbs over distant prohibitions.
The forced (a)/(b) branch removes the option to invent.

**Why rule 3 and rule 4 (D-46 rev2 and D-51) share the same shape**: the
same failure mode applies to code identifiers (§D-51 below). Both use the
identical (a)/(b) branch so the model sees one pattern rather than two
different mechanisms.

## D-47 — "hallucinating is worse than jargon" — rationale note (msg-1442 §28.2)

**Position**: this is NOT an independent safety-net rule. It is a
rationale sentence embedded in the prompt near D-46 rev2 / D-51 (spec
§D-51) that names the trade-off. Enforcement is done by the (a)/(b)
branches; D-47 exists so a model that reads the rules and asks "why?"
sees the answer nearby (LLMs are known to comply better with rules
paired with immediate rationale). The v1 iteration placed D-47 as a
standalone Hard Rule; that positioning failed by Einstein's mechanism
(msg-1441 #1) because the "explain it" imperative in rules 3 and 4
sat between "explain" and the distant "do not invent". Fold, don't
stack.

## D-48 rev2 — no character-count targets (msg-1442 §28.3)

**Problem this closes**: v1's original v2 draft carried a "~600 characters
for question, ~120 for label" soft budget. Einstein msg-1441 #2 flagged
that any numeric character target is read by LLMs as a compression
mandate — the model will drop the very explanations D-46 rev2 / D-51
were introduced to protect, because it is easier to hit a character
target by summarising than by prioritising a qualitative rule.

**Rule**: the prompt states "there is no length limit and no length
target". The rationale that survives in the prompt is:
"understandability beats brevity". The physical Discord budget
(`$DecisionMessageDiscordBudget`, currently 1950) is enforced downstream
in the wrapper's truncation ladder — the model does not need to know it.

**What is NOT dropped**: a sentence-count floor and a soft sentence
ceiling. `question` has "at least 2 sentences" and "around 6 sentences
is usually enough". Rationale (msg-1442 §28.3): brevity pressure operates
on characters WITHIN a sentence (jargon buys characters, not sentences).
A sentence-count floor pushes in the opposite direction — it cannot
reward compression. A minimum of 2 sentences is also the direct
opposite of the v1 "one-line question" wording; removing it entirely
would leave room to regress to "1 line" behaviour.

**Ceiling is soft and self-releasing**: the ceiling ("around 6 sentences
is usually enough") is paired with an explicit release valve — "if you
are past that, check … but do NOT delete an explanation to get under
it." The floor is what enforces D-48 rev2; the ceiling is a comfort
hint that must never be enforced by deleting an explanation.

**Registered cost (msg-1442 §28.3)**: output length rises; the risk of
brushing the `DEFAULT_TIMEOUT_SECONDS` ceiling (A-20) and the risk of
Discord truncation (A-21 rev2) both rise. This is the explicit trade-off
of §7 (understandability > brevity). Do not restore a character target
by the back door.

## D-49 — sha256 pin of the prompt text tied to `PROMPT_VERSION` (msg-1442 §28.6)

**Problem this closes**: `prompt_version` in `envelope.extras` is a
runtime observability invariant. A retrospective that groups by
`prompt_version` is only usable if the version string and the prompt
text remain bound. A silent edit to the prompt text without a version
bump would poison every downstream analysis.

**Implementation**: the module carries a `PROMPT_DIGEST_V2` constant
alongside `PROMPT_VERSION`. Its value is
`sha256(_SYSTEM_PROMPT.encode('utf-8')).hexdigest()`. A single test
(`TestPromptDigestPin` in `tests/test_claude_code_composer.py`) asserts
the pin. Updating the prompt requires a coordinated edit:
`PROMPT_VERSION` bumps, `PROMPT_DIGEST_V2` (or a next-version constant)
recomputed, in the SAME commit.

**Why this pin is not "just an arbitrary constant"** (msg-1441 endorsement
of D-49 mechanism): the pinned rejected v1 timeout constant was arbitrary
because timeout is a measurement, not an invariant. `prompt_version` IS
an invariant, because its whole reason for existing in extras is that
downstream analysis relies on its stability. Pinning the digest protects
the invariant.

**What this pin explicitly does NOT do**: it does not assert any content.
Text-based assertions on prompt content (e.g. "the word `explain` appears
in rule 3") would be false comfort (msg-1442 §28.6) — the stub backend
never exercises the real LLM, and no test at this layer can verify
whether the model actually complies with a prompt rule. The pin is the
only new test introduced with the v2 revision.

## D-50 rev2 — schema keys and copy discipline (msg-1442 §28.4.1, msg-1464 §30)

**Split into three parts, because the original D-50 argument was
over-broad**:

1. **Renaming or deleting a schema key is prohibited.** The wrapper
   parses envelopes against the S1 shape; a removed or renamed key
   produces a parse failure that fails-open to the raw ping (I-2) —
   silent functional loss, exactly the failure mode §14 exhibited.
2. **Adding a schema key is deferred for v2.** Not because it is
   necessarily fatal (whether the parser tolerates unknown keys is
   unverified, and D-50 rev2 does not require it to be verified), but
   because: (i) today's reader-facing surfaces are the Discord message
   and `pending-decisions.json`; the Discord formatter is unaware of a
   new key, so any content placed in one would not reach the reader
   who receives the notification; (ii) magickit's decision page is
   in-flight (`T-decision-page`), and coupling a schema change to that
   in-flight design introduces cross-repo entanglement that S3's PR
   is explicitly not scoped for (§Non-goals).
3. **Verbatim copy of the msg-1442 §28.5 or msg-1464 §30.2 prompt
   fragments is prohibited.** The composer's own wording carries the
   same function; a verbatim paste treats the specifying messages as
   the SOT, which contradicts §Provenance and D-34 (this spec file is
   the SOT). The functional match is what binds; the wording is the
   implementation's.

**Reconsideration trigger (msg-1442 §28.4.4)**: if A-19 rev2 fails with
reason (ii) — "the explanations are there but the sentences are too
tangled to follow" — the inline-explanation approach itself is at its
limit, and the correct next step is a `glossary` field with the Discord
formatter and magickit's decision page updated in coordination. That
scope change is a Tier-C call, not a further prompt iteration.

## D-51 — code identifiers on first use are grounded from the tail or marked as unknown (msg-1442 §28.2)

**Problem this closes**: the v1 prompt allowed the model to name a code
identifier (`FFieldRegularizeParams`) without ever saying what the
identifier does. The Discord reader saw a bare identifier and could
not tell whether the composer knew what it was or was guessing.

**Rule (verbatim intent — implementer adapts to the prompt's numbering
scheme, D-50 rev2 §3)**: the first time the model uses a code identifier
(type, function, flag, filename), it MUST take one of these two
branches, chosen by what the tail actually contains:

- **(a)** the tail states what the identifier does → say what it does,
  in plain words, next to the identifier.
- **(b)** the tail does NOT state what the identifier does → name how
  the thread USES the identifier, mark the gap in the same sentence,
  and add the identifier to `unknowns`.

**The prompt must also state that the model has not seen the code**, so
that (b) is understood as the ordinary case, not a failure mode.
Writing (a) when only (b) is supported is the worst outcome under
these rules: the reader cannot distinguish an inspection from a guess.

**Same-shape justification**: D-46 rev2 (thread labels) and D-51 (code
identifiers) intentionally share the (a)/(b) shape, so the model sees
one pattern rather than two. Einstein #1 (msg-1441) flagged the label
side; the code-identifier side has the same failure mechanism and takes
the same fix.

## D-52 — explanations live in their own sentences (msg-1442 §28.4.3)

**Problem this closes**: D-50 rev2 §2 keeps explanations inline (no
`glossary` key). Einstein #3 (msg-1441) warned that inline restatements
tend to be injected via em-dashes and parentheticals, producing
garden-path sentences that are just as unreadable as the original
jargon, only in a different way.

**Rule**: each explanation goes in its own short sentence. Dashes,
brackets, and parentheses MUST NOT be used to stack a definition
inside an outer sentence. One idea per sentence. The prompt carries
a WRONG example (nested definitions) and a RIGHT example (separated
sentences) so the model has a concrete anchor.

**Interaction with D-48 rev2**: D-52 costs sentences — the prompt
states this explicitly ("explaining costs sentences, not clauses.
Spend the sentences"). A sentence-count cap that penalises D-52 would
partially undo it, which is why D-48 rev2 states the ceiling as a
comfort hint with a release valve rather than a hard cap.

**Interaction with D-53 rev2 (see §D-53 below)**: D-52 says the last
sentence of `question` is the question itself. D-53 rev2 (a) appends
a URL to the end of the question. The URL is not a sentence, so
attaching it to the final sentence (space-separated) does not violate
"one idea per sentence" or "the last sentence is the question". This
is an application-time convention, NOT a change to D-52.

## D-53 rev2 — carry a URL when the tail supplies one; never fabricate (msg-1464 §30)

**Problem this closes**: msg-1461 §3 records that a merge request for
"PR #171" arrived with no repository identifier and no URL. The
recipient had to search for it. The decision material is missing its
target's location.

**Trigger (lexical, not ontological — Einstein msg-1463)**: this rule
applies ONLY when the decision target is a pull request, an issue, a
ticket, or a dashboard. It does NOT apply to source files, functions,
design choices, scope calls, or schedules. The trigger is closed to
that four-item list on purpose: most decisions in this project are
about source files or design choices, and the rule was originally
written with "a file" among the triggering examples. Einstein predicted
(msg-1463) that a broader ontological trigger would fire on every
codebase discussion and inject "the thread never gives the full link"
noise into `question` and `unknowns` on the majority of stops. The
list is narrow so silence is the default outcome; when in doubt, do
not fire.

**Rule (verbatim intent)** for a triggering decision target:

- **(a)** the tail contains the FULL url starting with `https://` →
  copy it EXACTLY, character for character. Place it at the end of the
  final sentence of `question`, preceded by a single space, with
  NOTHING attached after it (no closing bracket, no period, no comma).
  Do not wrap it in brackets or markdown. Do not shorten it. Do not
  "fix" it.
- **(b)** the tail names it only by number or short reference (`#171`,
  `PR 171`, `issue 42`) and gives NO full url → write the reference
  exactly as it appears, state in the same sentence that the thread
  never gives the full link, and add the reference to `unknowns`.
- **NEVER build a url from a number.** The composer does not know
  which repository or which host owns the number. A fabricated url
  opens something, and the reader believes it is the right thing.
  That is worse than giving no link at all.

**Silence otherwise (D-53.6 rev2)**: for decisions outside the four-item
list, or triggering decisions where the tail contains no reference at
all, the prompt says NOTHING about links. Do not add "no url" to
`unknowns`. Do not mention that a link is absent. This is what makes
Einstein #3's "meta-noise" prediction not fire.

**Placement (D-53.4 / D-53.5)**: the URL lives at the end of the final
question sentence in `question`. No new schema key is added (D-50 rev2
§2). No URL is placed in `options[].label` / `gain` / `loss` (repeats
would consume the Discord budget for no additional information).

**Format (D-53.2)**: bare absolute URL, `https://...`, no markdown
brackets. Rationale: markdown auto-linking in Discord is not
guaranteed by the current wrapper, and a bare URL is the widest-support
form that both Discord and any later decision-page renderer can link
against.

**Reserved list (msg-1464 §30.5)**: the trigger list excludes `commit`
and `file` deliberately. Both can have URLs, but neither has appeared
as a decision target in this thread's measured history, so including
them would raise the misfire rate without raising the hit rate. If a
future stop makes a commit the actual decision target, adding it is a
one-word prompt edit with a `PROMPT_VERSION` bump.

## Prompt-version bump policy (D-49 corollary)

Every prompt-text edit is a two-line change: `PROMPT_VERSION` moves and
`PROMPT_DIGEST_V<N>` gets a new value (either the same-versioned
constant updated, or a new-versioned constant added alongside a new
`TestPromptDigestPin` case). Do the edits in the SAME commit as the
prompt text change. A prompt edit committed without a digest bump is
what the D-49 pin exists to reject.

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
| `subprocess.TimeoutExpired` at the 60 s ceiling (see D-45)     | `TIMEOUT`                 |
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

## D-45 — wall-clock ceiling raised 30 s → 60 s (Tier-C msg §25.2)

The original ceiling was 30 s, derived from S2's stub (measured <1 s) and
what looked like a comfortable margin for an LLM call. A-18 (§25.1)
measured the real end-to-end elapsed at **33,812 ms** on a live parked
thread (`spirrow-voxelworld/T-T227-P0-spec-kickoff`, tail 6 msgs /
21,026 chars). 30 s would time out on real inputs while staying green on
stub tests — exactly the CI-blind failure mode §14 / D-44 exhibit.

Decision (Tier-C msg §25.2):

1. Raise the ceiling in **all three sites** to 60 s. They MUST match:
   - `DEFAULT_TIMEOUT_SECONDS` in `src/spirrow_mindwire/decision_request/claude_code.py`
   - `$DecisionComposerTimeoutSeconds` in `deploy/run-conductor-scheduled.ps1`
   - `--timeout-seconds` default in `src/spirrow_mindwire/decision_request/cli.py`
     (currently sourced from `DEFAULT_TIMEOUT_SECONDS` at import time — do
     not hard-code a second literal).
2. **Do NOT trim the tail** to buy latency. A-18 confirms the 21 KB input
   produces a high-quality question (F-1 rubric satisfied). Trading
   quality for latency defeats the reason case B (independent composer)
   was chosen over case A.
3. Cost: composer failure adds up to 60 s of notification delay before I-2
   fires the raw ping. Human response latency is 8-11 h (msg-1370 §1). The
   extra 30 s is negligible against that background.
4. **If 60 s starts brushing** on a future measurement, do NOT raise the
   ceiling further unilaterally. Report to Tier-C — the tail cap may need
   to be lowered instead, and that is a Tier-C trade-off (Bohr's original
   D-5 chose 60 s under the same logic; going above it changes the
   trade-off's shape).

Test coverage: existing unit tests pin `timeout_seconds=30` explicitly in
the two D-41 timeout cases; those constants are arbitrary test doubles
and do NOT track the default. Adding a pin on the default itself would
make every future ceiling change a two-file edit for no diagnostic gain;
the three-site synchronization requirement above is the invariant that
matters, and the D-45 note in each site is its documentation.

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
  so the wall-clock ceiling (`DEFAULT_TIMEOUT_SECONDS`) has real data
  behind it. Tier-C msg §24.5 reserved raising the ceiling to itself;
  §25.2 executed that reservation (see D-45) — an implementer who sees
  the real run brush against the current ceiling still reports it and
  does NOT bump the constant unilaterally.
  - **A-18 pass on record** (Tier-C msg §25.1, 2026-08-21): input
    `spirrow-voxelworld/T-T227-P0-spec-kickoff` (msg-2511, tail 6 /
    21,026 chars) → `composer_status=ok`, `error=None`, `tail_used=6`,
    `omitted_count=54`, stdout pure-ASCII (D-33 also confirmed). Real
    elapsed = **33,812 ms**. Question and 3 options (A/B/C) with
    per-option gain/loss, recommendation A cites the actual thread
    (msg-2510 §0), F-1 rubric satisfied, 7 explicit unknowns. This
    result is the evidence base for D-45.
- **A-19 rev2** (msg-1442 §28.6, endorsed msg-1461 §2): a real parked
  `NEXT: human` stop is composed twice — once with `PROMPT_VERSION=1`
  and once with `PROMPT_VERSION=2` — and both outputs are shown to the
  human judge (Takahito, per msg-1461 §2). The judgement is qualitative:
  can the reader tell from the after-version what is being asked,
  without opening the thread? Fail-diagnosis is REQUIRED — the human
  returns not only the labels/identifiers they could not follow, but
  the distinction between **(i)** unfamiliar terms and **(ii)** terms
  are explained but the sentences are too tangled to follow. (i) and
  (ii) have opposite fixes (msg-1442 §28.4.4 / D-50 rev2 reconsideration
  trigger). If A-19 rev2 fails with reason (ii), do not iterate the
  prompt further — the correct next step is a `glossary` field, which
  is a Tier-C scope call. **Baseline for after-comparison** (msg-1461
  §2, 2026-08-22): the v1 real run produced
  "D-0 調査を候補 A (`FFieldRegularizeParams`) を対象として続行しますか…"
  and the concrete complaint was "D-0 が何なのかが分からない" /
  "`FFieldRegularizeParams` が何なのかわからない". The v2 run is
  compared against that.
  - Not gated in CI. Runs on the deploy host end-to-end.
- **A-20** (msg-1442 §28.6): `envelope.extras.duration_ms` is reported.
  Baseline is 40,213 ms (Tier-C §2, 2026-08-22). The v2 prompt removed
  the character-count target, so output length rises and elapsed is
  expected to rise. If a timeout fires under v2, do NOT raise
  `DEFAULT_TIMEOUT_SECONDS` unilaterally (see D-45 clause 4 and
  msg-1442 §26.2 / §28.6). Report the distribution to Tier-C instead.
- **A-21 rev2** (msg-1442 §28.6, msg-1461 §3, msg-1464 §30.3): the real
  Discord message body from a triggering stop is captured and inspected
  against the `$DecisionMessageDiscordBudget` (1950 chars). Three
  observations are recorded:
  1. Did the question survive?
  2. Did the option labels survive?
  3. If the decision target was a triggering type (D-53 rev2) AND the
     tail contained a full URL, did that URL survive? If the decision
     target was NOT a triggering type OR the tail had no URL, this
     item is recorded as "not applicable" (silence is the correct
     D-53.6 rev2 outcome, and a missing "not applicable" note is
     indistinguishable from a URL that was truncated away).
  **Also record what was NOT survived** (msg-1464 §30.3): Einstein
  msg-1463's minor observation predicts URL may be safer than option
  labels because it lives on `question` (which prints first). If
  labels dropped while URL survived, that is the material for R-4
  (truncation-ladder design) and evidence the R-5 concern (URL
  disappears silently) was over-weighted. Do not alter the truncation
  ladder based on A-21 alone — feed R-4 with the observation.
- **A-22** (msg-1442 §28.6, §28.7 renumbered): `bash .mindwire-gate`
  green. This is what "A-5" (§5 of msg-1370) referred to; renumbered
  in the v2 acceptance set for clarity.

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
   ("2" as of the v2 revision), and the extras key `prompt_version`
   reports its value verbatim.
6a. **Prompt digest pin (D-49)** — `TestPromptDigestPin` computes
    `sha256(_SYSTEM_PROMPT.encode('utf-8')).hexdigest()` and asserts it
    equals `PROMPT_DIGEST_V2`. This is the ONLY new test introduced by
    the v2 revision (msg-1442 §28.6). Content-oriented assertions on
    the prompt text are DELIBERATELY not added: the stub backend does
    not exercise the real LLM, so any test at this layer that claimed
    to verify prompt compliance would be a false comfort (§28.6, §14
    / §24 same-shape principle).
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
