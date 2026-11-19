# PR-0 measurements — widened gate input (T-gate-diff-context-truncates-branch-bodies)

Status: **complete except (b)/(c)** (2026-11-20, Heisenberg). Design: Bohr v3.8 (msg-9790), go by
Einstein with no conditions. This file records what PR-0 has confirmed and what it
has not. Nothing here changes code or switches anything on.

Sources read for this file:

- spirrow-lexora `main` = `3e19e9052c0a29ecc94115afdcc3264fa8555586`
  (`src/lexora/backends/codex.py`, `src/lexora/api/routes.py`,
  `src/lexora/ingress/{gate,redact,context}.py`, `src/lexora/backends/fallback.py`).
- spirrow-cognilens `main`: `config.yaml`.
- spirrow-docs `main`: `docs/platform/infra-registry.md`.
- spirrow-mindwire `main` = `2c13bb05979fdd7c1f1f5ade9055c2cacec9e899`.
- Live probes from `{{HOST_LOOP}}` (the loop host), 2026-11-20.

## 1. Cognilens reachability (msg-9783 item 7, v3.5 §4)

| Check | Result |
|---|---|
| Host Cognilens runs on | `{{HOST_SERVICES}}` (port 8111 per [[platform:infra-registry]]), not the loop host |
| `config.yaml` bind | `host: "0.0.0.0"`, `port: 8111` — but production runs through `start.sh` (mcp-proxy / SSE), which is not in the repository; magickit's deploy registry health URL is `http://127.0.0.1:8111/sse` |
| `127.0.0.1:8111` from `{{HOST_LOOP}}` | nothing listening (curl `000`) — expected, Cognilens is on the other host |
| tailnet IP `:8111/sse` from `{{HOST_LOOP}}` | **connection timed out** (8 s) |
| LAN IP `:8111/sse` from `{{HOST_LOOP}}` | **connection timed out** (6 s) |
| Control: Lexora `:8110/health` on the same tailnet IP | 200, healthy |
| Control: Prismind `:8112/sse` (registry: localhost bind) | connection timed out — the same signature as Cognilens |

**Verdict: not reachable as it is.** Reaching it needs a change to the listen address
or the network settings on `{{HOST_SERVICES}}`, which widens a trust boundary.
Per msg-9783 item 7 this would go to a person before any implementation. (Design
v3.9 §1 then moved the parse route to Lexora, which is already reachable, so no
trust-boundary change arises; Einstein msg-9800 accepted that.) **A Cognilens
`custom_route` does not start.** Which of bind / firewall causes the timeout was
not determined (`start.sh` is unreadable to me, and `:22` on that host is closed).

## 2. Backstop: does any hop silently shorten an over-long input? (Obj-12-1)

### (a) Lexora code — confirmed by reading, at the commit above

- `request_to_prompt` (`codex.py` ~L814–837) flattens every message into one stdin
  prompt (`[role]\n<text>` joined by blank lines). No length check, no slicing, no
  message dropping.
- The prompt goes to `codex exec ... -` on stdin in full
  (`process.communicate(input=prompt.encode("utf-8"))`, ~L1361).
- `routes.py`: no slicing or dropping of `messages`; `max_tokens` is passed through.
- Ingress redaction (`ingress/redact.py`, ADR-14 §7.3) **replaces** matched spans with
  labels. It changes content by design; it does not trim by length. The only `[:n]`
  slices on that path are on log fields (`gate.py:311`) and webhook notices
  (`fallback.py`, 500 chars), not on the prompt.
- Failure mapping: a non-quota non-zero `codex exec` exit becomes `CodexFailed`
  (`classify_failure`, ~L722–738). The route layer maps backend failures to
  **HTTP 502** (e.g. `routes.py:689`, `:1044`). **AC-8's assumed "HTTP 400" is
  therefore probably wrong**; the fixture must come from (c), not from the design text.

**(a) = confirmed: Lexora does not trim the naysayer prompt.**

### (b) codex CLI / model behaviour on an over-window prompt — NOT confirmed

The pin is `gpt-6.1-sol`, `context_window=272000`, `max_context_window=872000`
(`codex.py` ~L269–277). Whether `codex exec` (codex-cli 0.160.0) with a first prompt
over the window fails, or auto-compacts / truncates, is not stated in the Lexora
code, and I have no official source for it on this host. Not confirmed.

### (c) Measured refusal — NOT run

Not run this turn. A deliberately over-budget prompt runs on the same codex
subscription the production naysayer uses; a usage-window failure puts codex on
hold (`codex.py` ~L1290, 15 min if the reset time is unknown) and would stall
live PR gates. It needs a quiet window to run. Pending.

**backstop_confirmed = false** (only (a) of three).

## 3. Calibration data (v3.7 E-2(i))

- `GET /stats/costs/recent?limit=500`: 500 rows back to 2026-11-18T05:33Z, of which
  **276** are successful `tier=naysayer`, `backend=codex` rows with `tokens_input`
  (min 5,732, max 62,145). So the ledger **does** carry real input token counts.
