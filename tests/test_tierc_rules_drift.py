"""T-tier-c-six-articles-security-boundary part 2 — the runtime rules file vs the template.

Spec: Bohr msg-9912 A (a warning, never a refusal — Einstein msg-9913).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spirrow_mindwire.decider.questions import TierCRulesError, tierc_rules_template_path
from spirrow_mindwire.preflight import tierc_rules_drift

TEMPLATE = tierc_rules_template_path()


def _write(path: Path, rules: list[tuple[str, str]]) -> Path:
    path.write_text(
        "".join(f'[[rule]]\nid = "{rid}"\ntext = "{text}"\n\n' for rid, text in rules),
        encoding="utf-8",
    )
    return path


def test_the_shipped_template_matches_itself() -> None:
    assert tierc_rules_drift(TEMPLATE, TEMPLATE) is None


def test_comments_and_formatting_do_not_warn(tmp_path: Path) -> None:
    copy = tmp_path / "tierc_rules.toml"
    copy.write_text("# local copy\n\n" + TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    assert tierc_rules_drift(copy) is None


def test_a_file_without_rule_6_warns_and_names_it(tmp_path: Path) -> None:
    template = _write(tmp_path / "t.toml", [("rule_1", "a"), ("rule_6", "six")])
    runtime = _write(tmp_path / "r.toml", [("rule_1", "a")])
    warning = tierc_rules_drift(runtime, template)
    assert warning is not None
    assert "missing rule_6" in warning
    assert "runtime rules_sha256=" in warning and "template rules_sha256=" in warning


def test_reworded_and_extra_rules_are_named(tmp_path: Path) -> None:
    template = _write(tmp_path / "t.toml", [("rule_1", "a"), ("rule_2", "b")])
    runtime = _write(
        tmp_path / "r.toml", [("rule_1", "a, edited"), ("rule_2", "b"), ("rule_9", "z")]
    )
    warning = tierc_rules_drift(runtime, template)
    assert warning is not None
    assert "worded differently rule_1" in warning
    assert "not in the template rule_9" in warning
    assert "missing" not in warning


def test_a_missing_runtime_file_is_reported_as_startup_reports_it(tmp_path: Path) -> None:
    with pytest.raises(TierCRulesError, match="init-config tierc-rules"):
        tierc_rules_drift(tmp_path / "absent.toml")
