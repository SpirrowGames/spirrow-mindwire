"""Naysayer review rounds: exhaustive first, scoped after, ``LATE`` counted.

Thread ``T-naysayer-exhaustive-first-then-scoped`` (decisions msg-6856 / msg-6857, design v1.5 =
msg-7405 + 7467 + 7474 + 7505 + 7620 + 7732, human approval msg-7721 / msg-7777).

This module is the one home of:

* the rule TEXT every naysayer prompt loads (D1): design-time on both backends and the PR gate,
  so ``LATE`` counts stay comparable across backends. These are procedure, not principles, so
  they are not in ``spec/NAYSAYER_PRINCIPLES.md`` and ``principles_version`` does not move;
* the ``REVIEW ROUND`` line the conductor prepends from round 2 (D2). The code counts rounds; the
  model is never asked to;
* the objection-block reader for BOTH placements (D3), and the counters derived from it (D4/D5).

**What the counters may never do (D4).** ``fix_missing`` and ``late_advisory`` are measurements.
They never become ``MISSING``, never derive REQUEST_CHANGES and never make the conductor hand
back: adding blocking outcomes for a formatting slip would inflate objections, and using the
derived verdict for the gate is Stage 2, a separate Tier-C decision.

**Two readers, on purpose.** :func:`read_objection_block` here reads a posted body for measurement
and understands the design-thread fence. ``pr_review.parse_objections`` keeps its hardened,
injection-reviewed rules (strict single marker, no scan-forward, chained-array refusal) untouched,
because the fence form's "look further down for an opener" is the very scan-forward that parser
refuses. Nothing this module reads reaches a gate verdict.

**Rebase costs a full review (msg-7467 Obj-2).** When ``prev..head`` cannot be read because the
previous head is no longer an ancestor (force-push or rebase), the PR gate reviews everything and
records ``scope=full-fallback``. That is a cost accepted on purpose; take the base in with a merge
commit to avoid it. If the D5 log shows this more than a few times a month, reconsider a
``range-diff`` comparison (msg-7406 objection 2's minimal fix).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

OBJECTIONS_SENTINEL = "<!-- mindwire:objections v1 -->"

_MARKER_RE = re.compile(rf"^{re.escape(OBJECTIONS_SENTINEL)}", re.MULTILINE)
_OPEN_FENCE_RE = re.compile(r"^[ \t]*```json[ \t\r]*$", re.MULTILINE)
# msg-7732 (human decision msg-7721): the first line that STARTS with ``` closes the fence, and
# what follows on that line is ignored. Valid JSON escapes newlines inside strings as ``\n``, so a
# line starting with ``` cannot occur inside a valid JSON string and this cannot cut one short.
_CLOSE_FENCE_RE = re.compile(r"^[ \t]*```.*$", re.MULTILINE)

ReviewScope = Literal["full", "delta", "full-fallback"]
_FIX_WEIGHTS = ("small", "large")

# ── Rule text (D1) ────────────────────────────────────────────────────────────────────────────

REVIEW_ROUND_RULES = """\
Review rounds. Round 1 is exhaustive: with no limit on the count, raise every blocking and every \
advisory objection you can find, so the author can address them all in one revision. A prompt \
without a REVIEW ROUND line is round 1.

From round 2 the prompt names the scope. It is the change since your previous review, plus the \
existing parts that change affects: its callers, the invariants it relies on, and the steps \
before and after it in the same procedure. A fix that breaks something elsewhere is in scope. A \
blocking objection from your previous review that the change did not fix is still in scope. Do \
not re-review the rest. If you notice a blocking defect outside the scope that round 1 could \
have found, you may raise it marked `LATE:` in the prose and with "late": true in the objection \
block. Do not raise advisory objections outside the scope. A LATE objection is a new objection \
with its own hand-back allowance.

For every blocking objection give the author options, not an instruction: the smallest fix \
(`fix_min`); a cut that removes the defect by dropping or narrowing the feature or step \
(`fix_cut`, or "none: <one-line reason>" when it clearly cannot be cut, e.g. the spec requires \
it); and the weight of the fix (`fix_weight`: "small" or "large"). Do not settle on one option \
for the author. Where the material you were given marks a passage `as-proposed`, check that it \
matches your proposal AND look for defects in the proposal itself. A pull request review is given \
the diff and not the PR body, so expect no such marking there.
"""

OBJECTION_ELEMENT_KEYS = (
    '"late": true (optional, omit when false), '
    '"fix_min": "...", "fix_cut": "...", "fix_weight": "small|large" '
    "(the three fix keys are required on blocking objections and not asked of advisory ones)"
)

# msg-7474 Obj-2-1: the design thread is read by people, so the block is folded. The marker stays
# directly before the code block; msg-7505 fixed which two shapes the reader accepts.
DESIGN_OBJECTION_FORMAT = f"""\
Machine-readable record. At the end of your critique, above any TIER-C-CHECK and NEXT: lines, \
record every objection you stated, in the same order (`[]` if none), in exactly this form. The \
prose is for reading and this block is what is counted; it adds nothing and changes no routing.

