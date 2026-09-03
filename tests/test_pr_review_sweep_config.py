"""Loader tests for ``deploy/pr_review_sweep.json.example``'s two tables.

The example file is not just documentation here — one test loads the shipped example
itself, so the prose and the parser cannot drift apart the way they did on heads
``0d9b205`` (an example that violated its own placeholder rule) and ``ac5df4e`` (prose
claiming a deduplication the schema did not perform).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from spirrow_mindwire.pr_review_sweep.config import (
    ProjectEntry,
    SweepConfigError,
    load_sweep_config,
    parse_sweep_config,
    thread_prefix_for,
)

_EXAMPLE = Path(__file__).resolve().parents[1] / "deploy" / "pr_review_sweep.json.example"


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": 1,
        "projects": [{"project": "p", "owner": "o", "repo": "r"}],
    }
    base.update(overrides)
    return base


def test_the_shipped_example_loads() -> None:
    """The file an operator is told to copy must survive the loader it is copied for."""
    config = load_sweep_config(_EXAMPLE)
    slugs = [e.project for e in config.entries]
    assert slugs == ["spirrow-mindwire", "spirrow-magickit"]


def test_the_example_shows_both_valid_shapes_of_table_2() -> None:
    """One entry populated, one with the key omitted — the two legal states."""
    config = load_sweep_config(_EXAMPLE)
    populated, undetermined = config.entries
    assert populated.gate_active_since is not None
    assert populated.gate_active_since.date == date(2025, 1, 15)
    assert undetermined.gate_active_since is None


def test_thread_prefix_is_derived_not_configured() -> None:
    entry = ProjectEntry(project="spirrow-mindwire", owner="spirrowgames", repo="spirrow-mindwire")
    assert entry.thread_prefix == "T-pr-review-spirrow-mindwire-"
    assert thread_prefix_for("x") == "T-pr-review-x-"
    # The schema must not carry the prefix; a config that sets one is simply ignored,
    # which is the outcome that keeps the single derivation authoritative.
    config = parse_sweep_config(
        _payload(projects=[{"project": "p", "owner": "o", "repo": "r", "thread_prefix": "NOPE"}])
    )
    assert config.entries[0].thread_prefix == "T-pr-review-p-"


@pytest.mark.parametrize(
    ("thread_id", "expected"),
    [
        ("T-pr-review-p-42", 42),
        ("T-pr-review-p-0", 0),
        ("T-pr-review-q-42", None),  # another project's thread
        ("T-pr-review-p-", None),  # no number
        ("T-pr-review-p-abc", None),
        ("T-pr-review-p-4-2", None),
        ("T-something-else", None),
        # Non-ASCII digits: ``isdigit()`` says yes and ``int()`` would parse them into a
        # number the thread id does not spell. Refuse rather than resolve a wrong PR.
        ("T-pr-review-p-４２", None),  # noqa: RUF001 - fullwidth 42, on purpose
    ],
)
def test_pr_number_parsing(thread_id: str, expected: int | None) -> None:
    entry = ProjectEntry(project="p", owner="o", repo="r")
    assert entry.pr_number_for_thread(thread_id) == expected


def test_placeholder_justification_is_refused() -> None:
    for placeholder in ("", "   ", "unknown", "UNKNOWN", "推定"):
        payload = _payload(
            projects=[
                {
                    "project": "p",
                    "owner": "o",
                    "repo": "r",
                    "gate_active_since": {"date": "2025-01-15", "justification": placeholder},
                }
            ]
        )
        with pytest.raises(SweepConfigError, match="placeholder"):
            parse_sweep_config(payload)


def test_the_refusal_names_the_escape_hatch() -> None:
    """Refusing without saying what to do instead just invites a fake justification."""
    payload = _payload(
        projects=[
            {
                "project": "p",
                "owner": "o",
                "repo": "r",
                "gate_active_since": {"date": "2025-01-15", "justification": ""},
            }
        ]
    )
    with pytest.raises(SweepConfigError) as excinfo:
        parse_sweep_config(payload)
    assert "OMIT" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["2025-01", "2025", "20250115", "2025-01-15T00:00:00Z", "x"])
def test_date_must_be_exactly_yyyy_mm_dd(bad: str) -> None:
    payload = _payload(
        projects=[
            {
                "project": "p",
                "owner": "o",
                "repo": "r",
                "gate_active_since": {"date": bad, "justification": "PR #1"},
            }
        ]
    )
    with pytest.raises(SweepConfigError, match="YYYY-MM-DD"):
        parse_sweep_config(payload)


def test_a_well_formed_but_impossible_date_is_refused() -> None:
    payload = _payload(
        projects=[
            {
                "project": "p",
                "owner": "o",
                "repo": "r",
                "gate_active_since": {"date": "2025-02-30", "justification": "PR #1"},
            }
        ]
    )
    with pytest.raises(SweepConfigError, match="not a real date"):
        parse_sweep_config(payload)


def test_duplicate_project_slugs_are_refused() -> None:
    """The gate's advisory on head d718ebc: an array cannot enforce key uniqueness.

    The loader must not pick one arbitrarily — a silently dropped entry means a whole
    project goes unswept while the config looks correct.
    """
    payload = _payload(
        projects=[
            {"project": "p", "owner": "o", "repo": "r"},
            {"project": "p", "owner": "o2", "repo": "r2"},
        ]
    )
    with pytest.raises(SweepConfigError, match="duplicate project"):
        parse_sweep_config(payload)


@pytest.mark.parametrize("missing", ["project", "owner", "repo"])
def test_every_identifier_is_required(missing: str) -> None:
    row = {"project": "p", "owner": "o", "repo": "r"}
    del row[missing]
    with pytest.raises(SweepConfigError, match=missing):
        parse_sweep_config(_payload(projects=[row]))


def test_owner_and_repo_are_never_inferred_from_project() -> None:
    """msg-2162 ③: the GitHub tuple is a different namespace and must be written out."""
    with pytest.raises(SweepConfigError):
        parse_sweep_config(_payload(projects=[{"project": "spirrow-mindwire"}]))


@pytest.mark.parametrize("version", [0, 2, "1", None])
def test_unknown_schema_version_is_refused(version: object) -> None:
    with pytest.raises(SweepConfigError, match="schema_version"):
        parse_sweep_config(_payload(schema_version=version))


def test_projects_must_be_a_non_empty_array() -> None:
    bads: list[object] = [[], {}, "x", None]
    for bad in bads:
        with pytest.raises(SweepConfigError, match="projects"):
            parse_sweep_config(_payload(projects=bad))


def test_unreadable_and_malformed_files_raise_sweep_config_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(SweepConfigError, match="cannot read"):
        load_sweep_config(missing)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(SweepConfigError, match="not valid JSON"):
        load_sweep_config(broken)


def test_round_trip_through_a_file(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")
    config = load_sweep_config(path)
    assert config.entry_for_project("p") == ProjectEntry(project="p", owner="o", repo="r")
    assert config.entry_for_project("absent") is None
