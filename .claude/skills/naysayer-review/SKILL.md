---
name: naysayer-review
description: Fire the independent naysayer (Gemini) PR review — the Stage 3 Tier B gate for a pull request into `main`. Use when a mindwire PR needs the independent review gate before a human merge. Gathers the PR diff RAW, has the independent Gemini naysayer judge it, posts the critique to the magickit chatroom, and relays the verdict into the design thread, then submits a GitHub PR review (APPROVE / REQUEST_CHANGES). Invoke with **two** values — `/naysayer-review <PR ref> <design thread>` (e.g. `/naysayer-review 82 T-the-thread-this-gate-was-fired-from`); the design thread is required, not optional.
---

# naysayer-review

> **実インフラ値**（ホスト名 / IP / パス）は [[platform:infra-registry]] が正本。この文書は `{{PLACEHOLDER}}` で参照する（規約 §3.1）。

Fires the **independent naysayer review** (ADR-05 §5 / ADR-07 Tier B) for a
pull request. The judgment is made by the **Gemini** naysayer (a different model
family from `main`), not by you.

## Independence contract — read before running

You (this Claude Code session) are the **gatherer and relay, never the judge**.
Your model is the same family as the implementer, so any judgment *you* add
would defeat the independence the naysayer exists to provide.

- **Do not review the PR yourself.** Run the driver; let the Gemini naysayer
  decide. Relay its critique + verdict **verbatim** — do not summarize, soften,
  reorder, or "correct" it.
- **Pass raw primary source only.** The driver hands the naysayer the raw PR
  diff. Never substitute a curated/summarized digest — the naysayer must read
  the primary source and form its own judgment.
- **Do not let the PR author hand-pick context.** If you are the session that
  authored the PR, that is fine for *firing* the driver (it gathers raw
  deterministically), but you must not inject your own framing of what to look
  at.

## How to fire

`$ARGUMENTS` is **two** values:

1. the **PR reference** — a number like `82`, or `owner/repo#n`, or a PR URL;
2. the **design thread** — the chatroom thread the gate is being fired *from*.

Both are required by the driver. If only one was given, ask for the other —
**do not guess a design thread and do not substitute the
`T-pr-review-<repo>-<n>` ledger id** (see step 5b for why they are different
threads).

1. **Normalize the PR ref.** If only a number was given, expand it to
   `SpirrowGames/spirrow-mindwire#<n>` (or confirm the repo with `gh repo view`).
2. **Confirm the design thread.** It is the design/work thread whose turn this
   gate serves — the place the verdict has to arrive for the loop to move.
3. **Check preconditions** (the driver fails loud otherwise): the env vars
   `MINDWIRE_MAGICKIT_MCP_URL`, `MINDWIRE_LEXORA_URL`,
   `MINDWIRE_NAYSAYER_GITHUB_TOKEN` are set and the magickit chatroom MCP +
   Lexora are reachable from **this host**. (On `{{HOST_SERVICES}}` they are
   local; from a box without the chatroom MCP on `:8117` this will fail at the
   chatroom post — run it where the loop runs.)
4. **Run the driver** (it does the whole gate — raw diff → Gemini review →
   chatroom post → verdict relay → GitHub PR review submit):

   ```bash
   uv run python scripts/naysayer_review.py --pr <owner/repo#n> --design-thread <the design thread you are firing from> --project <chatroom project>
   ```

   `--project` defaults to `spirrow-mindwire`; pass it when firing for another
   project. `--design-thread` has no default — omitting it is `error: the
   following arguments are required`, exit 2, before anything is billed.

5. **Relay the result verbatim.** Report the printed `VERDICT:` line and the
   critique back to the user / thread exactly as the naysayer wrote it. Do not
   edit it. The driver already posted to the chatroom and submitted the GitHub
   review — **do not duplicate** either.
5b. **Say where the verdict is, in the design thread — one line, never the critique.**
   The driver's chatroom post goes to the `T-pr-review-<repo>-<n>` **ledger**; the
   design thread the gate was fired from is a different thread. Post there:
   ``verdict は `T-pr-review-<repo>-<n>` の msg-NNN にある（VERDICT: X）``. This is
   not a duplicate of anything step 5 names — it is a pointer, not the chatroom
   post and not the GitHub review — and **copying the critique body there is
   still forbidden**: an in-family relay of a Tier B judgement is exactly what
   the independence contract above exists to prevent. Skip it only when the
   driver printed a non-empty `relay=` (it already did this for you); a
   `relay=DROPPED` line (exit 2) means it did not, so do it by hand.
6. If the driver reports no naysayer reply (Lexora/GitHub/chatroom unreachable),
   surface the error; do **not** substitute your own review.

## Notes

- **Model**: the naysayer tier routes to **Gemini** (plain `generateContent`,
  **no tools / grounding / cached content** — a deliberate data-governance gate).
  This is why the naysayer is fed a context bundle rather than being an agent.
- **Scope (v1)**: reviews the PR **diff** only. A richer context bundle (design
  threads / ADRs / changed-file context) is the follow-up *bundle builder*
  (T-stage3-loop-wiring msg-385 §4).
- **Identity**: the GitHub review is submitted as `spirrowgames-ops` (the
  naysayer identity, distinct from the PR author `takahito-spirrowgames`), so it
  is not a self-review. APPROVE is a *necessary* condition for Takahito's merge
  GO (Tier C), not sufficient.

## Where this command line is written down — and which copy is canonical

Read this before editing the command above, or before copying it somewhere new.

- **Canonical: the `argparse` block in `scripts/naysayer_review.py`.** It is the
  only *executable* statement of which flags exist and which are required. Every
  piece of prose — including this file — is a mirror of it and can be wrong.
- **Checked mirrors: exactly two, both in this repository.**
  1. `scripts/naysayer_review.py`'s module docstring (`Run::` block);
  2. this file's fenced command line, step 4.

  `tests/test_gate_command_doc_consistency.py` parses the `argparse` block with
  `ast` (it never imports the driver — that would drag in the gate stack and can
  cost a billed call) and fails if either mirror omits a required flag, **or if a
  third runnable command line appears anywhere in the repo**. The count is
  pinned on purpose: a third copy is the next thing to drift. Fold a new copy
  into one of these two, or make it a pointer to them.
- **`~/.claude/**` (the user-level `naysayer-review` and `mindwire-operator`
  skills) cannot be a mirror, and must not spell out flags.** CI runs on
  `ubuntu-latest` with only `actions/checkout`, so that directory does not exist
  on the runner: a check over it would be vacuously green (worse than none — it
  would look enforced) or permanently red. No machine check here can cross the
  repository boundary, so those copies should point at the driver or at this
  file rather than restate the flags. A flag written there is unverifiable by
  construction, and that is exactly how the divergence this section exists to
  prevent was produced: #244 made `--design-thread` required and updated no
  prose anywhere.
