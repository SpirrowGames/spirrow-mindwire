"""Tests for the allow-list denial record — spec ``spec/design/T-denial-detail-and-overdeny.md``.

The M-matrix below is a **characterization** test: the expectations record what the
classifier does *today*, not what it ought to do. That is deliberate. The open
question this record exists to settle is whether the coarse floor over-denies — and
answering it by first changing the classifier would destroy the evidence. So PR-1
pins current behaviour (AC2: not one verdict changes) and makes the provenance
visible; any change to these values is a separate, deliberate decision.
"""

from __future__ import annotations

import pytest

from spirrow_mindwire.adapters.implementer import classify_tool_call
from spirrow_mindwire.allowlist import ClassifiedAction
from spirrow_mindwire.denial_record import redact

# The exact body that was being written when six sessions halted on 2026-08-11:
# PowerShell test cleanup. It contains `Remove-Item` (a _RAW_COARSE keyword) *and*
# `$(` (which opens the _INDIRECTION_RE gate), which is what makes M1 the crux.
_PS_BODY = """\
$parseErrors | ForEach-Object { Write-Host "PARSE ERROR line $($_.Extent.StartLineNumber)" }
try { Check "fresh (0h) -> quarantined" 'quarantined' }
finally { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force } }
"""

_HEREDOC_WRITE = f"cat <<'EOF' > tests/Test-SweepQuarantine.ps1\n{_PS_BODY}EOF"


def _bash(command: str) -> ClassifiedAction:
    return classify_tool_call("Bash", {"command": command})


# --------------------------------------------------------------------------- #
# M — characterization matrix (T3)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# T1 — redaction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
        "github_pat_11ABCDEFG0abcdefghijklmnop",
        "xoxb-123456789012-abcdefghijkl",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    ],
)
def test_t1_known_token_shapes_do_not_survive_redaction(secret: str) -> None:
    assert secret not in redact(f"curl -H 'Authorization: Bearer {secret}' https://x/y")


def test_t1_labelled_values_and_url_userinfo_are_masked() -> None:
    assert "hunter2" not in redact("gh auth login --token hunter2")
    assert "s3cr3t" not in redact("git clone https://user:s3cr3t@example.com/r.git")
    assert "abc123XYZ" not in redact("export DEPLOY_SECRET=abc123XYZ")


# --------------------------------------------------------------------------- #
# T2 — layer A carries no input-derived data
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# T4 — record shape / message compatibility
# --------------------------------------------------------------------------- #
