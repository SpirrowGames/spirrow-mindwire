"""Model / CLI selection reaches the two Anthropic-routed roles, and only those.

The behavioural half is ordinary wiring: ``[loop].role_model`` /
``[loop].role_cli_path`` have to arrive in the ``ClaudeAgentOptions`` the
proposer and the implementer spawn with, and nothing may arrive when they are
unset.

The half worth writing carefully is the *negative* one. A newer CLI does not
degrade the naysayer's Lexora route, it breaks it — measured 2026-09-23, same
host, same options, only the binary differing:

    bundled 2.1.133  ->  reply returned, model_usage == {"naysayer": ...}
    PATH    2.1.280  ->  422 body.messages[1].role: expected 'user' or
                         'assistant', got 'system'

So "the naysayer keeps the vendored CLI" is a correctness constraint, not a
preference, and the tests below pin it structurally (the adapter has no
parameter to pass, and its module never calls the helper) rather than by
asserting on one call site that a later refactor could add a second of.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.adapters._cli_selection import cli_selection_kwargs
from spirrow_mindwire.adapters.claude_code_sdk import ClaudeCodeSdkAdapter
from spirrow_mindwire.adapters.implementer import (
    ImplementerSdkAdapter,
    _default_sdk_executable_path,
)
from spirrow_mindwire.adapters.naysayer_sdk import NaysayerSdkAdapter
from spirrow_mindwire.config import MindwireSettings, Stage3LoopConfig
from spirrow_mindwire.loop_runner import (
    _resolve_role_cli_path_or_exit,
    build_implementer,
    build_proposer,
)
from spirrow_mindwire.naysayer.principles import NAYSAYER_MODEL_TIER
from spirrow_mindwire.obligations import load_manifest
from spirrow_mindwire.value_objects import Role

_OBLIGATIONS = load_manifest()

_ADAPTERS_DIR = Path(__file__).resolve().parents[1] / "src" / "spirrow_mindwire" / "adapters"


# --------------------------------------------------------------------------- #
# the helper
# --------------------------------------------------------------------------- #


def test_choosing_nothing_produces_nothing() -> None:
    """The default has to be the absence of the keys, not the keys set to None."""
    assert cli_selection_kwargs() == {}


def test_each_choice_travels_under_its_own_key() -> None:
    assert cli_selection_kwargs(model="claude-opus-5-5") == {"model": "claude-opus-5-5"}
    assert cli_selection_kwargs(cli_path="/opt/claude") == {"cli_path": "/opt/claude"}


def test_a_path_is_handed_over_as_a_string(tmp_path: Path) -> None:
    """The SDK spawns this; ``Path`` would reach the subprocess layer unconverted."""
    kwargs = cli_selection_kwargs(cli_path=tmp_path / "claude.exe")
    assert kwargs["cli_path"] == str(tmp_path / "claude.exe")
    assert isinstance(kwargs["cli_path"], str)


# --------------------------------------------------------------------------- #
# the proposer's session carries the choice
# --------------------------------------------------------------------------- #


class _FakeClient:
    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


def _capture() -> tuple[list[Any], Any]:
    captured: list[Any] = []

    def factory(options: Any) -> Any:
        captured.append(options)
        return _FakeClient()

    return captured, factory


@pytest.mark.anyio
async def test_the_proposer_session_runs_the_model_and_binary_it_was_given(
    tmp_path: Path,
) -> None:
    from spirrow_mindwire.ports import SpawnContext
    from spirrow_mindwire.value_objects import Event, ReplyDraft, ThreadRef

    captured, factory = _capture()

    async def on_reply(_draft: ReplyDraft) -> None:
        return None

    async def on_event_log(_event: Event) -> None:
        return None

    adapter = ClaudeCodeSdkAdapter(
        cwd=tmp_path,
        model="claude-opus-5-5",
        cli_path=tmp_path / "claude.exe",
        client_factory=factory,
    )
    await adapter.spawn(
        ThreadRef(project_id="p", thread_id="T", chatroom_uri="mc://t/1"),
        Role.PROPOSER,
        SpawnContext(
            on_reply=on_reply,
            on_event_log=on_event_log,
            own_role=Role.PROPOSER,
            own_instance_id="proposer-1",
        ),
    )
    assert captured[0].model == "claude-opus-5-5"
    assert captured[0].cli_path == str(tmp_path / "claude.exe")


@pytest.mark.anyio
async def test_a_proposer_given_no_choice_leaves_the_sdk_defaults_alone(tmp_path: Path) -> None:
    from spirrow_mindwire.ports import SpawnContext
    from spirrow_mindwire.value_objects import Event, ReplyDraft, ThreadRef

    captured, factory = _capture()

    async def on_reply(_draft: ReplyDraft) -> None:
        return None

    async def on_event_log(_event: Event) -> None:
        return None

    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=factory)
    await adapter.spawn(
        ThreadRef(project_id="p", thread_id="T", chatroom_uri="mc://t/1"),
        Role.PROPOSER,
        SpawnContext(
            on_reply=on_reply,
            on_event_log=on_event_log,
            own_role=Role.PROPOSER,
            own_instance_id="proposer-1",
        ),
    )
    assert captured[0].model is None
    assert captured[0].cli_path is None


# --------------------------------------------------------------------------- #
# the implementer's session, and the belt that has to point at the same binary
# --------------------------------------------------------------------------- #


def test_the_implementer_session_runs_the_model_and_binary_it_was_given(tmp_path: Path) -> None:
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="https://api.anthropic.com",
        model="claude-opus-5-5",
        cli_path=tmp_path / "claude.exe",
    )
    options = adapter._make_options()
    assert options.model == "claude-opus-5-5"
    assert options.cli_path == str(tmp_path / "claude.exe")


def test_an_implementer_given_no_choice_leaves_the_sdk_defaults_alone(tmp_path: Path) -> None:
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="https://api.anthropic.com",
    )
    options = adapter._make_options()
    assert options.model is None
    assert options.cli_path is None


def test_the_leftover_belt_looks_for_the_binary_the_session_actually_runs(tmp_path: Path) -> None:
    """Launch path and recognition path are the same binary, or the belt never matches.

    ``lookup_and_assign_leftover`` compares a leftover child's real absolute
    path against this string. Left on the vendored default while the session
    runs a host install, both sides are real, absolute, and different — so the
    belt silently stops adopting anything.
    """
    cli = tmp_path / "claude.exe"
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="https://api.anthropic.com",
        cli_path=cli,
    )
    assert adapter._sdk_executable_path == str(cli)


def test_an_explicit_belt_target_still_wins_over_the_cli_path(tmp_path: Path) -> None:
    """The v12 constructor arg is the test seam; ``cli_path`` supplies a default, not an
    override."""
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="https://api.anthropic.com",
        cli_path=tmp_path / "claude.exe",
        sdk_executable_path=str(tmp_path / "explicit.exe"),
    )
    assert adapter._sdk_executable_path == str(tmp_path / "explicit.exe")


def test_no_cli_path_leaves_the_belt_on_the_vendored_binary(tmp_path: Path) -> None:
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="https://api.anthropic.com",
    )
    assert adapter._sdk_executable_path == _default_sdk_executable_path()


# --------------------------------------------------------------------------- #
# the naysayer is excluded structurally, not by convention
# --------------------------------------------------------------------------- #


def test_the_naysayer_adapter_has_no_cli_path_to_pass() -> None:
    """A caller cannot put the naysayer on a newer CLI, because there is no parameter for it."""
    params = inspect.signature(NaysayerSdkAdapter.__init__).parameters
    assert "cli_path" not in params


def test_the_naysayer_module_never_reaches_for_the_selection_helper() -> None:
    """AST, not a substring: naming the helper in a docstring is not calling it."""
    tree = ast.parse((_ADAPTERS_DIR / "naysayer_sdk.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            assert name != "cli_selection_kwargs", (
                "naysayer_sdk.py selects a CLI binary. A CLI newer than the vendored one "
                "breaks its Lexora route with HTTP 422 (see adapters/_cli_selection), which "
                "would take the independent-review leg down with it"
            )


def test_the_naysayer_keeps_its_own_tier(tmp_path: Path) -> None:
    adapter = NaysayerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lexora.invalid:8110",
    )
    assert adapter._model == NAYSAYER_MODEL_TIER


# --------------------------------------------------------------------------- #
# composition root: config -> the two roles, and a fail-loud bad path
# --------------------------------------------------------------------------- #


def test_build_proposer_forwards_the_pair(tmp_path: Path) -> None:
    proposer = build_proposer(tmp_path, model="claude-opus-5-5", cli_path=tmp_path / "claude.exe")
    assert proposer._model == "claude-opus-5-5"
    assert proposer._cli_path == tmp_path / "claude.exe"


def test_build_implementer_forwards_the_pair(tmp_path: Path) -> None:
    implementer = build_implementer(
        tmp_path,
        obligations=_OBLIGATIONS,
        model="claude-opus-5-5",
        cli_path=tmp_path / "claude.exe",
    )
    assert implementer._model == "claude-opus-5-5"
    assert implementer._cli_path == tmp_path / "claude.exe"


class _InertMcp:
    """Enough of ``McpToolCaller`` to assemble a dispatcher; never called here."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:  # pragma: no cover
        raise AssertionError(f"the composition root should not call {name}")


