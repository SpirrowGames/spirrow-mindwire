"""T46: every loop role's whole callable surface, pinned — no agent-to-agent side channel.

Thread ``T-agmsg-transport-lessons-readiness-session-claim-board`` (Fermi msg-3099 §3, redesigned
by Bohr msg-4884 §3 / msg-4886 §1, approved by Einstein msg-4887; build instructions msg-4957).

The invariant: agents talk to each other through the chatroom and nowhere else. A side channel
(Claude Code's native ``SendMessage`` / ``ListAgents`` family, a ``Task`` sub-agent, or an MCP tool
that reaches another session) would leave no trace in the thread the humans and the naysayer read.

Why this is a test and not a runtime name denylist (msg-4886 §1, endorsed msg-4887): a denylist can
only name tools someone already thought of, and an attached MCP server can expose a side channel
under any name. What actually decides the surface is the composition root — which built-ins go into
``tools=`` and which servers go into ``mcp_servers`` — together with the two isolation options that
stop a session from inheriting anything else from the host. So those three things are pinned here,
per role, from the production builders:

(a) the ``tools=`` built-in set holds nothing from the out-of-band communication family;
(b) ``mcp_servers`` holds only in-process SDK servers (``type: "sdk"``) — today, none at all;
(c) the session carries :func:`session_isolation_kwargs` (``setting_sources=[]`` and
    ``strict_mcp_config=True``), so no host connector, plugin or ``.mcp.json`` can widen (a)/(b).

**If you are here because (b) went red:** adding a stdio / http MCP server to a role is allowed, but
the PR that does it must say why the new server opens no agent-to-agent path outside the chatroom,
and it must also redesign T43's readiness rule. ``spawn.ready`` is emitted when ``connect()``
returns, which only means "the session is listening" while every tool is in-process; an
out-of-process server can still be booting at that point (Einstein msg-4885 §3, msg-4887).
"""

from __future__ import annotations

import asyncio
import typing
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig

from spirrow_mindwire.adapters._session_isolation import session_isolation_kwargs
from spirrow_mindwire.claude_code.tools.mindwire_server import build_mindwire_mcp_server
from spirrow_mindwire.loop_runner import build_implementer, build_naysayer, build_proposer
from spirrow_mindwire.obligations import load_manifest
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.value_objects import ReplyDraft, Role, ThreadRef

#: Built-in tools that move a message, or a unit of work, to another agent without the chatroom.
#: ``Task`` / ``Agent`` start a sub-agent; ``SendMessage`` / ``ListAgents`` are the agent-team
#: messaging family agmsg's SKILL.md warns disappears from the shared history.
_OUT_OF_BAND_BUILTINS: frozenset[str] = frozenset({"Task", "Agent", "SendMessage", "ListAgents"})

_OBLIGATIONS = load_manifest()


async def _noop_reply(_draft: ReplyDraft) -> None:
    return None


async def _noop_log(_event: Any) -> None:
    return None


