"""Scope-clarified RE-naysayer for a single PR (one-shot, fail-closed).

Same independence machinery as ``naysayer_review.py`` (ADR-05 §5: gather raw diff →
different-distribution model judges → relay verbatim), with ONE addition: an adjudicated
SCOPE block is prepended to the user message so the independent reviewer judges the PR
under the binding scope a proposer (Bohr) has already set — instead of re-deriving the
same out-of-scope objections every cycle.

This is used when a prior naysayer REQUEST_CHANGES has been adjudicated (the objections
re-scoped to a later stage as binding acceptance criteria) and the proposer asks for a
scope-clarified fresh re-review limited to: "is the change ITSELF sound to land?".

Invariants preserved (naysayer discipline):
- the diff is fetched RAW from GitHub and passed UNTOUCHED (no curation),
- the verdict is the independent model's (Gemini ``naysayer`` tier), parsed with the SAME
  injection-safe :func:`~spirrow_mindwire.naysayer.pr_review.decide_verdict` (last
  standalone VERDICT line; never APPROVE on a truncated / length-capped / timed-out review),
- the L1 CI-gate runs first (fail-closed),
- the GitHub review is submitted as the SEPARATE ``spirrowgames-ops`` identity,
- the caller does NOT edit the verdict — the scope is framing context, not a verdict hint.

Run::

    uv run python scripts/naysayer_review_scoped.py \
        --pr SpirrowGames/Spirrow-VoxelWorld#52 \
        --scope-file C:/path/to/scope.md
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from spirrow_mindwire.github.client import (
    CiState,
    GitHubClient,
    GitHubHTTPError,
    ReviewEvent,
    naysayer_github_token,
    parse_pr_ref,
)
from spirrow_mindwire.lexora.client import (
    ChatMessage,
    LexoraClient,
    LexoraTimeoutError,
)
from spirrow_mindwire.naysayer.pr_review import (
    _DEFAULT_MAX_TOKENS,
    _DEFAULT_TIMEOUT_SECONDS,
    _PR_REVIEW_SYSTEM_PROMPT,
    _ci_gate_response,
    _make_diff_view,
    decide_verdict,
)
from spirrow_mindwire.naysayer.principles import (
    NAYSAYER_MODEL_TIER,
    build_preamble,
    principles_version,
)

_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8", errors="backslashreplace")


def _build_messages(text: str, pr_slug: str, scope: str) -> list[ChatMessage]:
    """Build the scoped-review messages from a (possibly truncated) diff ``text``.

    Truncation is done by the caller via :func:`_make_diff_view` so the pre-truncation
    length is captured for the gate-notice path — this function only formats.
    """
    system = f"{build_preamble()}\n\n{_PR_REVIEW_SYSTEM_PROMPT}"
    user = (
        f"Review the diff for pull request {pr_slug}. Critique it, quoting the "
        f"specific hunks you object to, and end with your VERDICT line.\n\n"
        f"=== BINDING SCOPE FOR THIS REVIEW (set by the proposer; adjudicated) ===\n"
        f"{scope}\n"
        f"=== END SCOPE ===\n\n"
        f"```diff\n{text}\n```"
    )
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user),
    ]


async def main() -> None:
    parser = argparse.ArgumentParser(description="Scope-clarified re-naysayer for one PR.")
    parser.add_argument("--pr", required=True, help="PR ref: 'owner/repo#n' or URL")
    parser.add_argument("--scope-file", required=True, help="file with the binding scope block")
    parser.add_argument("--no-submit", action="store_true", help="skip the GitHub review submit")
    args = parser.parse_args()

    pr = parse_pr_ref(args.pr)
    if pr is None:
        print(f"[scoped-naysayer] could not parse PR ref: {args.pr!r}", file=sys.stderr)
        sys.exit(2)
    with open(args.scope_file, encoding="utf-8") as fh:
        scope = fh.read().strip()

    lexora = LexoraClient(timeout_seconds=_DEFAULT_TIMEOUT_SECONDS)
    github = GitHubClient(naysayer_github_token())
    try:
        # L1 CI-gate (fail-closed): never review content while CI is not green.
        ci = await github.fetch_ci_status(pr)
        if ci.state is not CiState.SUCCESS:
            verdict, body = _ci_gate_response(ci, pr.slug)
            print(f"[scoped-naysayer] CI not green ({ci.state.value}); verdict={verdict.value}")
            print(body)
            return

        diff = await github.fetch_pr_diff(pr)
        view = _make_diff_view(diff)
        print(
            f"[scoped-naysayer] CI green, diff={view.original_chars} chars "
            f"(truncated={view.truncated}); firing {NAYSAYER_MODEL_TIER} "
            f"(Gemini, billed) with scope context ..."
        )
        try:
            completion = await lexora.chat_completion(
                model=NAYSAYER_MODEL_TIER,
                messages=_build_messages(view.text, pr.slug, scope),
                max_tokens=_DEFAULT_MAX_TOKENS,
            )
        except LexoraTimeoutError as exc:
            print(f"[scoped-naysayer] TIMEOUT (fail-closed REQUEST_CHANGES): {exc}")
            sys.exit(3)

        body = (completion.content or "").strip()
        if not body:
            print(
                f"[scoped-naysayer] EMPTY reply (finish_reason="
                f"{completion.finish_reason!r}) — refusing to relay",
                file=sys.stderr,
            )
            sys.exit(4)
        decision = decide_verdict(body, view=view, finish_reason=completion.finish_reason)
        verdict = decision.gate_verdict

        print("\n===== GEMINI VERDICT (verbatim) =====")
        print(body)
        print("===== END GEMINI VERDICT =====")
        print(
            f"\n[scoped-naysayer] parsed verdict={verdict.value}  "
            f"finish_reason={completion.finish_reason!r}  "
            f"model={completion.model or NAYSAYER_MODEL_TIER}  "
            f"principles_version={principles_version()}  head={ci.head_sha}"
        )

        if args.no_submit:
            print("[scoped-naysayer] --no-submit: skipping GitHub review submission")
            return
        try:
            await github.submit_review(pr, event=verdict, body=body)
            print(f"[scoped-naysayer] GitHub review submitted as spirrowgames-ops: {verdict.value}")
        except GitHubHTTPError as exc:
            if exc.status_code == 422 and "own pull request" in str(exc).lower():
                await github.submit_review(pr, event=ReviewEvent.COMMENT, body=body)
                print("[scoped-naysayer] 422 own-PR → submitted as COMMENT (verdict in body)")
            else:
                raise
    finally:
        await lexora.aclose()
        await github.aclose()


if __name__ == "__main__":
    asyncio.run(main())