def test_the_composition_root_routes_config_to_the_two_anthropic_roles(tmp_path: Path) -> None:
    """``[loop]`` -> proposer + implementer; the naysayer is skipped at the same call site."""
    from spirrow_mindwire.loop_runner import _build_dispatcher

    cli = tmp_path / "claude.exe"
    cli.write_text("", encoding="utf-8")
    settings = MindwireSettings(
        loop=Stage3LoopConfig(
            repo_dir=tmp_path,
            role_model="claude-opus-5-5",
            role_cli_path=cli,
        )
    )

    _, registry, _ = _build_dispatcher(
        settings,
        mcp=_InertMcp(),
        proposer=None,
        implementer=None,
        naysayer=None,
    )

    proposer = registry.qualified_for(Role.PROPOSER)[0]
    implementer = registry.qualified_for(Role.IMPLEMENTER)[0]
    naysayer = registry.qualified_for(Role.NAYSAYER)[0]

    assert proposer._model == "claude-opus-5-5"  # type: ignore[attr-defined]
    assert proposer._cli_path == cli  # type: ignore[attr-defined]
    assert implementer._model == "claude-opus-5-5"  # type: ignore[attr-defined]
    assert implementer._cli_path == cli  # type: ignore[attr-defined]
    # Not "the naysayer got None" — it has nowhere to put one, and its model is
    # the tier that makes it a different distribution from the two above.
    assert not hasattr(naysayer, "_cli_path")
    assert naysayer._model == NAYSAYER_MODEL_TIER  # type: ignore[attr-defined]


