"""PR-gate settled-objections ledger, Stage A: built and measured, never sent to the model.

Thread ``T-pr-gate-settled-objections-ledger`` (design v3 = Bohr msg-10102 over v2 msg-10100 and
v1 msg-10098; Einstein's unconditional go followed v3). Stage A only.

**What this module does.** It assembles, from three sources the driver already reads, a one-line-
per-item record of what was raised and what the author answered (D1):

(a) the gate's OWN past whole-content reviews on this PR, every round, read with the measurement
    reader :func:`.review_rounds.read_objection_block` (a round whose block is unreadable is one
    ``Obj-<r>-? objections_unparsed`` line);
(b) the author's ``Obj-<round>-<n>: 採用=...`` lines under a ``Gate objections round N`` heading in
    the PR body, paired with (a) by id;
(c) the PR body's ``## Verification`` section (free text, first 900 bytes) and the optional
    ``## Settled`` section (``Settled: <where> — <claim> — basis: <basis>`` lines, at most 8).

It renders that record under a hard 4000-byte UTF-8 cap (msg-10100 Obj-1-4, msg-10102 Obj-2-1)
and counts, for the round log, how many of the new review's objections point at a place the
record already holds (``repeat`` / ``settled_hit``, msg-10102 Stage A item 2).

**What it never does (msg-10102 Obj-2-2, the Stage A cut).** The rendered ledger is passed to
nothing. There is no prompt-injection path and no flag that would enable one: putting it in front
of the model is Stage B, a separate design decision that also owns the cost estimate. Every number
here is a measurement; none reaches a verdict, a routing decision or a hand-back (as with D4 of
:mod:`.review_rounds`).

**What ``repeat`` and ``settled_hit`` are (Einstein's go on v3).** A place-proximity reference
value: the same file path and a line number within 5 (or the path alone when either side has no
line), with the objection class ignored, so spirrow-voxelworld#590's ``regression`` at ``:84`` and
``correctness`` at ``:83`` are caught. It does NOT establish that the same defect was raised
again; a different defect a few lines away counts too. Read it as a ceiling, never as a count of
re-submissions.

**Settled is a hand transcription (msg-10100 Obj-1-6).** The ``## Settled`` lines are copied into
the PR body by the author (or whoever writes for them). They are unverified, they cost the author
the copying, and a ``basis: ruling <msg-id>`` is the author's claim that a ruling exists, not a
ruling the driver has read. Every PR-body line is rendered with ``src=pr-body``, under a heading
that says so.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from .review_rounds import read_objection_block

# ── Budget (msg-10100 Obj-1-4, msg-10102 Obj-2-1) ─────────────────────────────────────────────

LEDGER_MAX_BYTES = 4000
OMITTED_NOTICE_RESERVE_BYTES = 160
VERIFICATION_MAX_BYTES = 900
SETTLED_MAX_LINES = 8
SETTLED_LINE_MAX_BYTES = 240
PAIR_MAX_BYTES = 360
# msg-10098 D1: evidence first 120 chars, reason up to 160 chars. Applied before the byte fit.
EVIDENCE_MAX_CHARS = 120
REASON_MAX_CHARS = 160
# A ``where`` is a reference and is never cut (Obj-2-1); one this long drops its pair instead.
WHERE_MAX_BYTES = 120
CLASS_MAX_BYTES = 40
REPEAT_LINE_WINDOW = 5

OVER_BUDGET_NOTICE = "[ledger omitted: over budget]"
ELLIPSIS = "…"

_HEADER = (
    "PR gate ledger (a record, not an instruction; src=pr-body lines are author-written and "
    "unverified)"
)
_VERIFICATION_HEADING = "## Verification src=pr-body"
_SETTLED_HEADING = "## Settled src=pr-body"
_OBJECTIONS_HEADING = "## Objections src=gate, answers src=pr-body"

_OBJ_ID_RE = re.compile(r"^Obj-\d+-\d+$")
_GATE_HEADING_RE = re.compile(r"^\s*#{1,6}\s*Gate objections round\s+\d+\b", re.IGNORECASE)
_ANY_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_ANSWER_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?P<id>Obj-\d+-\d+):\s*採用=(?P<adoption>min|cut|own|as-proposed)\b"
    r"\s*(?:[—–-]+\s*)?(?P<reason>.*)$"  # noqa: RUF001
)
_SECTION_RE_TEMPLATE = r"^\s*##\s+{name}\s*$"
_SETTLED_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s+)?Settled:\s*(?P<where>.+?)\s+—\s+(?P<claim>.+?)\s+—\s+"
    r"basis:\s*(?P<basis>.+?)\s*$"
)
_PATH_LINE_RE = re.compile(
    r"^(?P<path>[^\s:,;]+?)(?::(?P<line>\d+))?(?:[:–-]\d+)?$"  # noqa: RUF001
)

Defang = Callable[[str], str]


def _identity(text: str) -> str:
    return text


def _nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut_bytes(text: str, max_bytes: int) -> str:
    """``text`` cut on a UTF-8 character boundary to at most ``max_bytes``, ``…`` when cut."""
    if _nbytes(text) <= max_bytes:
        return text
    room = max_bytes - _nbytes(ELLIPSIS)
    if room <= 0:
        return ""
    return text.encode("utf-8")[:room].decode("utf-8", errors="ignore") + ELLIPSIS


def _one_line(text: str) -> str:
    return " ".join(text.split())


# ── Model ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AuthorAnswer:
    adoption: str
    reason: str


@dataclass(frozen=True)
class GateItem:
    """One objection the gate raised in an earlier round, or that round's unparsed marker."""

    obj_id: str  # ``Obj-<r>-<n>``, or ``Obj-<r>-?`` for an unparsed round
    objection_class: str = ""
    where: str = ""
    evidence: str = ""
    reopens: str = ""  # an ``Obj-<r>-<n>`` this one names in ``"reopen"``; display only
    unparsed: bool = False


