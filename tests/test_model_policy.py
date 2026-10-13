"""Tests for :mod:`spirrow_mindwire.conductor.model_policy` (T-spawn-via-claude-bg PR-A).

ACCEPTANCE (msg-6830 §5):
* each of C1-C6 knocked out on its own returns Opus; all passing returns Sonnet;
* a non-column-0 ``ACCEPTANCE:``, a C5 marker in the body, a ``sonnet_exclusion`` record and a
  missing turn kind each return Opus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spirrow_mindwire.conductor.model_policy import (
    ModelChoice,
    ThreadFlags,
    choose_model,
    glob_match,
    parse_handoff_fields,
)
from spirrow_mindwire.config import ModelPolicyConfig, load_settings

POLICY = ModelPolicyConfig()
OPUS = POLICY.opus_model
SONNET = POLICY.sonnet_model

LOCAL_FIX = """\
Please add the new enum value.
TURN-KIND: local-fix
FILES: src/spirrow_mindwire/conductor/stop_kind.py, tests/test_stop_kind.py
ACCEPTANCE: StopKind has the new member and its test passes
NEXT: Heisenberg
"""


def _choose(
    text: str = LOCAL_FIX,
    *,
    repo_kind: str | None = "python",
    flags: ThreadFlags | None = None,
    clean: bool | None = True,
    design_text: str | None = None,
    policy: ModelPolicyConfig = POLICY,
) -> ModelChoice:
    return choose_model(
        handoff_text=text,
        repo_kind=repo_kind,
        thread_flags=flags or ThreadFlags(),
        worktree_clean=clean,
        policy=policy,
        design_text=design_text,
    )


def _failed(reasons: tuple[str, ...]) -> list[str]:
    return [r for r in reasons if ":fail:" in r]


# ─── all conditions pass ──────────────────────────────────────────────────────────────


def test_all_conditions_pass_returns_sonnet_and_reports_every_condition() -> None:
    model, reasons = _choose()
    assert model == SONNET
    assert [r.split(":")[0] for r in reasons] == ["C1", "C2", "C3", "C4a", "C4b", "C5", "C6"]
    assert _failed(reasons) == []


def test_result_unpacks_as_model_reasons_tuple() -> None:
    choice = _choose()
    model, reasons = choice
    assert (model, reasons) == (choice.model, choice.reasons)


@pytest.mark.parametrize(
    "text",
    [
        "TURN-KIND: mechanical\nFILES:\n",
        "TURN-KIND: mechanical\nFILES: docs/x.md\n",
        "TURN-KIND: docs-config\nFILES: docs/a.md, tests/test_x.py, deploy/mindwire.toml.example\n",
    ],
)
def test_other_allow_listed_kinds_return_sonnet(text: str) -> None:
    model, reasons = _choose(text)
    assert model == SONNET, reasons


# ─── each condition knocked out on its own → Opus ─────────────────────────────────────


def test_c1_design_impl_returns_opus() -> None:
    model, reasons = _choose(LOCAL_FIX.replace("local-fix", "design-impl"))
    assert model == OPUS
    assert _failed(reasons) == ["C1:fail:turn-kind=design-impl"]


def test_c1_docs_config_with_a_src_file_returns_opus() -> None:
    model, reasons = _choose(
        "TURN-KIND: docs-config\nFILES: docs/a.md, src/spirrow_mindwire/cli.py\n"
    )
    assert model == OPUS
    assert _failed(reasons) == ["C1:fail:docs-config-outside-globs=src/spirrow_mindwire/cli.py"]


def test_c1_docs_config_without_files_returns_opus() -> None:
    model, reasons = _choose("TURN-KIND: docs-config\nFILES:\n")
    assert model == OPUS
    assert _failed(reasons) == ["C1:fail:docs-config-no-files"]


def test_c1_local_fix_without_files_returns_opus() -> None:
    model, reasons = _choose("TURN-KIND: local-fix\nFILES:\nACCEPTANCE: x\n")
    assert model == OPUS
    assert _failed(reasons) == ["C1:fail:local-fix-no-files"]


def test_c1_local_fix_without_acceptance_returns_opus() -> None:
    text = "\n".join(line for line in LOCAL_FIX.splitlines() if not line.startswith("ACCEPTANCE"))
    model, reasons = _choose(text)
    assert model == OPUS
    assert _failed(reasons) == ["C1:fail:local-fix-no-acceptance"]


@pytest.mark.parametrize("repo_kind", ["ue-cpp", None, "  "])
def test_c2_ue_cpp_or_unknown_repo_returns_opus(repo_kind: str | None) -> None:
    model, reasons = _choose(repo_kind=repo_kind)
    assert model == OPUS
    assert [r.split(":")[0] for r in _failed(reasons)] == ["C2"]


@pytest.mark.parametrize(
    "path",
    [
        "src/spirrow_mindwire/conductor/handoff.py",
        "src/spirrow_mindwire/conductor/head_skip.py",
        "src/spirrow_mindwire/conductor/stand_down.py",
        "src/spirrow_mindwire/conductor/parked_lane.py",
        "src/spirrow_mindwire/pr_review_sweep/phase1.py",
        "deploy/lib/SweepFairness.ps1",
        "deploy/sweep.json.example",
        "SRC/Spirrow_Mindwire/Conductor/Handoff.py",
    ],
)
def test_c3_forbidden_path_returns_opus(path: str) -> None:
    model, reasons = _choose(LOCAL_FIX.replace("tests/test_stop_kind.py", path))
    assert model == OPUS
    assert _failed(reasons) == [f"C3:fail:forbidden-path={path}"]


def test_c4a_recent_trouble_returns_opus() -> None:
    model, reasons = _choose(flags=ThreadFlags(recent_trouble=("REQUEST_CHANGES",)))
    assert model == OPUS
    assert _failed(reasons) == ["C4a:fail:recent-trouble=REQUEST_CHANGES"]


def test_c4b_sonnet_exclusion_returns_opus_even_with_an_empty_recent_window() -> None:
    flags = ThreadFlags(recent_trouble=(), sonnet_excluded=True, exclusion_trigger="forbidden-path")
    model, reasons = _choose(flags=flags)
    assert model == OPUS
    assert _failed(reasons) == ["C4b:fail:sonnet-excluded:forbidden-path"]


def test_c4b_exclusion_without_trigger_still_returns_opus() -> None:
    model, reasons = _choose(flags=ThreadFlags(sonnet_excluded=True))
    assert model == OPUS
    assert _failed(reasons) == ["C4b:fail:sonnet-excluded:unknown"]


@pytest.mark.parametrize("marker", ["仕様の誤り", "訂正", "revised", "Supersedes", "REVISED"])
def test_c5_marker_in_body_returns_opus_with_all_fields_present(marker: str) -> None:
    model, reasons = _choose("The design was " + marker + " last round.\n" + LOCAL_FIX)
    assert model == OPUS
    assert [r.split(":")[0] for r in _failed(reasons)] == ["C5"]


def test_c5_marker_in_separate_design_text_returns_opus() -> None:
    model, reasons = _choose(design_text="msg-6812: 仕様の誤り を直した")
    assert model == OPUS
    assert _failed(reasons) == ["C5:fail:revision-marker=仕様の誤り"]


def test_c6_dirty_worktree_returns_opus() -> None:
    model, reasons = _choose(clean=False)
    assert model == OPUS
    assert _failed(reasons) == ["C6:fail:dirty-worktree"]


def test_c6_unknown_worktree_returns_opus() -> None:
    model, reasons = _choose(clean=None)
    assert model == OPUS
    assert _failed(reasons) == ["C6:fail:worktree-unknown"]


# ─── the structured-field reading rule ────────────────────────────────────────────────


def test_acceptance_not_at_column_0_does_not_count() -> None:
    text = (
        LOCAL_FIX.replace("ACCEPTANCE: StopKind", "The ACCEPTANCE: StopKind")
        + "  ACCEPTANCE: indented does not count either\n"
    )
    model, reasons = _choose(text)
    assert model == OPUS
    assert _failed(reasons) == ["C1:fail:local-fix-no-acceptance"]


def test_blank_acceptance_does_not_count() -> None:
    model, reasons = _choose(
        LOCAL_FIX.replace("ACCEPTANCE: StopKind has the new member", "ACCEPTANCE:").replace(
            " and its test passes", ""
        )
    )
    assert model == OPUS
    assert _failed(reasons) == ["C1:fail:local-fix-no-acceptance"]


@pytest.mark.parametrize(
    "text",
    [
        "Just do the thing.\nFILES: docs/a.md\n",
        "  TURN-KIND: mechanical\nFILES: docs/a.md\n",
        "Use TURN-KIND: mechanical here\nFILES: docs/a.md\n",
        "turn-kind: mechanical\nFILES: docs/a.md\n",
        "TURN-KIND: mechanical\n",
        "TURN-KIND: trivial\nFILES: docs/a.md\n",
        "TURN-KIND: mechanical\nTURN-KIND: mechanical\nFILES: docs/a.md\n",
        "TURN-KIND: mechanical\nFILES: docs/a.md\nFILES: docs/b.md\n",
        "TURN-KIND: mechanical\nFILES: docs/a.md, ../outside.md\n",
        "TURN-KIND: mechanical\nFILES: /etc/passwd\n",
        "TURN-KIND: mechanical\nFILES: C:/x/y.md\n",
        "TURN-KIND: mechanical\nFILES: docs/a.md,,docs/b.md\n",
        "TURN-KIND: mechanical\nFILES: `docs/a.md`\n",
        "",
    ],
)
def test_missing_or_malformed_fields_return_opus_with_no_turn_kind(text: str) -> None:
    model, reasons = _choose(text)
    assert model == OPUS
    c1 = next(r for r in reasons if r.startswith("C1:"))
    assert c1.startswith("C1:fail:no-turn-kind")
    c3 = next(r for r in reasons if r.startswith("C3:"))
    assert c3 == "C3:fail:files-unknown"


def test_parse_reads_fields_and_normalises_paths() -> None:
    fields = parse_handoff_fields(
        "TURN-KIND: local-fix\nFILES: ./src\\a.py , tests/b.py\nACCEPTANCE: one\nACCEPTANCE: two\n"
    )
    assert fields.usable
    assert fields.turn_kind == "local-fix"
    assert fields.files == ("src/a.py", "tests/b.py")
    assert fields.acceptance == ("one", "two")


def test_parse_reads_fields_inside_a_code_fence() -> None:
    # msg-6830 §5 hands PR-A over with the fields inside a ``` block; lines still start at col 0.
    fields = parse_handoff_fields("```\nTURN-KIND: design-impl\nFILES: a.py\n```\n")
    assert fields.turn_kind == "design-impl"
    assert fields.files == ("a.py",)


# ─── globs and config ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("docs/a/b.md", "docs/**", True),
        ("README.md", "**/*.md", True),
        ("docs/x/README.md", "**/*.md", True),
        ("src/a.py", "src/*.py", True),
        ("src/a/b.py", "src/*.py", False),
        ("scripts/pr_review_sweep_phase0.py", "scripts/pr_review_sweep_*", True),
        ("deploy/sweep.json.example", "deploy/**/*sweep*", True),
        ("src/a.py", "src/?.py", True),
        ("src/ab.py", "src/?.py", False),
    ],
)
def test_glob_match(path: str, pattern: str, expected: bool) -> None:
    assert glob_match(path, [pattern], ignore_case=False) is expected


def test_docs_config_glob_is_case_sensitive() -> None:
    model, reasons = _choose("TURN-KIND: docs-config\nFILES: DOCS/a.py\n")
    assert model == OPUS
    assert _failed(reasons) == ["C1:fail:docs-config-outside-globs=DOCS/a.py"]


def test_custom_policy_models_and_lists_are_honoured() -> None:
    policy = ModelPolicyConfig(
        opus_model="o",
        sonnet_model="s",
        sonnet_forbidden_paths=("tests/**",),
        revision_markers=("zzz",),
    )
    assert _choose(policy=policy)[0] == "o"  # tests/test_stop_kind.py is now forbidden
    text = LOCAL_FIX.replace(", tests/test_stop_kind.py", "")
    assert _choose(text, policy=policy)[0] == "s"
    assert _choose(text + "zzz\n", policy=policy)[0] == "o"


def test_model_policy_section_loads_from_toml(tmp_path: Path) -> None:
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        'schema_version = 1\n[model_policy]\nsonnet_forbidden_paths = ["a/**"]\n'
        'revision_markers = ["fix-the-spec"]\nrecent_window = 5\n',
        encoding="utf-8",
    )
    settings = load_settings(cfg)
    assert settings.model_policy.sonnet_forbidden_paths == ("a/**",)
    assert settings.model_policy.revision_markers == ("fix-the-spec",)
    assert settings.model_policy.recent_window == 5
    assert settings.model_policy.opus_model == "claude-opus-5-5"


def test_example_config_model_policy_section_matches_defaults() -> None:
    example = Path(__file__).resolve().parents[1] / "deploy" / "mindwire.toml.example"
    import tomllib

    data = tomllib.loads(example.read_text(encoding="utf-8"))
    assert ModelPolicyConfig(**data["model_policy"]) == ModelPolicyConfig()


_DEFAULTED_LISTS = ("sonnet_forbidden_paths", "docs_config_globs", "revision_markers")


def test_example_config_leaves_policy_lists_to_code_defaults() -> None:
    """The lists stay commented out so a copied example cannot pin stale values (#480 advisory),
    yet the commented text must still mirror the code defaults when uncommented."""
    import tomllib

    example = Path(__file__).resolve().parents[1] / "deploy" / "mindwire.toml.example"
    text = example.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    for key in _DEFAULTED_LISTS:
        assert key not in data["model_policy"]
    section = text[text.index("[model_policy]") :]
    section = section[: section.index("\nrecent_window")]
    # Uncomment only the TOML lines of the defaulted lists: the key line, its items, the closer.
    toml_lines = [
        line[2:]
        for line in section.splitlines()
        if line.startswith(tuple(f"# {k} =" for k in _DEFAULTED_LISTS))
        or line.startswith(("#   ", "# ]"))
    ]
    parsed = tomllib.loads("\n".join(toml_lines))
    defaults = ModelPolicyConfig()
    for key in _DEFAULTED_LISTS:
        assert tuple(parsed[key]) == getattr(defaults, key)


@pytest.mark.parametrize(
    "bad",
    [
        {"sonnet_forbidden_paths": ["ok/**", " "]},
        {"revision_markers": [""]},
        {"opus_model": ""},
        {"recent_window": 0},
        {"unknown_key": 1},
    ],
)
def test_model_policy_config_rejects_bad_values(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ModelPolicyConfig.model_validate(bad)