def _proposer_options(tmp_path: Path) -> Any:
    """The options the production proposer actually hands the SDK (it builds them in ``spawn``)."""
    adapter = build_proposer(tmp_path)
    captured: list[Any] = []

    class _Client:
        async def connect(self) -> None:
            return None

        async def query(self, prompt: str) -> None:
            return None

        async def receive_response(self) -> AsyncIterator[Any]:
            messages: list[Any] = []
            for message in messages:
                yield message

        async def interrupt(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    def factory(options: Any) -> _Client:
        captured.append(options)
        return _Client()

    adapter._client_factory = factory
    ctx = SpawnContext(
        on_reply=_noop_reply,
        on_event_log=_noop_log,
        own_role=Role.PROPOSER,
        own_instance_id="proposer-1",
    )
    asyncio.run(
        adapter.spawn(
            ThreadRef(project_id="p", thread_id="t", chatroom_uri="chatroom://p/t"),
            Role.PROPOSER,
            ctx,
        )
    )
    assert len(captured) == 1
    return captured[0]


def _role_options(role: str, tmp_path: Path) -> Any:
    if role == "proposer":
        return _proposer_options(tmp_path)
    if role == "implementer":
        return build_implementer(tmp_path, obligations=_OBLIGATIONS)._make_options()
    if role == "naysayer":
        return build_naysayer(tmp_path, obligations=_OBLIGATIONS)._make_options()
    raise AssertionError(role)


_ROLES = ("proposer", "implementer", "naysayer")


@pytest.mark.parametrize("role", _ROLES)
def test_a_builtin_tools_hold_no_out_of_band_channel(role: str, tmp_path: Path) -> None:
    declared = _role_options(role, tmp_path).tools
    # ``tools=None`` means "not declared", which the SDK may widen to its default built-in set —
    # so an undeclared surface cannot pass this check by looking empty (PR #376 review).
    assert declared is not None, (
        f"{role} does not declare tools= explicitly; the built-in surface is then the SDK "
        f"default and cannot be checked for out-of-band channels (T46 (a))"
    )
    tools = set(declared)
    leaked = tools & _OUT_OF_BAND_BUILTINS
    assert not leaked, (
        f"{role}'s built-in tools include {sorted(leaked)} — a path from one agent to another "
        f"that bypasses the chatroom (T46)"
    )


def _server_type(cfg: Any) -> Any:
    """The transport ``type`` a server config declares, whatever shape the config has.

    In the pinned SDK every server config is a ``TypedDict`` (a plain ``dict`` at runtime), so the
    mapping branch is the live one. The attribute branch is there so that a config object is judged
    by the type it declares rather than rejected for not being a ``dict`` (PR #376 review). A
    config with no ``type`` at all reads as ``None`` — the SDK treats that as stdio, so it is not
    in-process.
    """
    if isinstance(cfg, Mapping):
        return cfg.get("type")
    return getattr(cfg, "type", None)


def _non_sdk_servers(servers: Mapping[str, Any]) -> dict[str, Any]:
    """Name → declared type of every attached server that is not in-process (``type: "sdk"``)."""
    return {
        name: declared for name, cfg in servers.items() if (declared := _server_type(cfg)) != "sdk"
    }


@pytest.mark.parametrize("role", _ROLES)
def test_b_mcp_servers_are_in_process_sdk_only(role: str, tmp_path: Path) -> None:
    # stdio / http servers: see the module docstring — this also re-opens T43's ready rule.
    not_sdk = _non_sdk_servers(dict(_role_options(role, tmp_path).mcp_servers or {}))
    assert not not_sdk, (
        f"{role} attaches non-in-process MCP servers {not_sdk}. Say in the PR why they open no "
        f"agent-to-agent path outside the chatroom (T46), and redesign T43's spawn.ready rule, "
        f"which assumes connect() done == every tool available."
    )


def test_b_production_mcp_servers_are_empty_today(tmp_path: Path) -> None:
    """Today's measured fact behind (b): no loop role attaches any MCP server at all.

    Pinned separately so that adding even an in-process server is a visible diff to this test,
    not a silent pass of the ``type: "sdk"`` check above.
    """
    for role in _ROLES:
        assert dict(_role_options(role, tmp_path).mcp_servers or {}) == {}, role


def test_b_watcher_mcp_server_is_an_sdk_server() -> None:
    """The one MCP server the codebase builds (watcher, ``{"mindwire": ...}``) is in-process."""
    hints = typing.get_type_hints(build_mindwire_mcp_server)
    assert hints["return"] is McpSdkServerConfig


def test_b_check_accepts_a_real_in_process_server() -> None:
    """The (b) check is not vacuous: a real in-process server passes it.

    No role attaches a server today, so the per-role check above never sees one. This feeds it
    what ``create_sdk_mcp_server`` actually returns — the same call the watcher's builder makes —
    so the day a role attaches that server, the check is already known to accept it.
    """
    real = create_sdk_mcp_server(name="probe", version="0.0.0", tools=[])
    assert _non_sdk_servers({"probe": real}) == {}
    # Same verdict when the config is an object rather than a mapping.
    assert _non_sdk_servers({"probe": SimpleNamespace(type="sdk")}) == {}


@pytest.mark.parametrize(
    "cfg, declared",
    [
        ({"type": "stdio", "command": "x"}, "stdio"),
        ({"command": "x"}, None),  # stdio's ``type`` is optional in the SDK
        ({"type": "http", "url": "http://h"}, "http"),
        ({"type": "sse", "url": "http://h"}, "sse"),
        (SimpleNamespace(type="http", url="http://h"), "http"),
        (SimpleNamespace(command="x"), None),
    ],
)
def test_b_check_rejects_out_of_process_servers(cfg: Any, declared: Any) -> None:
    """The (b) check goes red for every out-of-process transport, in either config shape."""
    assert _non_sdk_servers({"probe": cfg}) == {"probe": declared}


@pytest.mark.parametrize("role", _ROLES)
def test_c_session_isolation_applied(role: str, tmp_path: Path) -> None:
    options = _role_options(role, tmp_path)
    for key, value in session_isolation_kwargs().items():
        assert getattr(options, key) == value, (
            f"{role} session is missing isolation option {key}={value!r}: without it the host's "
            f"connectors / plugins / .mcp.json can add tools this test never sees (T46 (c))"
        )