<details><summary>objections</summary>

{OBJECTIONS_SENTINEL}
```json
[{{"class": "<a class name from objection_classes>", "where": "<section or msg id>", \
"evidence": "<what that class's evidence: line asks for>"}}]
```

</details>

Each element may also carry: {OBJECTION_ELEMENT_KEYS}.
"""

# D6 as replaced by msg-7467 Obj-1. The offline script reads these from the post or PR body.
AUTHOR_OBJECTION_LINE_RULE = """\
When a revision answers naysayer objections, write one line per objection, and answer them all in \
one revision rather than one at a time: \
`Obj-<round>-<n>: 採用=min|cut|own|as-proposed — <reason>`, where <n> is the objection's \
1-based position in that round's objections array, and as-proposed means you used the \
naysayer's proposal unchanged. Say why you chose it."""

AUTHOR_OBJECTION_LINE_RULE_PR = (
    AUTHOR_OBJECTION_LINE_RULE
    + """ On a pull request these lines go under a `Gate objections round N` heading in the PR \
body, not in commit messages. Take the base into your branch with a merge commit: a rebase or \
force-push makes the next gate review the whole PR again."""
)


def render_review_round_line(round_number: int, after_msg_id: str) -> str:
    """The D2 line for a design-thread round >= 2. Round 1 carries no line at all."""
    scope = f"{after_msg_id} より後の全投稿（作者を問わない）"  # noqa: RUF001
    return f"REVIEW ROUND: {round_number} / SCOPE: {scope}"


# ── Reader (D3) ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ObjectionBlock:
    """One read of a posted body. ``elements`` is ``None`` iff the block could not be read.

    ``None`` and ``()`` are different facts: ``()`` is a naysayer stating no objections, ``None``
    is ``objections_unparsed``, which is never counted as zero (msg-7505).
    """

    elements: tuple[Any, ...] | None
    reason: str = ""


def _unparsed(reason: str) -> ObjectionBlock:
    return ObjectionBlock(elements=None, reason=reason)


def _load_list(text: str) -> list[Any] | None:
    """``json.loads`` narrowed to "a list, or nothing". Over-deep input is nothing, not a crash."""
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, list) else None


def read_objection_block(body: str) -> ObjectionBlock:
    """Read the objection array after the marker. Never raises.

    After the one column-zero marker, whichever comes first (msg-7505):

    1. a raw ``[`` separated from the marker by whitespace only (the PR-gate form), read with
       ``raw_decode`` because the verdict line and the footer follow it;
    2. a ```` ```json ```` fence, closed by the first line starting with ```` ``` ```` (msg-7732).
       The fence is what is searched for, so a ``[`` in ``<summary>`` is never read. Its content is
       parsed; on failure, its first ``[`` to last ``]`` is tried once (msg-7620).

    Anything else is ``objections_unparsed``. So are several markers: choosing one would be a guess
    about which array the naysayer meant.
    """
    markers = list(_MARKER_RE.finditer(body))
    if not markers:
        return _unparsed("no-marker")
    if len(markers) > 1:
        return _unparsed("multi-marker")
    rest = body[markers[0].end() :]

    stripped = rest.lstrip()
    if stripped.startswith("["):
        try:
            raw, _ = json.JSONDecoder().raw_decode(stripped)
        except (ValueError, RecursionError):
            return _unparsed("bad-json")
        return ObjectionBlock(tuple(raw)) if isinstance(raw, list) else _unparsed("bad-json")

    opened = _OPEN_FENCE_RE.search(rest)
    if opened is None:
        return _unparsed("no-block")
    closed = _CLOSE_FENCE_RE.search(rest, opened.end())
    if closed is None:
        return _unparsed("unclosed-fence")
    content = rest[opened.end() : closed.start()]

    parsed = _load_list(content)
    if parsed is None:
        first, last = content.find("["), content.rfind("]")
        if 0 <= first < last:
            parsed = _load_list(content[first : last + 1])
    return ObjectionBlock(tuple(parsed)) if parsed is not None else _unparsed("bad-json")


# ── Counters (D4 / D5) ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ObjectionFacts:
    """What the counters need to know about one objection, from either reader."""

    blocks: bool
    late: bool = False
    fix_min: str = ""
    fix_cut: str = ""
    fix_weight: str = ""


def _text(element: Mapping[str, Any], key: str) -> str:
    value = element.get(key)
    return value if isinstance(value, str) else ""


