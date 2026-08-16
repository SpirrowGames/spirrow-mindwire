"""Tests for the conductor NEXT-handoff parser + resolver (msg-520 / msg-522 Obj3)."""

from __future__ import annotations

from spirrow_mindwire.conductor.handoff import (
    HUMAN_TOKEN,
    NONE_TOKEN,
    Handoff,
    HandoffKind,
    build_handoff_protocol_block,
    parse_next_token,
    resolve_handoff,
)
from spirrow_mindwire.value_objects import Role

_ROSTER = {"Bohr": Role.PROPOSER, "Heisenberg": Role.IMPLEMENTER, "Einstein": Role.NAYSAYER}


def test_no_authorisation_marker_is_parsed_from_message_bodies() -> None:
    # The DELEGATE marker that used to grant design→implement autonomy per thread is gone;
    # authorisation is per-project state (conductor/control.py), not something written into a
    # message. Pinned as a test because a marker re-introduced here would be a *second* source of
    # the same truth, and the two would drift the moment they disagreed.
    import spirrow_mindwire.conductor.handoff as handoff_mod

    assert not [name for name in dir(handoff_mod) if "delegate" in name.casefold()]


# --------------------------------------------------------------------------- #
# parse_next_token
# --------------------------------------------------------------------------- #


def test_parse_plain_token() -> None:
    assert parse_next_token("blah blah\n\nNEXT: Heisenberg") == "Heisenberg"


def test_parse_strips_cjk_parenthetical_gloss() -> None:
    # The real msg-520 handoff: a CJK-paren gloss follows the name.
    body = "design...\n\nNEXT: Einstein（独立 naysayer review）→ Tier-C"  # noqa: RUF001
    assert parse_next_token(body) == "Einstein"


def test_parse_strips_ascii_parenthetical_and_trailing_punct() -> None:
    assert parse_next_token("NEXT: Bohr (disposition ...)") == "Bohr"
    assert parse_next_token("NEXT: human.") == "human"


def test_parse_takes_last_next_line() -> None:
    # An earlier quoted NEXT (e.g. inside a relayed critique) must not win over the final handoff.
    body = "I am relaying a review that ended with\nNEXT: human\n\n...my reply...\nNEXT: Bohr"
    assert parse_next_token(body) == "Bohr"


def test_parse_none_when_absent() -> None:
    assert parse_next_token("a reply with no handoff line at all") is None
    # 'NEXT' must be the 'NEXT:' directive on its own line, not prose mentioning next.
    assert parse_next_token("the next thing we do is unclear") is None


# --------------------------------------------------------------------------- #
# resolve_handoff — PR-gate sentinel (PR-2b-2)
# --------------------------------------------------------------------------- #


def test_resolve_pr_review_sentinel() -> None:
    h = resolve_handoff("opened the PR\n\nNEXT: pr-review acme/widgets#7", _ROSTER)
    assert h.kind is HandoffKind.PR_REVIEW
    assert h.token == "acme/widgets#7"  # the whole ref, not just a leading name


def test_resolve_pr_review_case_insensitive_and_strips_surrounding_space() -> None:
    h = resolve_handoff("x\n\nNEXT:  PR-Review   owner/repo#42  ", _ROSTER)
    assert h.kind is HandoffKind.PR_REVIEW
    assert h.token == "owner/repo#42"


def test_resolve_pr_review_accepts_a_url_ref() -> None:
    url = "https://github.com/acme/widgets/pull/7"
    h = resolve_handoff(f"done\n\nNEXT: pr-review {url}", _ROSTER)
    assert h.kind is HandoffKind.PR_REVIEW
    assert h.token == url


def test_resolve_bare_pr_review_without_ref_falls_through_to_absent() -> None:
    # "pr-review" with no ref is not the PR-gate sentinel (the regex needs a ref); it falls through
    # to a roster lookup, which fails → ABSENT (route to human), not a half-formed gate fire.
    h = resolve_handoff("oops\n\nNEXT: pr-review", _ROSTER)
    assert h.kind is HandoffKind.ABSENT


