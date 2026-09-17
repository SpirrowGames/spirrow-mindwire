"""The one definition of "a role session does not inherit this host".

Every SDK-backed role adapter passes these options. They were written three
times before they were written once: ``implementer.py`` had them from T37 #4,
and the proposer and the naysayer were given them only after the proposer's
omission cost a parked thread (see below). Three copies of a
security-relevant invariant is three chances to drift, which is what this
module exists to remove — a fourth option added here reaches every role at
once, and a role that stops calling this is visible as a missing call rather
than as a quietly shorter argument list.

What the options do
-------------------
``setting_sources=[]`` runs the session in SDK isolation mode: this host's
user / project / local settings are not loaded, so the session inherits no
claude.ai connectors, no ``CLAUDE.md``, no hooks, no settings env.
``strict_mcp_config=True`` honors only the ``mcp_servers`` the adapter passes
explicitly, so a project ``.mcp.json`` or a plugin server cannot widen the
session either. Together they leave the composition root as the only thing
that decides what a role may reach.

Why it is not optional
----------------------
On 2026-09-15 the proposer — which had neither option — inherited this host's
account-level claude.ai connector, was handed a thread head asking it to read
a pull request, and called ``mcp__claude_ai_SpirrowMagickit__github``.
``_PathScopeGuard`` refused, correctly: it bounds ``Read`` / ``Glob`` /
``Grep`` and cannot bound an MCP tool's input. The CLI then died with
``AxiosError: Request failed with status code 403``, the conductor recorded
``sdk-error-during-execution``, and the thread was quarantined — a state with
no automatic clear path, so one inherited connector parked a live thread until
a human cleared it.

``mcp_servers={}`` does not substitute for this. An inherited connector does
not arrive through that argument, which is how a naysayer post's provenance
marker could read ``mcp=0`` while the session held connector tools.

Inference routing is independent of settings sources (it comes from the
credentials file and each adapter's ``ANTHROPIC_BASE_URL``), so isolation does
not affect which backend a role talks to.
"""

from __future__ import annotations

from typing import Any

__all__ = ["session_isolation_kwargs"]


def session_isolation_kwargs() -> dict[str, Any]:
    """``ClaudeAgentOptions`` kwargs that keep host settings and MCP config out.

    Returns a fresh dict each call, holding a fresh ``setting_sources`` list:
    these go into a live options object, and a module-level constant would hand
    every adapter the same mutable list to share.
    """
    return {"setting_sources": [], "strict_mcp_config": True}
