"""T-tier-c-six-articles-security-boundary — the gate side of article 6.

Bohr msg-9650 §1.1 (the ``SECURITY-BASIS:`` table and RETRY), msg-9653 / msg-9655 §1.1'' (the
security hold in the gate), msg-9665 §1.1⁷ (a structural refusal always records its own hold,
deduped by the normalised body hash). Jev's side (hold relations, ``exposure_claim``) is the
conductor's and is tested there.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from spirrow_mindwire.tier_c_admission_gate import (
    SECURITY_BASIS_HINT,
    SECURITY_RETRY_SUFFIX,
    AdmissionDecision,
    AdmissionVerdict,
    BounceReason,
    LogKind,
    check_security_basis,
    decide_admission,
    normalized_body_sha256,
)

NOW = datetime(2026, 11, 20, tzinfo=UTC)


def _no_retries(_uuid: str, _author: str) -> bool:
    return False


def _any_retry(_uuid: str, _author: str) -> bool:
    return True


def _decide(body: str, *, hold: bool = False, retry: bool = False) -> AdmissionDecision:
    return decide_admission(
        body=body,
        author="Bohr",
        retry_lookup=_any_retry if retry else _no_retries,
        now=NOW,
        bounce_uuid="u-1",
        security_hold=hold,
    )


def _sec(basis: str = "") -> str:
    return f"本文\n{basis}TIER-C: security\nNEXT: human\n"


# --------------------------------------------------------------------------- grammar table


@pytest.mark.parametrize(
    ("basis", "reason"),
    [
        ("", BounceReason.SECURITY_BASIS_MISSING),
        (
            "SECURITY-BASIS: exposure a.py:1\nSECURITY-BASIS: exposure a.py:1\n",
            BounceReason.SECURITY_BASIS_MALFORMED,
        ),
        (
            "SECURITY-BASIS: tighten deny gh auth token\n",
            BounceReason.SECURITY_TIGHTEN_IS_AGENT_CALL,
        ),
        ("SECURITY-BASIS: harden x\n", BounceReason.SECURITY_TIGHTEN_IS_AGENT_CALL),
        ("SECURITY-BASIS: narrow x\n", BounceReason.SECURITY_TIGHTEN_IS_AGENT_CALL),
        ("SECURITY-BASIS: leak a.py:1\n", BounceReason.SECURITY_BASIS_MALFORMED),
        ("SECURITY-BASIS: exposure\n", BounceReason.SECURITY_BASIS_MALFORMED),
        ("SECURITY-BASIS:\n", BounceReason.SECURITY_BASIS_MALFORMED),
        ("SECURITY-BASIS: exposure src/a.py:12\n", None),
        ("SECURITY-BASIS: widen gh token scope repo->org\n", None),
        ("security-basis: Exposure abc1234\r\n", None),  # case-insensitive, CRLF
    ],
)
def test_basis_table(basis: str, reason: BounceReason | None) -> None:
    assert check_security_basis(_sec(basis)).reason is reason
    result = _decide(_sec(basis))
    if reason is None:
        assert result.verdict is AdmissionVerdict.ADMIT and result.rule == "R1-admit"
    else:
        assert result.verdict is AdmissionVerdict.BOUNCE and result.bounce_reason is reason


def test_missing_basis_records_a_structural_hold_with_the_body_hash() -> None:
    body = _sec()
    result = _decide(body)
    (entry,) = result.log_entries
    assert entry.kind is LogKind.BOUNCED
    assert entry.payload["security_hold"] is True
    assert entry.payload["hold_kind"] == "structural"
    assert entry.payload["held_basis"] is None
    assert entry.payload["body_sha256"] == normalized_body_sha256(body)
    assert result.bounce_hint == SECURITY_BASIS_HINT
    assert "never the secret value" in SECURITY_BASIS_HINT


def test_malformed_basis_records_the_line_as_held_basis() -> None:
    (entry,) = _decide(_sec("SECURITY-BASIS: leak a.py:1\n")).log_entries
    assert entry.payload["held_basis"] == "SECURITY-BASIS: leak a.py:1"


def test_tighten_puts_no_hold() -> None:
    (entry,) = _decide(_sec("SECURITY-BASIS: tighten x\n")).log_entries
    assert "security_hold" not in entry.payload
    assert "DECIDED: [security-tighten]" in str(entry.payload["hint"])


# --------------------------------------------------------------------------- RETRY


def test_retry_without_basis_bounces_again_instead_of_force_admitting() -> None:
    result = _decide("RETRY: u-0\n" + _sec(), retry=True)
    assert result.verdict is AdmissionVerdict.BOUNCE
    assert result.bounce_reason is BounceReason.SECURITY_BASIS_MISSING
    assert result.rule == "R0-RETRY:security-basis"
    assert result.bounce_hint is not None and result.bounce_hint.endswith(SECURITY_RETRY_SUFFIX)
    assert result.log_entries[0].payload["security_hold"] is True


def test_retry_with_basis_fixed_is_label_corrected() -> None:
    result = _decide("RETRY: u-0\n" + _sec("SECURITY-BASIS: exposure a.py:3\n"), retry=True)
    assert result.verdict is AdmissionVerdict.ADMIT
    assert result.rule == "R0-RETRY:label_corrected"


def test_retry_relabelled_other_still_force_admits_without_a_hold() -> None:
    """No hold open: the existing infinite-loop cutoff is unchanged. The exposure check before
    delivery (msg-9659 §1.3) is what stops an unlocated exposure on this path."""
    result = _decide("RETRY: u-0\nTIER-C: other: leak\nNEXT: human\n", retry=True)
    assert result.rule == "R0-RETRY:second_time_force_admit"


# --------------------------------------------------------------------------- hold


@pytest.mark.parametrize(
    "body",
    [
        "TIER-C: other: leak\nNEXT: human\n",
        "TIER-C: unsure:goal?\nNEXT: human\n",
        "TIER-C: release-cross-repo\nNEXT: human\n",
        "TIER-C: frobnicate\nNEXT: human\n",
        "NEXT: human\n",
        _sec(),
        _sec("SECURITY-BASIS: leak a.py:1\n"),
    ],
)
@pytest.mark.parametrize("retry", [False, True])
def test_hold_bounces_everything_unadmittable_even_on_retry(body: str, retry: bool) -> None:
    prefix = "RETRY: u-0\n" if retry else ""
    result = _decide(prefix + body, hold=True, retry=retry)
    assert result.verdict is AdmissionVerdict.BOUNCE
    assert result.bounce_reason is BounceReason.SECURITY_HOLD
    (entry,) = result.log_entries
    assert entry.payload["security_hold"] is True
    assert entry.payload["hold_kind"] == "structural"  # msg-9665 §1.1⁷: its own hold


@pytest.mark.parametrize(
    "body",
    [
        _sec("SECURITY-BASIS: exposure a.py:9\n"),
        "TIER-C: goal\nNEXT: human\n",
        "TIER-C: cost\nNEXT: human\n",
        "TIER-C: irreversible\nNEXT: human\n",
        "TIER-C: dispute\nNEXT: human\n",
        "TIER-C: scope\nNEXT: human\n",
    ],
)
def test_hold_admits_admittable_labels_only_as_recheck(body: str) -> None:
    """Part 1b (Bohr msg-9886 §2): a well-formed admittable head passes the label gate only as
    ``RH-recheck`` — never as an ordinary ``R1`` / RETRY admit — and writes no row; the conductor
    delivers it only when Jev answers every open hold ``supplemented``
    (``test_tier_c_hold_relations``). The RETRY line buys nothing while held."""
    result = _decide("RETRY: u-0\n" + body, hold=True, retry=True)
    assert result.verdict is AdmissionVerdict.ADMIT
    assert result.rule == "RH-recheck"
    assert result.log_entries == []


def test_hold_does_not_hold_a_tighten() -> None:
    result = _decide(_sec("SECURITY-BASIS: tighten x\n"), hold=True)
    assert result.bounce_reason is BounceReason.SECURITY_TIGHTEN_IS_AGENT_CALL
    assert "security_hold" not in result.log_entries[0].payload


# --------------------------------------------------------------------------- dedupe hash


def test_body_hash_ignores_whitespace_line_endings_and_retry_line() -> None:
    a = "B 本文  です\n\nTIER-C: security\nNEXT: human\n"
    b = "RETRY: u-9\r\nB  本文 です\r\nTIER-C: security  \r\nNEXT: human"
    assert normalized_body_sha256(a) == normalized_body_sha256(b)
    assert normalized_body_sha256(a) != normalized_body_sha256(a.replace("本文", "別件"))