def test_resolve_pr_review_strips_trailing_gloss() -> None:
    # LLMs often append a parenthetical gloss; the ref is the single non-whitespace token, not the
    # whole remainder of the line — else an invalid ref reaches GitHub (Tier B PR #103 round 2).
    h = resolve_handoff("done\n\nNEXT: pr-review acme/widgets#7 (please review)", _ROSTER)
    assert h.kind is HandoffKind.PR_REVIEW
    assert h.token == "acme/widgets#7"


def test_resolve_pr_review_strips_trailing_punctuation() -> None:
    # A ref directly followed by sentence punctuation (no space) must still resolve cleanly, else an
    # invalid ``...#7.`` / ``...#7,`` reaches GitHub (Tier B PR #103 round 3).
    dot = resolve_handoff("a\n\nNEXT: pr-review acme/widgets#7.", _ROSTER)
    assert dot.kind is HandoffKind.PR_REVIEW
    assert dot.token == "acme/widgets#7"
    comma = resolve_handoff("b\n\nNEXT: pr-review acme/widgets#7, please review now", _ROSTER)
    assert comma.token == "acme/widgets#7"


# --------------------------------------------------------------------------- #
# resolve_handoff
# --------------------------------------------------------------------------- #


def test_resolve_role_from_roster() -> None:
    h = resolve_handoff("...\nNEXT: Heisenberg", _ROSTER)
    assert h == Handoff(
        kind=HandoffKind.ROLE, identity="Heisenberg", role=Role.IMPLEMENTER, token="Heisenberg"
    )


def test_resolve_is_case_insensitive_and_canonicalizes_identity() -> None:
    h = resolve_handoff("NEXT: einstein", _ROSTER)
    assert h.kind is HandoffKind.ROLE
    assert h.role is Role.NAYSAYER
    assert h.identity == "Einstein"  # canonical roster spelling, not the raw token


def test_resolve_human_and_none_sentinels() -> None:
    assert resolve_handoff("NEXT: human", _ROSTER).kind is HandoffKind.HUMAN
    assert resolve_handoff("NEXT: Human", _ROSTER).kind is HandoffKind.HUMAN
    assert resolve_handoff("NEXT: none", _ROSTER).kind is HandoffKind.NONE


def test_resolve_absent_when_no_next_line() -> None:
    assert resolve_handoff("no handoff here", _ROSTER).kind is HandoffKind.ABSENT


def test_resolve_unknown_participant_is_absent() -> None:
    # Obj3: an unknown name is not silently dropped — it resolves to ABSENT so the conductor routes
    # to a human rather than stranding the thread.
    h = resolve_handoff("NEXT: Schrodinger", _ROSTER)
    assert h.kind is HandoffKind.ABSENT
    assert h.token == "Schrodinger"


# --------------------------------------------------------------------------- #
# build_handoff_protocol_block (emission side — PR-2b-1)
# --------------------------------------------------------------------------- #


def test_protocol_block_uses_the_reserved_sentinels_for_every_role() -> None:
    # The emitted instructions and the parser read the SAME vocabulary (single SOT): the block
    # spells the reserved words from HUMAN_TOKEN / NONE_TOKEN so emit and parse cannot drift.
    for role in Role:
        block = build_handoff_protocol_block(role)
        assert "NEXT:" in block
        assert f"NEXT: {HUMAN_TOKEN}" in block
        assert f"NEXT: {NONE_TOKEN}" in block


def test_proposer_block_forbids_direct_implementer_handoff() -> None:
    block = build_handoff_protocol_block(Role.PROPOSER)
    assert "naysayer" in block
    assert "Do NOT hand a design straight to the implementer" in block


def test_implementer_block_hands_back_to_proposer_and_never_merges() -> None:
    block = build_handoff_protocol_block(Role.IMPLEMENTER)
    assert "proposer" in block
    assert "never merge" in block