- But the ledger stores **no prompt size**, and mindwire does not persist the
  finished prompt's byte count per `trace_id` either (`n3_routing.py` computes
  `prompt_chars` only for the routing reason). The `bytes / tokens` pairs that
  E-2(i) needs cannot be formed from existing records.
- Note for whoever calibrates: `tokens_input` includes the codex harness's own
  prompt, so `bytes / tokens_input` understates bytes per token — conservative.

**calibration_available = false today.** Path to true: PR-2 logs the finished
prompt's UTF-8 byte count with its `trace_id` (switch-off-safe, it is a log line),
and the 30 samples accrue from normal gate traffic.

## 4. Byte-bound (v3.7 Obj-12-2)

Whether `gpt-6.1-sol` uses a byte-level BPE (every token covers ≥ 1 byte) has
**not been confirmed** from an official source; I have none on this host.
`o200k_base` remains an assumption, not a measurement.

**byte_bound_confirmed = false.**

## 5. EN (v3.8)

```
can_enable = (backstop_confirmed AND calibration_available) OR byte_bound_confirmed
           = (false AND false) OR false = false
```

The switch stays off. PR-2 may still be implemented and merged with the switch off
(v3.7 E-1).

## 6. Four-PR re-measurement — REFERENCE ONLY (old prototype)

**These numbers come from a scratch prototype that keeps 3 lines of context (U=3)
around every change, including changes between class members. D-A' (v3.9 §2) removes
that context, so these are NOT D-A' measurements.** The D-A' sizes — the report
msg-9783 item 6 asks for — are measured again with PR-2's real generator
(Bohr v3.10 Obj-14-3).

Method (scratch prototype, not committed):

- Diff = three-dot `base...head` of each PR (same as the gate). This repository
  has no `.naysayer-ignore`, so nothing is excluded.
- Alignment from `git diff -U<whole file>`; every changed line is widened to its unit
  on the new side (a deletion uses the new-side line it sits after), plus U=3.
- `.py`: innermost function incl. decorators; outside any function, the innermost
  direct member of the class body (**D-A**); a class is never a unit, so a line
  between members (blank line, comment) keeps only its U=3. Else the top-level
  statement. Unparseable → whole file (did not occur).
- `.md`: smallest paragraph / fenced code block / whole list / heading / other
  top-level block containing the change.
- Other files: whole file (did not occur in these four PRs).
- Finished prompts: `_build_messages` / `_build_pass2_messages` from `main` with the
  widened text, flattened exactly as Lexora's `request_to_prompt` does
  (`[role]
<text>` joined by blank lines). Coverage, round context, design
  conditions and ignore report were left empty, so **the prompt sizes are lower
  bounds** (round 1 of a PR without design conditions is the exact case).

| PR | raw U=3 chars | widened chars | widened UTF-8 bytes | pass-1 chars / bytes | pass-2 chars / bytes | E-2 with k=1 (≤ 145,536 bytes, ≤ 400k chars) |
|---|---|---|---|---|---|---|
| #216 | 27,396 | 34,649 | 34,712 | 48,061 / 48,202 | 49,018 / 49,982 | pass |
| #525 | 119,594 | 179,488 | 180,052 | 192,900 / 193,542 | 193,857 / 195,322 | **fail** (bytes) |
| #562 | 86,790 | 177,111 | 181,646 | 190,523 / 195,136 | 191,480 / 196,916 | **fail** (bytes) |
| #557 | 57,652 | 73,015 | 78,356 | 86,427 / 91,846 | 87,384 / 93,626 | pass |

By unit kind (widened chars): #562 = `.py` 173,663 + `.md` 3,448; #557 = `.py`
63,103 + `.md` 9,912; #216 and #525 are `.py` only.

Observations:

- **The class fallback matters.** With a class as the fallback for a line between methods, #525's
  `conductor/core.py` alone widened to 197,323 chars (one change sat on the blank
  line between two methods of the 3,514-line `Conductor`). With the class excluded (prototype: U=3 only between members) it is 22,391;
  this is not a D-A' figure.
- **Markdown by block works for #557:** `.md` widens 9,422 → 9,912 chars. The
  earlier 211k estimate came from sending `.md` files whole.
- #562 stays large because many functions change at once (`core.py` 59,223,
  `handoff.py` 53,896), as reported before.
- G-1 check on #216: both counter-evidence lines of msg-2442 §3
  (`pairs.append((ThreadFacts(...), pr))` and `for dropped in ...`) are in the
  widened input.
- **If EN were satisfied only through the byte-bound (k = 1), 2 of the 4 PRs
  (#525, #562) would fail input generation.** Characters (≤ 400k) never decide
  here; bytes do. v3.7 E-2(ii) said this result goes back to Takahito.

## 7. Not done yet

- (b) codex CLI behaviour on an over-window prompt; (c) a measured refusal (needs a
  quiet window on the shared codex subscription).
- Calibration samples (need PR-2's per-`trace_id` byte log).