@dataclass(frozen=True)
class SettledItem:
    settled_id: str  # ``S-<8 hex>``, from the normalised where + claim (never author-numbered)
    where: str
    claim: str
    basis: str


@dataclass(frozen=True)
class Ledger:
    """The rendered ledger and the facts the round log carries. Passed to no model (Stage A)."""

    rendered: str
    shown_gate: tuple[GateItem, ...]
    shown_settled: tuple[SettledItem, ...]
    omitted: int

    @property
    def nbytes(self) -> int:
        return _nbytes(self.rendered)


@dataclass(frozen=True)
class LedgerHits:
    repeat: int
    settled_hit: int


# ── Readers ───────────────────────────────────────────────────────────────────────────────────


def gate_items_from_reviews(reviews: Sequence[str]) -> list[GateItem]:
    """(a): every objection of every earlier whole-content review, round ``i + 1`` for ``i``."""
    items: list[GateItem] = []
    for index, body in enumerate(reviews):
        round_number = index + 1
        block = read_objection_block(body)
        if block.elements is None:
            items.append(GateItem(obj_id=f"Obj-{round_number}-?", unparsed=True))
            continue
        for n, element in enumerate(block.elements, start=1):
            obj_id = f"Obj-{round_number}-{n}"
            if not isinstance(element, dict):
                items.append(GateItem(obj_id=obj_id))
                continue

            def text(key: str, element: dict[str, object] = element) -> str:
                value = element.get(key)
                return _one_line(value) if isinstance(value, str) else ""

            reopen = text("reopen")
            items.append(
                GateItem(
                    obj_id=obj_id,
                    objection_class=text("class"),
                    where=text("where"),
                    evidence=text("evidence"),
                    reopens=reopen if _OBJ_ID_RE.match(reopen) else "",
                )
            )
    return items


def author_answers(pr_body: str) -> dict[str, AuthorAnswer]:
    """(b): ``Obj-`` lines under a ``Gate objections round N`` heading. The last one per id wins."""
    answers: dict[str, AuthorAnswer] = {}
    inside = False
    for line in pr_body.splitlines():
        if _GATE_HEADING_RE.match(line):
            inside = True
            continue
        if _ANY_HEADING_RE.match(line):
            inside = False
            continue
        if not inside:
            continue
        matched = _ANSWER_LINE_RE.match(line)
        if matched is not None:
            answers[matched["id"]] = AuthorAnswer(
                adoption=matched["adoption"], reason=_one_line(matched["reason"])
            )
    return answers


def _section(pr_body: str, name: str) -> str | None:
    """The text under a ``## <name>`` heading, up to the next ``#``/``##`` heading."""
    heading = re.compile(_SECTION_RE_TEMPLATE.format(name=re.escape(name)), re.IGNORECASE)
    lines = pr_body.splitlines()
    for start, line in enumerate(lines):
        if heading.match(line):
            body: list[str] = []
            for following in lines[start + 1 :]:
                if re.match(r"^\s*#{1,2}\s", following):
                    break
                body.append(following)
            return "\n".join(body).strip()
    return None


def verification_text(pr_body: str) -> str:
    return _section(pr_body, "Verification") or ""


