"""ADR-pointer channel for the PR-gate naysayer (2-pass A-3 structure).

Where this fits — target driver
--------------------------------
Owned exclusively by :class:`spirrow_mindwire.naysayer.pr_review.NaysayerPrReviewDriver`
(``naysayer/pr_review.py``); ``scripts/naysayer_review_scoped.py`` is unrelated. The
driver runs TWO concurrent Lexora calls per PR review and merges their outputs
deterministically in code:

* **Pass 1 (the verdict pass)** — index-**less**. Same prompt / behaviour the gate has
  always had. **This is the pass whose ``VERDICT:`` line drives the gate.** The pass-1
  code path is unchanged by this module; that invariance is the structural safety
  guarantee (Tier-C decision, spec msg-692 §1: A-3 with structural guarantee).
* **Pass 2 (the ADR-pointer pass)** — index-injected. Given the deterministic in-repo
  ADR index (:mod:`spirrow_mindwire.naysayer.adr_index` — same source of truth as the
  design-time naysayer) plus the same diff, the model is instructed to output ONLY a
  JSON array of ``{"adr_id": ..., "reason": ...}`` records. **Its output cannot alter
  the verdict**: the driver never asks it to produce one, and even if it did — even if
  the model returned ``{"verdict": "REQUEST_CHANGES"}`` or any other prose — this
  module discards everything outside the JSON pointer schema and returns only the
  filtered pointer list. No code path in the driver reads a verdict from pass 2.

The names ``pass 1`` / ``pass 2`` are ordinal, not sequential: the driver runs them in
parallel (``asyncio.gather``). ``transport != judge`` (msg-430/432): calling
``chat_completion`` twice is transport, not an Agent-loop escalation.

Why 2 passes rather than 1 pass with a prompt constraint
--------------------------------------------------------
The proposer's msg-690 §2.3 originally allowed A-1 (naive full injection). The Tier-C
decision (msg-692 §2) rejected that: a single pass with prompt-side guardrails
("do not cite ADR ids as authority") depends on the model obeying an instruction —
and prompt compliance cannot be regression-tested. The structural 2-pass form makes
verdict-safety a **type-level invariant**: the code that renders the review body
literally never reads a verdict token from the pass-2 return value, so any drift in
the pass-2 model's obedience is inert. This is the ADR-2026-06-04-19-adjacent design
change chosen in spec-thread ``T-pr-gate-adr-index-scope`` msg-692..705.

Vocabulary — the loop-facing marker
-----------------------------------
Every review body carries **one** deterministic marker line on its own line, appended
by the driver (never emitted by the LLM). Its shape is either

* ``ADR-INDEX: pointers=<p>, dropped=<d>`` — pass 2 returned a well-formed JSON array
  of length ``N``. ``p`` is the count of pointers the driver **adopted** (after
  filtering + de-duplication + the ``MAX_POINTERS`` cap). ``d = N - p`` is the count
  of pointers the driver **dropped**. The invariant ``p + d == N`` (see
  :func:`select_adr_pointers`) holds on every parse-success path.
* ``ADR-INDEX: unavailable`` — pass 2 produced NO usable pointer list. This happens
  when the call failed (timeout / HTTP error / cancellation), when the response body
  exceeded ``MAX_POINTER_PAYLOAD_BYTES`` before parse, or when the response could not
  be parsed as a JSON array. On this path ``N``, ``p``, ``d`` are all undefined and
  are NOT emitted (the invariant does not apply). A reason suffix is **not** placed
  on the marker (msg-705 M3): the reason and measured byte count are written to the
  loop-host log (a separate observation surface) — the marker is the loop-facing
  index/alarm, not a dump.

Every other candidate marker payload (manifest hash, per-cause dropped breakdown,
ADR id list) was considered and rejected across msg-696..701 under a single
principle: **the marker records only what cannot be derived elsewhere**. The
per-cause breakdown of ``d`` is derivable from ``[raw pointer array] x [in-repo
manifest]`` and is therefore logged (M5', below) rather than stamped on the marker.

Vocabulary — the terms used by the spec messages
------------------------------------------------
* ``pointer`` (this module uses ``adr_pointer`` in identifiers to avoid colliding with
  the unrelated CLAUDE.md "process pointer" concept): one ``{"adr_id": str,
  "reason": str}`` record emitted by pass 2 — a **non-blocking hint** that the ADR
  named by ``adr_id`` MAY be relevant to the diff. It is a hint, not a finding: see
  the T33 note below.
* ``N``: total pointer count in the raw JSON array pass 2 returned, **before** any
  filtering (unknown ``adr_id`` / duplicates / cap-overflow). This IS the count of
  parsed elements — a size-cap bounce or a parse failure yields no ``N`` at all
  (``unavailable``).
* ``p``: pointers surviving the M2 pipeline (adopted, rendered into the review body).
* ``d``: pointers dropped by the M2 pipeline (``N - p``). Includes ALL drop causes.

The T33 note (msg-692 §5) — must live in code, not chat only
-------------------------------------------------------------
The naysayer sees only the ADR **id + title** (via ``spec/adr_index.yaml`` — no ADR
body is available at inference time; the bodies live in a separate docs repo
inaccessible to the loop). Therefore an ADR-index-derived pointer is an
**unverifiable hint, not a finding**: pass 2 cannot know whether an ADR's body
actually conflicts with the diff — it can only observe title-shape resemblance. This
is why pass 2's output is structurally quarantined from the verdict path: any
authority we granted a hint that provably cannot verify itself would be authority
laundered from a limitation. A-3 does not fix T33 — it survives it.

The Einstein condition (msg-822): code-side definitions, not chat-only
----------------------------------------------------------------------
The spec messages this module implements (T-pr-gate-adr-index-scope msg-690..705)
define ``pointer``, ``N``, ``p``, ``d``, the marker output location, and the target
driver. Those definitions ARE this docstring (and the constants below). The chat
history is not a second source of truth; if a downstream reader wants to know what
``p + d == N`` means, they read here, not the thread.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..lexora.client import ChatMessage
from .adr_index import build_adr_index_block, load_adr_index
from .principles import build_preamble

# --- M2 pipeline parameters (test-imported, so tests never re-hardcode them) ---------- #

# Maximum number of pointers the driver adopts from pass 2. Rationale (msg-694 §5,
# msg-698 §3): a hard cap on adopted count bounds the review-body real estate the
# non-blocking hint section can consume, and (with the ``dropped`` counter) makes
# over-cap conditions observable on the marker. cap=5 is a compromise the spec
# thread fixed and did not re-litigate ("cap=5 is not the argument in this thread —
# do not change it here", msg-698 §4).
MAX_POINTERS = 5

# Maximum length of a single ``reason`` field, after ``\n`` removal, before it is
# rendered into the PR body. msg-696 §5 residual + msg-696 §4 rationale: a hint's
# reason line must fit on one line and not be a bookshelf's worth of prose.
MAX_REASON_CHARS = 200

# --- M1' oversize guard (F-4 read-back — see PR body for measurement caveat) ---------- #

# The maximum number of raw bytes (UTF-8) the driver will parse from pass 2's
# ``content`` field. Above this the driver returns ``unavailable(oversize)`` WITHOUT
# invoking the JSON parser, and logs the measured byte count (M5').
#
# Rationale (msg-703 §5, msg-705 §5): the cap does NOT defend against OOM
# (the response body is already in memory once ``httpx`` returns; the true OOM
# defence lives in the transport layer, deferred to a separate item as
# msg-705's F-3). What it DOES defend against is (a) non-linear pipeline cost in
# subsequent parse/normalise/dedup steps when pass 2 misbehaves and floods
# ``content`` with prose, and (b) the M5' log surface, which repeats the raw
# content for post-mortem — capping the parse input bounds that log line too.
#
# Derivation (from ``_DEFAULT_MAX_TOKENS = 32000`` in
# :mod:`spirrow_mindwire.naysayer.pr_review`):
# * Legitimate pass-2 output shape: MAX_POINTERS=5 records x
#   (~30-byte ADR id + MAX_REASON_CHARS=200-byte reason + JSON scaffolding ~20 bytes)
#   ≈ 1.25 KiB, plus outer array brackets and whitespace ≈ ~1.5 KiB worst case.
# * 8 KiB is ~5x that ceiling: comfortably absorbs JSON reformatting variance,
#   whitespace, and one or two oversize ``reason`` fields (they truncate in M2 §7,
#   so their pre-truncation size does not compromise correctness); still small
#   enough that a runaway model burning the full 32 000-token output budget on
#   critique prose (~30-100+ KiB depending on token density) trips the cap and
#   surfaces as ``unavailable``, rather than silently smuggling free-form text
#   through the pointer channel.
# * NOT the naive 16 KiB (msg-705 §5 explicitly forbids adopting 16 KiB without
#   justification — that value only ever appeared in msg-696 as a placeholder).
# * NOT measured against a live Gemini tokenizer: there is no Gemini tokenizer in
#   the repo and the sandbox cannot reach Lexora. The measurement gap is disclosed
#   in the PR body per msg-705 §6 F-4; the 8 KiB choice is a bounded rationale
#   from the shape of legitimate output, not a byte-per-token multiplication.
MAX_POINTER_PAYLOAD_BYTES = 8 * 1024

# --- Marker vocabulary (test-imported so the strings live in one place) --------------- #

MARKER_PREFIX = "ADR-INDEX:"

# The ``unavailable`` state deliberately carries NO reason suffix on the marker
# (msg-705 M3). The suffix would enlarge the marker's state space with information
# that is already recorded on a separate observation surface (the loop-host log,
# via :func:`unavailable_log_line`); the marker's job is to be an index/alarm.
MARKER_UNAVAILABLE = f"{MARKER_PREFIX} unavailable"

# --- Pass 2 prompt (M1 self-declaration — code-side constant, tests import) ----------- #

# The pass-2 system-prompt task instructions. Kept intentionally rigid: the ONLY
# permitted output shape is a JSON array of pointer records. Any other output is
# discarded downstream (see :func:`select_adr_pointers`), and prompt compliance is
# never load-bearing — the structural filter is (msg-692 §2). This literal is the
# module-level constant so tests can assert on it without re-declaring the string.
ADR_POINTER_SYSTEM_TASK_PROMPT = """\
This is the ADR-POINTER pass (pass 2) of a two-pass PR review. Your ONLY job on \
this pass is to enumerate ADR ids from the injected ADR index that MIGHT be \
relevant to the diff, so a downstream reviewer can consult them.