def facts_from_element(element: Any, vocabulary_blocks: Mapping[str, bool]) -> ObjectionFacts:
    """Resolve one raw array element. Unknown or malformed counts as blocking, as the gate does."""
    if not isinstance(element, dict):
        return ObjectionFacts(blocks=True)
    name = _text(element, "class").strip()
    return ObjectionFacts(
        blocks=vocabulary_blocks.get(name, True),
        late=element.get("late") is True,
        fix_min=_text(element, "fix_min"),
        fix_cut=_text(element, "fix_cut"),
        fix_weight=_text(element, "fix_weight"),
    )


@dataclass(frozen=True)
class RoundCounts:
    blocking: int
    advisory: int
    late: int
    late_advisory: int
    fix_missing: int


def _lacks_fix_options(facts: ObjectionFacts) -> bool:
    return (
        not facts.fix_min.strip()
        or not facts.fix_cut.strip()
        or facts.fix_weight not in _FIX_WEIGHTS
    )


def count_round(facts: Iterable[ObjectionFacts], *, round_number: int | None) -> RoundCounts:
    """D4. ``late_advisory`` exists only from round 2: round 1 has no scope to be outside of."""
    items = tuple(facts)
    blocking = [f for f in items if f.blocks]
    advisory = [f for f in items if not f.blocks]
    scoped = round_number is not None and round_number >= 2
    return RoundCounts(
        blocking=len(blocking),
        advisory=len(advisory),
        late=sum(1 for f in blocking if f.late),
        late_advisory=sum(1 for f in advisory if f.late) if scoped else 0,
        fix_missing=sum(1 for f in blocking if _lacks_fix_options(f)),
    )


def scope_for_design_round(round_number: int | None) -> ReviewScope:
    return "delta" if round_number is not None and round_number >= 2 else "full"


def log_round(
    logger: logging.Logger,
    *,
    thread: str,
    pr: str,
    backend: str | None,
    model: str | None,
    round_number: int | None,
    scope: ReviewScope,
    counts: RoundCounts | None,
    out_tokens: object,
) -> None:
    """D5: one ``naysayer.round`` line per naysayer post, on the operator-side log.

    Naysayer-side facts only (msg-7474 Obj-2-2); the author's ``Obj-`` lines do not exist yet when
    this fires and are read offline by ``scripts/naysayer_round_stats.py``. ``counts`` of ``None``
    is ``objections_unparsed=1`` with every count ``-``, so it cannot be summed as zero (msg-7505).
    """

    def show(value: object) -> str:
        return "-" if value is None or value == "" else str(value)

    logger.info(
        "naysayer.round thread=%s pr=%s backend=%s model=%s round=%s scope=%s blocking=%s "
        "advisory=%s late=%s late_advisory=%s fix_missing=%s objections_unparsed=%d out_tokens=%s",
        show(thread),
        show(pr),
        show(backend),
        show(model),
        show(round_number),
        scope,
        show(counts.blocking if counts else None),
        show(counts.advisory if counts else None),
        show(counts.late if counts else None),
        show(counts.late_advisory if counts else None),
        show(counts.fix_missing if counts else None),
        0 if counts else 1,
        show(out_tokens),
    )


def log_design_round(
    logger: logging.Logger,
    *,
    body: str,
    thread: str,
    backend: str | None,
    model: str | None,
    round_number: int | None,
    out_tokens: object,
) -> None:
    """D5 for a design-thread post: read the block, count it, log it. Measurement must never
    cost a posted turn, so an unreadable vocabulary logs ``objections_unparsed`` instead of raising.
    """
    from .principles import PrinciplesError, objection_classes

    counts: RoundCounts | None = None
    block = read_objection_block(body)
    if block.elements is not None:
        try:
            vocabulary = {name: entry.blocks for name, entry in objection_classes().items()}
        except PrinciplesError:
            logger.warning("naysayer.round: objection vocabulary unreadable for %s", thread)
        else:
            counts = count_round(
                (facts_from_element(e, vocabulary) for e in block.elements),
                round_number=round_number,
            )
    log_round(
        logger,
        thread=thread,
        pr="",
        backend=backend,
        model=model,
        round_number=round_number,
        scope=scope_for_design_round(round_number),
        counts=counts,
        out_tokens=out_tokens,
    )


__all__ = [
    "AUTHOR_OBJECTION_LINE_RULE",
    "AUTHOR_OBJECTION_LINE_RULE_PR",
    "DESIGN_OBJECTION_FORMAT",
    "OBJECTIONS_SENTINEL",
    "OBJECTION_ELEMENT_KEYS",
    "REVIEW_ROUND_RULES",
    "ObjectionBlock",
    "ObjectionFacts",
    "ReviewScope",
    "RoundCounts",
    "count_round",
    "facts_from_element",
    "log_design_round",
    "log_round",
    "read_objection_block",
    "render_review_round_line",
    "scope_for_design_round",
]