def test_a_configured_binary_that_is_not_there_stops_the_daemon(tmp_path: Path) -> None:
    """At startup, once — not as a spawn failure repeated every five-minute tick."""
    missing = tmp_path / "nope" / "claude.exe"
    with pytest.raises(SystemExit, match="role_cli_path"):
        _resolve_role_cli_path_or_exit(missing)


def test_a_directory_is_not_an_executable(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="role_cli_path"):
        _resolve_role_cli_path_or_exit(tmp_path)


def test_a_real_binary_passes_through(tmp_path: Path) -> None:
    cli = tmp_path / "claude.exe"
    cli.write_text("", encoding="utf-8")
    assert _resolve_role_cli_path_or_exit(cli) == cli


def test_unset_stays_unset() -> None:
    assert _resolve_role_cli_path_or_exit(None) is None


# --------------------------------------------------------------------------- #
# config surface
# --------------------------------------------------------------------------- #


def test_the_loop_block_defaults_to_choosing_neither() -> None:
    cfg = MindwireSettings().loop
    assert cfg.role_model is None
    assert cfg.role_cli_path is None


def test_the_loop_block_carries_both_choices(tmp_path: Path) -> None:
    cfg = Stage3LoopConfig(
        repo_dir=tmp_path,
        role_model="claude-opus-5-5",
        role_cli_path=tmp_path / "claude.exe",
    )
    assert cfg.role_model == "claude-opus-5-5"
    assert cfg.role_cli_path == tmp_path / "claude.exe"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blanking_the_setting_means_unset_not_empty(blank: str) -> None:
    """``MINDWIRE_LOOP__ROLE_MODEL=`` is how an operator clears this, not how they break it.

    Taken literally, the two failures are ``--model ""`` and a ``cli_path`` of
    ``Path(".")`` — a directory offered to the SDK as an executable.
    """
    # Validated from a raw mapping, the shape TOML / env actually deliver.
    cfg = Stage3LoopConfig.model_validate({"role_model": blank, "role_cli_path": blank})
    assert cfg.role_model is None
    assert cfg.role_cli_path is None
