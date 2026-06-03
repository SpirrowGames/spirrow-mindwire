---
name: naysayer-review
description: Fire the independent naysayer (Gemini) PR review — the Stage 3 Tier B gate for a develop→main (or feature→main) pull request. Use when a mindwire PR needs the independent review gate before a human merge. Gathers the PR diff RAW, has the independent Gemini naysayer judge it, posts the critique to the magickit chatroom, and submits a GitHub PR review (APPROVE / REQUEST_CHANGES). Invoke as `/naysayer-review <PR ref>` (e.g. `/naysayer-review 82` or `/naysayer-review SpirrowGames/spirrow-mindwire#82`).
---

# naysayer-review

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

Argument `$ARGUMENTS` is the PR reference (a number like `82`, or a full
`owner/repo#n` / GitHub PR URL).

1. **Normalize the PR ref.** If only a number was given, expand it to
   `SpirrowGames/spirrow-mindwire#<n>` (or confirm the repo with `gh repo view`).
2. **Check preconditions** (the driver fails loud otherwise): the env vars
   `MINDWIRE_MAGICKIT_MCP_URL`, `MINDWIRE_LEXORA_URL`,
   `MINDWIRE_NAYSAYER_GITHUB_TOKEN` are set and the magickit chatroom MCP +
   Lexora are reachable from **this host**. (On `sg-ai-server-01` they are
   local; from a box without the chatroom MCP on `:8117` this will fail at the
   chatroom post — run it where the loop runs.)
3. **Run the driver** (it does the whole gate — raw diff → Gemini review →
   chatroom post → GitHub PR review submit):

   ```bash
   uv run python scripts/naysayer_review.py --pr <owner/repo#n>
   ```

4. **Relay the result verbatim.** Report the printed `VERDICT:` line and the
   critique back to the user / thread exactly as the naysayer wrote it. Do not
   edit it. The driver already posted to the chatroom and submitted the GitHub
   review — **do not duplicate** either.
5. If the driver reports no naysayer reply (Lexora/GitHub/chatroom unreachable),
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