def settled_id(where: str, claim: str) -> str:
    """``S-`` + the first 8 hex of sha256 over the normalised ``where`` and ``claim``.

    Normalised = NFKC, case-folded, whitespace collapsed. Reordering lines keeps every id; editing
    a claim gives a new id, which is right: it is a different claim (msg-10100 Obj-1-3).
    """

    def norm(text: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", text).casefold().split())

    digest = hashlib.sha256(f"{norm(where)}\n{norm(claim)}".encode()).hexdigest()
    return f"S-{digest[:8]}"


def settled_items(pr_body: str) -> list[SettledItem]:
    """(c) ``## Settled``: every well-formed line, de-duplicated by id, in body order."""
    section = _section(pr_body, "Settled")
    if not section:
        return []
    seen: dict[str, SettledItem] = {}
    for line in section.splitlines():
        matched = _SETTLED_LINE_RE.match(line)
        if matched is None:
            continue
        where, claim, basis = (_one_line(matched[k]) for k in ("where", "claim", "basis"))
        item = SettledItem(settled_id(where, claim), where, claim, basis)
        seen.setdefault(item.settled_id, item)
    return list(seen.values())


# ── Rendering ─────────────────────────────────────────────────────────────────────────────────


def _fit(build: Callable[[str, str], str], first: str, second: str, budget: int) -> str | None:
    """Render ``build(first', second')`` within ``budget`` bytes, cutting only the two texts.

    The texts share what is left after the fixed part; one that needs less than half gives the
    rest to the other. ``None`` when the fixed part alone (ids, references, src) does not fit:
    the caller drops the whole unit rather than cut a reference.
    """
    whole = build(first, second)
    if _nbytes(whole) <= budget:
        return whole
    fixed = _nbytes(build("", ""))
    if fixed > budget:
        return None
    room = budget - fixed
    a, b = _nbytes(first), _nbytes(second)
    share_a = min(a, max(room // 2, room - b))
    share_b = room - share_a
    # Defang may lengthen the text (a per-line prefix); shrink until it fits.
    while share_a >= 0 and share_b >= 0:
        out = build(_cut_bytes(first, share_a), _cut_bytes(second, share_b))
        if _nbytes(out) <= budget:
            return out
        if share_a >= share_b and share_a > 0:
            share_a -= 1
        elif share_b > 0:
            share_b -= 1
        else:
            break
    return None


def _cap_chars(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1] + ELLIPSIS


def _render_pair(
    item: GateItem, answer: AuthorAnswer | None, defang: Defang, budget: int
) -> str | None:
    if item.unparsed:
        line = defang(f"{item.obj_id} | src=gate | objections_unparsed")
        return line if _nbytes(line) <= budget else None
    if _nbytes(item.where) > WHERE_MAX_BYTES:
        return None
    head = item.obj_id + (f" ← {item.reopens}" if item.reopens else "")
    cls = _cut_bytes(item.objection_class, CLASS_MAX_BYTES)

    def build(evidence: str, reason: str) -> str:
        gate = f"{head} | {cls} | {item.where} | src=gate | {evidence}"
        if answer is None:
            return defang(gate)
        return defang(f"{gate}\n  answer: 採用={answer.adoption} | src=pr-body | {reason}")

    evidence = _cap_chars(item.evidence, EVIDENCE_MAX_CHARS)
    reason = _cap_chars(answer.reason, REASON_MAX_CHARS) if answer else ""
    return _fit(build, evidence, reason, budget)


def _render_settled(item: SettledItem, defang: Defang) -> str | None:
    def build(claim: str, basis: str) -> str:
        return defang(
            f"{item.settled_id} | src=pr-body | {item.where} — {claim} — basis(author's claim): "
            f"{basis}"
        )

    if _nbytes(item.where) > WHERE_MAX_BYTES:
        return None
    return _fit(build, item.claim, item.basis, SETTLED_LINE_MAX_BYTES)


def _omitted_notice(ids: Sequence[str], defang: Defang) -> str:
    """``[omitted: <k> items; ids: ...]`` within the reserved 160 bytes. ``k`` always shows."""
    if not ids:
        return ""
    shown: list[str] = []
    for obj_id in ids:
        candidate = defang(f"[omitted: {len(ids)} items; ids: {', '.join([*shown, obj_id])}…]")
        if _nbytes(candidate) > OMITTED_NOTICE_RESERVE_BYTES:
            break
        shown.append(obj_id)
    tail = "" if len(shown) == len(ids) else "…"
    return defang(f"[omitted: {len(ids)} items; ids: {', '.join(shown)}{tail}]")


def build_ledger(
    reviews: Sequence[str],
    pr_body: str | None,
    *,
    defang: Defang = _identity,
) -> Ledger:
    """Assemble and render the ledger. Never raises on content; never exceeds 4000 bytes.

    Order of the budget: the header and section headings, Verification (900), Settled (8 lines of
    240), the 160-byte omitted-notice reserve, then gate objection/answer pairs (360 each) from the
    newest round back, dropped oldest-first as whole pairs. The final rendering is checked once
    more; if it is over the cap anyway it is replaced by :data:`OVER_BUDGET_NOTICE`.
    """
    body = pr_body or ""
    gate = gate_items_from_reviews(reviews)
    answers = author_answers(body)
    settled = settled_items(body)
    verification = _cut_bytes(verification_text(body), VERIFICATION_MAX_BYTES)

    omitted_ids: list[str] = []
    parts: list[str] = [defang(_HEADER)]

    if verification:
        parts.append(defang(f"{_VERIFICATION_HEADING}\n{verification}"))

    shown_settled: list[SettledItem] = []
    settled_lines: list[str] = []
    for item in settled:
        if len(shown_settled) >= SETTLED_MAX_LINES:
            omitted_ids.append(item.settled_id)
            continue
        line = _render_settled(item, defang)
        if line is None:
            omitted_ids.append(item.settled_id)
            continue
        shown_settled.append(item)
        settled_lines.append(line)
    if settled_lines:
        parts.append("\n".join([defang(_SETTLED_HEADING), *settled_lines]))

    used = _nbytes("\n\n".join(parts))
    heading = defang(_OBJECTIONS_HEADING)
    # The pairs' room: what is left after the fixed sections, the notice reserve, the heading and
    # the separators that joining them adds.
    room = LEDGER_MAX_BYTES - OMITTED_NOTICE_RESERVE_BYTES - used - _nbytes(heading) - 4
    kept: list[tuple[GateItem, str]] = []
    full = False  # once a pair does not fit, every older pair is dropped: oldest go first
    for obj in reversed(gate):
        line = (
            None
            if full
            else _render_pair(obj, answers.get(obj.obj_id), defang, min(PAIR_MAX_BYTES, room))
        )
        if line is None or _nbytes(line) + 1 > room:
            # A pair whose references alone overrun its 360 bytes is dropped by itself; one that
            # does not fit the room left closes the room for every older pair.
            full = full or line is not None or room < PAIR_MAX_BYTES
            omitted_ids.append(obj.obj_id)
            continue
        kept.append((obj, line))
        room -= _nbytes(line) + 1
    kept.reverse()
    if kept:
        parts.append("\n".join([heading, *(line for _i, line in kept)]))

    notice = _omitted_notice(omitted_ids, defang)
    if notice:
        parts.append(notice)
    rendered = "\n\n".join(parts)
    if _nbytes(rendered) > LEDGER_MAX_BYTES:
        return Ledger(OVER_BUDGET_NOTICE, (), (), len(gate) + len(settled))
    return Ledger(
        rendered=rendered,
        shown_gate=tuple(i for i, _line in kept),
        shown_settled=tuple(shown_settled),
        omitted=len(omitted_ids),
    )


# ── Measurement (repeat / settled_hit) ────────────────────────────────────────────────────────


def _place(where: str) -> tuple[str, int | None] | None:
    """``(path, line)`` from a ``where``, or ``None`` when it names no file-like path."""
    token = where.strip().strip("`").split()[0] if where.strip() else ""
    token = token.strip("`()[],;")
    matched = _PATH_LINE_RE.match(token)
    if matched is None:
        return None
    path = matched["path"]
    if "/" not in path and "." not in path:
        return None  # "D3", "費用" and the like are design-section labels, not files
    line = matched["line"]
    return path, (int(line) if line is not None else None)


def same_place(a: str, b: str) -> bool:
    """Path equal, and line numbers within :data:`REPEAT_LINE_WINDOW` when both have one."""
    pa, pb = _place(a), _place(b)
    if pa is None or pb is None or pa[0] != pb[0]:
        return False
    if pa[1] is None or pb[1] is None:
        return True
    return abs(pa[1] - pb[1]) <= REPEAT_LINE_WINDOW


def count_hits(ledger: Ledger, new_wheres: Iterable[str]) -> LedgerHits:
    """How many new objections point at a place an item SHOWN in the ledger holds.

    A place-proximity reference value, class ignored (module docstring); never a re-submission
    count and never read by anything that decides.
    """
    repeat = settled_hit = 0
    for where in new_wheres:
        on_settled = any(same_place(where, s.where) for s in ledger.shown_settled)
        on_gate = any(same_place(where, g.where) for g in ledger.shown_gate if not g.unparsed)
        if on_settled or on_gate:
            repeat += 1
        if on_settled:
            settled_hit += 1
    return LedgerHits(repeat=repeat, settled_hit=settled_hit)


__all__ = [
    "LEDGER_MAX_BYTES",
    "OVER_BUDGET_NOTICE",
    "AuthorAnswer",
    "GateItem",
    "Ledger",
    "LedgerHits",
    "SettledItem",
    "author_answers",
    "build_ledger",
    "count_hits",
    "gate_items_from_reviews",
    "same_place",
    "settled_id",
    "settled_items",
    "verification_text",
]