def test_naysayer_block_is_advisory() -> None:
    block = build_handoff_protocol_block(Role.NAYSAYER)
    assert "advisory, not a veto" in block


# --------------------------------------------------------------------------- #
# Layer-2 Markdown tolerance — the *safety* tests come first.
#
# Layer 2 is a transitional bridge: an LLM sometimes wraps its final `NEXT:` line in
# common Markdown noise (blockquote / heading / list bullet / bold), and the parser
# tolerates a small, closed set of that noise so a well-formed handoff is not lost.
# It is explicitly a temporary layer — Layer 3 (a structured `next_participant`
# field on the message) will supersede it and this scaffolding is to be removed
# then, NOT kept as a permanent legacy fallback. See the Einstein review that
# authorised this branch (msg on 2026-08-16; the source design message is not
# reachable from this repo — see the read-back note in the PR body).
#
# The order is deliberate: the FIRST tests below pin the invariant that stripping
# Markdown shell characters (`>` / `#` / `-` / `*` / `+` and the surrounding
# `**` / `*` / `` ` ``) NEVER damages the payload — most importantly the `#`
# inside a `pr-review owner/repo#n` ref. This is the concrete bug Einstein
# flagged in the design review; it is regression-fenced BEFORE any tolerance
# extension lands, so the tolerance code cannot be written in a way that
# violates it.
# --------------------------------------------------------------------------- #