Rules for this pass:
  1. You have NO authority to decide whether the diff should be merged. A separate \
     verdict pass (pass 1) — which is not given the ADR index — is the sole source \
     of the VERDICT line on this review. Do not emit a VERDICT line here; it will \
     be discarded.
  2. You have access to ADR ids + titles ONLY, not the ADR bodies (T33). A pointer \
     you emit is an UNVERIFIABLE HINT that a downstream human should cross-check, \
     not a finding. Do not claim an ADR is "violated" — you cannot know that.
  3. Your entire reply MUST be a single JSON array. No prose, no code fences, no \
     preamble, no trailing text. Each element MUST be an object of exactly:
         {"adr_id": "<ADR-YYYY-MM-DD-NN>", "reason": "<one-line hint <= 200 chars>"}
  4. If no ADR from the injected index looks relevant, reply with an empty array: []
  5. Prefer FEWER, higher-signal pointers to a long list. Redundant or unrelated \
     pointers will be silently dropped by downstream normalisation.
"""

# The pass-1 self-declaration prompt fragment. Under A-3 the pass-1 model does not
# receive the ADR index and does not need to reason about ADR ids; the structural
# 2-pass form protects the verdict from index-derived hallucinations without asking
# the pass-1 model to help. But msg-692 §3 endorses source-side self-declaration for
# both passes, so a short M1 stub is included here (test-imported): the prompt says
# it explicitly rather than leaving the invariant implicit.
PASS_1_ADR_INDEX_SELF_DECLARATION = """\
Note on ADR knowledge: this pass is not given the project's ADR index. A separate \
pass (pass 2) enumerates ADR pointers into a NON-BLOCKING hints section merged \
into the same review body; those hints do NOT affect the verdict. Focus on the \
diff. If the diff plausibly touches a norm you do not have visibility into, say so \
in prose — do NOT cite an ADR id from memory as authority.
"""


# --- Data ----------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AdrPointer:
    """A single normalised, adopted ADR pointer (a hint, not a finding — see T33)."""

    adr_id: str
    reason: str


@dataclass(frozen=True)
class AdrPointerSelection:
    """The result of running pass-2's raw response through the M2 pipeline.

    Exactly one of two shapes:

    * ``available=True``, with ``adopted`` / ``dropped`` / ``n_total`` populated
      (``n_total == len(adopted) + dropped``, i.e. the ``p + d == N`` M4 invariant).
    * ``available=False``, with ``unavailable_reason`` set to one of the M1'
      trigger strings and ``measured_bytes`` recording the observed payload size
      (for the M5' log line). Adopted / dropped / n_total are unused.
    """

    available: bool
    adopted: tuple[AdrPointer, ...] = ()
    dropped: int = 0
    n_total: int | None = None
    unavailable_reason: str | None = None  # "call-failed" | "oversize" | "parse-fail"
    measured_bytes: int = 0
    # Not part of the marker — kept for the M5' log (msg-701 §2, msg-703 M5'): a
    # post-mortem-friendly per-cause count of the drops. Derived from the pipeline
    # by construction; never expected to appear in the PR body.
    dropped_breakdown: dict[str, int] = field(default_factory=dict)


# --- Prompt builders (T1/T4-supporting entry points) ---------------------------------- #


def build_pr_review_pass1_system_prompt(*, verdict_task_prompt: str, nonce: str) -> str:
    """Assemble the pass-1 (verdict) system prompt from its production parts.

    This is the SINGLE entry point tests use to construct the exact system message
    the driver sends on pass 1 (T1). It exists so tests never re-hardcode the
    concatenation order and so any future change here is caught by the
    anti-tautology tests (T2 / T4) that assert the ADR index block is NOT part of
    what this returns.

    ``verdict_task_prompt`` is the driver's own pass-1 verdict instructions
    (``_PR_REVIEW_SYSTEM_PROMPT`` in :mod:`spirrow_mindwire.naysayer.pr_review`);
    it is passed in rather than imported so this module has no cyclic dependency
    on the driver module.

    ``nonce`` is the per-invocation hex string the driver generates for the
    objection-block marker. It is REQUIRED (not defaulted), for the same reason
    :func:`~spirrow_mindwire.naysayer.pr_review._build_messages` requires it: the
    ``verdict_task_prompt`` we are handed HARDCODES a sentence telling the model
    to look for a per-review nonce paragraph delivered below, so a builder call
    that omits ``nonce`` would produce a self-contradictory system message
    (instruction present, referent absent) and every review under it would derive
    MISSING. Defaulting the parameter would let a caller construct the invalid
    prompt silently — the exact dual-management defect the class system exists to
    remove. Rider-3 finding on PR #201 (naysayer, correctness) supplied the
    correction; this parameter shape is what pins it. Passed as an argument
    rather than generated here because the DRIVER owns the nonce's lifecycle:
    the same value must reach both the prompt and the parser, and generating it
    here would divorce the two.
    """
    return "\n\n".join(
        [
            build_preamble(),
            verdict_task_prompt,
            "PER-REVIEW NONCE for the objection-block marker: `"
            f"{nonce}"
            "`.\n\nThe exemplar above shows the marker as "
            "`<!-- mindwire:objections v1 nonce=NONCE -->`. In your reply, replace the literal "
            f"string `NONCE` in that marker with `{nonce}` — verbatim, no whitespace, no quotes. "
            "Any other value (including the placeholder `NONCE`, or no `nonce=` at all) is not "
            "authoritative, and the driver treats a block with such a marker as absent.",
            PASS_1_ADR_INDEX_SELF_DECLARATION,
        ]
    )


def build_pr_review_pass2_messages(
    diff: str, pr_slug: str, *, repo_root: Path | None = None
) -> list[ChatMessage]:
    """Assemble the pass-2 (ADR-pointer) messages the driver sends to Lexora.

    Includes the deterministic in-repo ADR index block (same
    :func:`~spirrow_mindwire.naysayer.adr_index.build_adr_index_block` the
    design-time naysayer uses — one source of truth per T33) AND the pass-2 task
    prompt that constrains the reply to a JSON pointer array only.
    """
    system = (
        f"{build_preamble()}\n\n"
        f"{ADR_POINTER_SYSTEM_TASK_PROMPT}\n\n"
        f"{build_adr_index_block(repo_root)}"
    )
    user = (
        f"Enumerate ADR pointers for pull request {pr_slug}. Reply with the JSON "
        f"array of pointer records only — no prose, no code fences.\n\n"
        f"```diff\n{diff}\n```"
    )
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user),
    ]


# --- M1' + M2 pipeline (raw content -> AdrPointerSelection) --------------------------- #

# Strip code fences the model may add despite prompt rule 3 (this is a common
# violation shape, cheap to normalise before the parse-fail bounce). We do NOT
# treat this normalisation as a filter step counted toward ``dropped``: it only
# rescues a syntactically-valid array that was wrapped, and never invents pointers.
#
# Public (was ``_strip_wrapping``) because pass 1's objection-block parser hits the exact
# same model behaviour — a JSON payload the prompt asked for unfenced, returned fenced.
# One normalisation, two readers: writing a second one is how the two would drift.
#
# The two branches are anchored (``^`` … ``$``, no ``re.MULTILINE``), so the alternation can
# match at most once each and ``sub`` removes exactly a WRAPPING pair. Deliberate: a closing
# fence that is not the last thing in the payload is left in place. The two consumers then
# diverge, and the difference is the reason this is anchored rather than greedy:
#   * ``parse_objections`` reads with ``raw_decode``, which stops at the end of the array —
#     an unstripped closing fence, the VERDICT line and any trailing prose are never consumed.
#   * ``select_adr_pointers`` reads with ``json.loads`` over the whole payload, so the same
#     input fails and degrades to ``unavailable(parse-fail)`` — fail-closed, as designed.
# Neither consumer wants a greedy strip: removing a ``\`\`\``` from the middle of a payload
# would be editing the model's content, not normalising its wrapper. Both behaviours are
# pinned by tests (see ``test_objection_block_parses_with_a_closing_fence_off_the_end``).
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?|\n?\s*```\s*$", re.IGNORECASE)


def strip_wrapping_fences(raw: str) -> str:
    stripped = raw.strip()
    # Strip a single leading + trailing code fence pair, if present.
    stripped = _FENCE_RE.sub("", stripped)
    return stripped.strip()


def select_adr_pointers(raw_content: str, manifest_ids: frozenset[str]) -> AdrPointerSelection:
    """Run pass-2's raw ``content`` through the M1'/M2 pipeline.

    Steps (deterministic, tested one by one):

    1. **M1' (i) oversize** — measure UTF-8 length of ``raw_content``. If it
       exceeds :data:`MAX_POINTER_PAYLOAD_BYTES`, return ``unavailable(oversize)``
       BEFORE the parser runs.
    2. **M1' (ii) parse-fail** — try to load the (fence-stripped) content as JSON.
       If it does not parse, or the top-level is not a list, return
       ``unavailable(parse-fail)``.
    3. **M2 (schema)** — walk the list in ORIGINAL order. For each element that
       is not a ``{"adr_id": str, "reason": str}`` object (with both fields as
       strings and ``adr_id`` non-empty), drop it and count under ``malformed``.
    4. **M2 (a) unknown_id** — drop pointers whose ``adr_id`` is not in
       ``manifest_ids``; count under ``unknown_id``.
    5. **M2 (b) dup** — drop pointers whose ``adr_id`` was already adopted
       (first-wins); count under ``dup``.
    6. **M2 (c) overflow** — once :data:`MAX_POINTERS` pointers are adopted, drop
       every subsequent survivor; count under ``overflow``.
    7. Normalise ``reason``: strip newlines (single-line invariant so the ``reason``
       cannot smuggle a fake marker line or a section-break into the review body)
       and truncate to :data:`MAX_REASON_CHARS`.

    Return an :class:`AdrPointerSelection` with ``adopted`` in **input order**
    (msg-701 §3: no adr_id sort — that would silently prefer the alphabetically
    earliest ADRs, a systemic bias without value), ``dropped`` = total dropped
    across ALL steps, and ``n_total`` = length of the parsed list (i.e. steps 3-6
    combined). The M4 invariant ``len(adopted) + dropped == n_total`` holds on
    every return path from this function's success branch.
    """
    measured_bytes = len(raw_content.encode("utf-8"))
    if measured_bytes > MAX_POINTER_PAYLOAD_BYTES:
        return AdrPointerSelection(
            available=False,
            unavailable_reason="oversize",
            measured_bytes=measured_bytes,
        )

    body = strip_wrapping_fences(raw_content)
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return AdrPointerSelection(
            available=False,
            unavailable_reason="parse-fail",
            measured_bytes=measured_bytes,
        )
    if not isinstance(parsed, list):
        return AdrPointerSelection(
            available=False,
            unavailable_reason="parse-fail",
            measured_bytes=measured_bytes,
        )

    n_total = len(parsed)
    adopted: list[AdrPointer] = []
    adopted_ids: set[str] = set()
    breakdown = {"malformed": 0, "unknown_id": 0, "dup": 0, "overflow": 0}

    for element in parsed:
        # Step 3: schema.
        if not isinstance(element, dict):
            breakdown["malformed"] += 1
            continue
        adr_id = element.get("adr_id")
        reason = element.get("reason")
        if not isinstance(adr_id, str) or not adr_id or not isinstance(reason, str):
            breakdown["malformed"] += 1
            continue
        # Step 4: manifest membership.
        if adr_id not in manifest_ids:
            breakdown["unknown_id"] += 1
            continue
        # Step 5: dedup (first-wins).
        if adr_id in adopted_ids:
            breakdown["dup"] += 1
            continue
        # Step 6: cap.
        if len(adopted) >= MAX_POINTERS:
            breakdown["overflow"] += 1
            continue
        # Step 7: reason normalisation.
        normalised_reason = reason.replace("\r", " ").replace("\n", " ").strip()
        if len(normalised_reason) > MAX_REASON_CHARS:
            normalised_reason = normalised_reason[:MAX_REASON_CHARS]
        adopted.append(AdrPointer(adr_id=adr_id, reason=normalised_reason))
        adopted_ids.add(adr_id)

    dropped = sum(breakdown.values())
    return AdrPointerSelection(
        available=True,
        adopted=tuple(adopted),
        dropped=dropped,
        n_total=n_total,
        measured_bytes=measured_bytes,
        dropped_breakdown={cause: n for cause, n in breakdown.items() if n},
    )


# --- Rendering (marker + PR-body section) --------------------------------------------- #


def render_marker(selection: AdrPointerSelection) -> str:
    """Render the single loop-facing marker line for ``selection``.

    Returns exactly one line, without a trailing newline. Callers stamp it onto
    the review body at a known position (the driver appends it as the final line
    of the body).
    """
    if not selection.available:
        return MARKER_UNAVAILABLE
    return f"{MARKER_PREFIX} pointers={len(selection.adopted)}, dropped={selection.dropped}"


def render_adr_pointers_section(selection: AdrPointerSelection) -> str:
    """Render the non-blocking human-readable pointers section for the PR body.

    Empty string when nothing to render (no adopted pointers AND unavailable does
    not carry a section — the marker line alone tells the reader). Otherwise a
    short section titled and clearly labelled non-blocking, listing each adopted
    pointer on its own line (guaranteed single-line by the reason normalisation
    in :func:`select_adr_pointers`).
    """
    if not selection.available or not selection.adopted:
        return ""
    lines = [
        "## ADR pointers (non-blocking hints from the ADR-index pass)",
        (
            "These pointers were emitted by the naysayer's index-injected pass and "
            "do NOT contribute to the VERDICT above. Each names an ADR whose id + "
            "title looked plausibly related to the diff — a reviewer should read the "
            "ADR body to decide whether it truly applies. The naysayer cannot verify "
            "these hints (T33: ADR bodies are not visible at inference time)."
        ),
        "",
    ]
    lines.extend(f"- `{p.adr_id}` — {p.reason}" for p in selection.adopted)
    return "\n".join(lines)


def append_marker(body: str, selection: AdrPointerSelection) -> str:
    """Return ``body`` with the pointer section (if any) and marker appended.

    The marker is always the FINAL non-empty line of the returned string — the
    driver's callers rely on the marker being locatable at a fixed position
    (msg-690 M2 "末尾推奨", following the #120 precedent that a tail-emitted
    self-declaration was what let the design-time bug be discovered).

    Precondition: ``body`` is the pass-1 critique (with its VERDICT line already
    present). This function does not touch the VERDICT line — it only appends
    downstream of it.
    """
    section = render_adr_pointers_section(selection)
    marker = render_marker(selection)
    trailer_parts = [body.rstrip()]
    if section:
        trailer_parts.append("")
        trailer_parts.append(section)
    trailer_parts.append("")
    trailer_parts.append(marker)
    return "\n".join(trailer_parts)


def unavailable_log_line(selection: AdrPointerSelection) -> str | None:
    """M5' log line for an unavailable pass-2 outcome; ``None`` when available.

    Returns a single-line string suitable for the loop-host logger; the driver is
    responsible for actually emitting it. Records the reason and the measured
    byte count — the two things not observable from the marker (msg-705 M5').
    """
    if selection.available:
        return None
    return (
        f"naysayer pr-gate pass-2 UNAVAILABLE: "
        f"reason={selection.unavailable_reason} bytes={selection.measured_bytes}"
    )


def available_log_line(selection: AdrPointerSelection, raw_content: str) -> str | None:
    """M5' log line for a successful pass-2 outcome; ``None`` when unavailable.

    Records the raw pointer payload (bounded by :data:`MAX_POINTER_PAYLOAD_BYTES`)
    and the per-cause dropped breakdown, so the internals of the marker's ``d``
    counter are recoverable post-mortem without stamping every cause on the
    marker itself (per msg-701 §1's derivability principle).
    """
    if not selection.available:
        return None
    # Bound the log line explicitly: the driver's cap already limits raw_content,
    # but a caller supplying an unrelated string should not silently emit a
    # 100-KB log line either.
    raw_bytes = raw_content.encode("utf-8")
    if len(raw_bytes) > MAX_POINTER_PAYLOAD_BYTES:
        raw_bytes = raw_bytes[:MAX_POINTER_PAYLOAD_BYTES]
    raw_repr = raw_bytes.decode("utf-8", errors="replace").replace("\n", "\\n")
    return (
        f"naysayer pr-gate pass-2 pointers={len(selection.adopted)} "
        f"dropped={selection.dropped} breakdown={selection.dropped_breakdown} "
        f"raw={raw_repr!r}"
    )


def load_manifest_ids(repo_root: Path | None = None) -> frozenset[str]:
    """The set of manifest ADR ids the M2 pipeline validates against.

    Thin wrapper around :func:`~spirrow_mindwire.naysayer.adr_index.load_adr_index`
    that returns only the ``adr_id`` set — the driver does not need the titles for
    validation, only membership.
    """
    return frozenset(adr_id for adr_id, _ in load_adr_index(repo_root))


__all__ = [
    "ADR_POINTER_SYSTEM_TASK_PROMPT",
    "MARKER_PREFIX",
    "MARKER_UNAVAILABLE",
    "MAX_POINTERS",
    "MAX_POINTER_PAYLOAD_BYTES",
    "MAX_REASON_CHARS",
    "PASS_1_ADR_INDEX_SELF_DECLARATION",
    "AdrPointer",
    "AdrPointerSelection",
    "append_marker",
    "available_log_line",
    "build_pr_review_pass1_system_prompt",
    "build_pr_review_pass2_messages",
    "load_manifest_ids",
    "render_adr_pointers_section",
    "render_marker",
    "select_adr_pointers",
    "unavailable_log_line",
]