class TestPrReviewPayloadSurvivesMarkdownStripping:
    """`#` in `pr-review owner/repo#n` is payload, not Markdown — never strip it."""

    def test_blockquote_prefixed_pr_review_keeps_the_hash_ref(self) -> None:
        # `> NEXT: pr-review owner/repo#7` — the blockquote `>` is Markdown shell,
        # but the `#7` inside the ref is the PR number and must survive intact.
        h = resolve_handoff("done\n\n> NEXT: pr-review acme/widgets#7", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"

    def test_heading_prefixed_pr_review_keeps_the_hash_ref(self) -> None:
        # `# NEXT: pr-review owner/repo#7` — the leading `#` is an ATX heading marker,
        # but the `#7` INSIDE the ref must not be stripped along with it. The bug
        # Einstein flagged is a naive `.strip('#')` on the whole line eating both.
        h = resolve_handoff("done\n\n# NEXT: pr-review acme/widgets#7", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"

    def test_bold_wrapped_pr_review_keeps_the_hash_ref(self) -> None:
        # `**NEXT: pr-review owner/repo#7**` — bold wrappers on the shell must not
        # bleed into the ref.
        h = resolve_handoff("done\n\n**NEXT: pr-review acme/widgets#7**", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"

    def test_list_bullet_prefixed_pr_review_keeps_the_hash_ref(self) -> None:
        h = resolve_handoff("done\n\n- NEXT: pr-review acme/widgets#7", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"

    def test_pr_review_ref_hash_is_never_treated_as_trailing_punct(self) -> None:
        # Combined stressor: blockquote + bold + list, still no damage to `#7`.
        h = resolve_handoff("x\n\n> - **NEXT: pr-review acme/widgets#7**", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"

    def test_atx_heading_close_after_ref_is_not_stripped_into_the_ref(self) -> None:
        # An ATX heading may end with a run of `#` (e.g. `## Heading ##`). The tolerance
        # layer accepts this as *shell*, but on a `pr-review` line the tail `#` is inside
        # the ref (the `#7`), and there is no separate closing marker — so the parser
        # must not synthesise one by eating the trailing `#N` of the ref.
        h = resolve_handoff("y\n\n# NEXT: pr-review acme/widgets#42 #", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#42"


class TestPersonaHandoffToleratesMarkdownNoise:
    """A single closed set of Markdown wrappers around the NEXT line is tolerated."""

    def test_blockquote_prefix(self) -> None:
        assert parse_next_token("body\n\n> NEXT: Heisenberg") == "Heisenberg"

    def test_heading_prefix(self) -> None:
        # `# NEXT: Bohr` (a bare ATX heading) is a common shape when an LLM stresses
        # its handoff line for emphasis.
        assert parse_next_token("body\n\n# NEXT: Bohr") == "Bohr"

    def test_list_bullet_prefix(self) -> None:
        assert parse_next_token("body\n\n- NEXT: Einstein") == "Einstein"
        assert parse_next_token("body\n\n* NEXT: Einstein") == "Einstein"
        assert parse_next_token("body\n\n+ NEXT: Einstein") == "Einstein"

    def test_bold_wrapper(self) -> None:
        assert parse_next_token("body\n\n**NEXT: Heisenberg**") == "Heisenberg"

    def test_italic_wrapper(self) -> None:
        assert parse_next_token("body\n\n*NEXT: Heisenberg*") == "Heisenberg"

    def test_inline_code_wrapper(self) -> None:
        # An LLM often quotes its own protocol literally in backticks.
        assert parse_next_token("body\n\n`NEXT: Heisenberg`") == "Heisenberg"

    def test_stacked_shell_markers_all_stripped(self) -> None:
        # blockquote + list + bold, still resolves to the persona.
        assert parse_next_token("body\n\n> - **NEXT: Bohr**") == "Bohr"

    def test_markdown_wrapped_sentinel_still_resolves(self) -> None:
        assert resolve_handoff("body\n\n> **NEXT: human**", _ROSTER).kind is HandoffKind.HUMAN
        assert resolve_handoff("body\n\n- `NEXT: none`", _ROSTER).kind is HandoffKind.NONE

    def test_markdown_wrapped_persona_resolves_case_insensitive(self) -> None:
        h = resolve_handoff("body\n\n**NEXT: einstein**", _ROSTER)
        assert h.kind is HandoffKind.ROLE
        assert h.identity == "Einstein"  # canonical spelling


class TestLayer2DoesNotDoubleTheExtractionLoop:
    """Structural: the extraction stays a single pass (Einstein review, principle 3).

    The tolerance for Markdown shell characters must not be implemented by running the
    NEXT-scan twice (once RAW, once normalised) — that duplication would give two
    sources of truth for "which line is the handoff" and let them drift. The regex
    itself absorbs the shell, so a single ``findall`` still selects the last line.
    """

    def test_last_wins_still_holds_under_markdown_noise(self) -> None:
        # An earlier bare `NEXT: human` inside a relayed quote must still lose to the
        # final wrapped handoff — proof the last-wins scan runs once over the whole
        # body (not once RAW + once normalised, which could pick different winners).
        body = (
            "I am relaying a review that ended with\n"
            "NEXT: human\n"
            "\n"
            "...my reply...\n"
            "> **NEXT: Bohr**"
        )
        assert parse_next_token(body) == "Bohr"

    def test_only_one_next_line_regex_is_exported(self) -> None:
        # A guard against silently introducing a second parser path. If a future
        # refactor adds `_NEXT_LINE_RE_NORMALISED` next to `_NEXT_LINE_RE`, this
        # test forces the change to be conscious — the design review specifically
        # forbade duplicating the extraction loop.
        import spirrow_mindwire.conductor.handoff as handoff_mod

        next_line_regexes = [n for n in dir(handoff_mod) if "NEXT_LINE_RE" in n]
        assert len(next_line_regexes) == 1, next_line_regexes


# --------------------------------------------------------------------------- #
# The emphasis CLOSE is not always the last thing on the line.
#
# msg-1074 §2 spelled out that this bug fails twice, and that fixing one end
# leaves it broken: the line head `**` defeats `^\s*`, AND — even once the head
# is tolerated — `_NAME_SPLIT_RE` cuts the token at whitespace so the name comes
# out as `Heisenberg**`, which no roster lookup matches.
#
# Tolerating the close only when it sits at end-of-line fixes `**NEXT: X**` but
# NOT the shape that actually stopped the loop, because a real handoff line
# carries a trailing gloss AFTER the closing `**`:
#
#     **NEXT: Heisenberg** — ③ fixture field-fidelity audit（…）に着手する。  # noqa: RUF003
#
# That is the verbatim line from `spirrow-voxelworld` msg-2488, the one that
# produced `exit=0 reason=no_handoff_to_human rounds=0`. msg-1074 §6-2 requires
# it to be pinned by its real text — "「直したつもり」を防ぐ唯一の機構がこれ".
# --------------------------------------------------------------------------- #


class TestEmphasisCloseFollowedByGloss:
    """The real incident shape: bold wrapper CLOSED mid-line, gloss after it."""

    def test_msg_2488_verbatim_line_resolves(self) -> None:
        # The exact body that stopped `T-slope-extension-dead-mode` on 2026-08-15.
        body = "**NEXT: Heisenberg** — ③ fixture field-fidelity audit（…）に着手する。"  # noqa: RUF001
        h = resolve_handoff(body, _ROSTER)
        assert h.kind is HandoffKind.ROLE
        assert h.identity == "Heisenberg"

    def test_bold_close_then_ascii_gloss(self) -> None:
        assert parse_next_token("**NEXT: Bohr** - please pick this up") == "Bohr"

    def test_bold_close_then_emdash_gloss(self) -> None:
        assert parse_next_token("**NEXT: Bohr** — next step") == "Bohr"

    def test_sentinel_with_close_then_gloss(self) -> None:
        h = resolve_handoff("**NEXT: human** — Tier-C の判断が要る", _ROSTER)
        assert h.kind is HandoffKind.HUMAN

    def test_inline_code_close_then_gloss(self) -> None:
        assert parse_next_token("`NEXT: Einstein` — independent review please") == "Einstein"

    def test_emphasis_on_the_name_only(self) -> None:
        # The wrapper need not surround `NEXT:` — an author may bold just the name.
        assert parse_next_token("NEXT: **Heisenberg** — go") == "Heisenberg"

    def test_pr_review_ref_does_not_absorb_the_closing_wrapper(self) -> None:
        # ★ The Tier-B gate breaker. With the close tolerated only at end-of-line,
        # a gloss after `**` leaves the ref as `acme/widgets#7**`, which is handed
        # to GitHub as an invalid ref. The `#7` payload must survive AND the `**`
        # must not.
        h = resolve_handoff("**NEXT: pr-review acme/widgets#7** — please gate", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/widgets#7"


class TestNormalisationDoesNotEatPayloadCharacters:
    """Emphasis characters are stripped only at word edges — never inside a token."""

    def test_underscore_inside_a_repo_name_survives(self) -> None:
        # `_` is a Markdown emphasis marker AND a legal character in a repo name /
        # URL. Stripping it wholesale would corrupt the ref, so the strip is
        # anchored to word boundaries.
        h = resolve_handoff("**NEXT: pr-review acme/my_repo#7**", _ROSTER)
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "acme/my_repo#7"

    def test_underscored_url_ref_survives(self) -> None:
        h = resolve_handoff(
            "**NEXT: pr-review https://github.com/acme/my_repo/pull/7** — gate it",
            _ROSTER,
        )
        assert h.kind is HandoffKind.PR_REVIEW
        assert h.token == "https://github.com/acme/my_repo/pull/7"

    def test_bare_forms_are_untouched(self) -> None:
        # msg-1074 §6-3: prove the pre-existing plain shapes still work.
        assert parse_next_token("NEXT: Heisenberg") == "Heisenberg"
        assert parse_next_token("NEXT: Heisenberg — do the thing") == "Heisenberg"
        assert parse_next_token("NEXT: Heisenberg（実装を進める）") == "Heisenberg"  # noqa: RUF001
        assert resolve_handoff("NEXT: none", _ROSTER).kind is HandoffKind.NONE
