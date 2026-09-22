"""Which model — and therefore which CLI binary — an Anthropic-routed role session runs on.

The two options are in one module because they are **not independent**. Naming
a model that the bundled CLI is too old for does not fail at the SDK boundary
where it could be read as a typo; it fails at the API, once per turn, as a 400
that names a version number:

    API Error: 400 Claude Code 2.1.133 does not support this model;
    version 2.1.280 or newer is required.

Measured 2026-09-23 on ``sg-tomtebo-01`` for ``claude-opus-5-5``. The SDK
prefers its vendored binary over anything on ``PATH``
(``_find_bundled_cli`` is tried first in
``claude_agent_sdk._internal.transport.subprocess_cli.SubprocessCLITransport._find_cli``),
so the host having a new enough ``claude`` installed does not help by itself —
and bumping the SDK inside this repo's ``claude-agent-sdk>=0.1.0,<0.2`` pin does
not reach it either: 0.1.77 bundles CLI 2.1.133 and 0.1.81 (the newest under the
pin) bundles 2.1.139, both below the floor. ``ClaudeAgentOptions.cli_path`` is
what overrides the bundled binary, which is why a caller that sets ``model``
almost always has to set ``cli_path`` as well.

Why the naysayer does not get this
----------------------------------
The design-time naysayer routes inference through Lexora's Anthropic-compatible
gateway (``MINDWIRE_NAYSAYER_BASE_URL``) rather than ``api.anthropic.com``, and
a newer CLI **breaks that route**. Measured the same day, same options, same
host, only the binary differing:

    bundled 2.1.133  ->  reply returned, model_usage == {"naysayer": ...}
    PATH    2.1.280  ->  API Error: 422 body.messages[1].role
                         "Input should be 'user' or 'assistant'", input: "system"

The newer CLI puts a ``system``-role entry inside ``messages[]``; the gateway
rejects it. So the independent-review leg stays on the vendored binary, and
:class:`~spirrow_mindwire.adapters.naysayer_sdk.NaysayerSdkAdapter` deliberately
has no ``cli_path`` parameter to pass — the constraint is structural rather than
a comment asking the next caller not to. Its ``model`` is the ``naysayer`` tier
alias (``naysayer.principles.NAYSAYER_MODEL_TIER``), decided by that module, not
by this one.

What ``cli_path`` costs
-----------------------
A vendored binary is pinned by the lockfile; a path is not. Claude Code updates
itself in place, so a role session pointed at a host install can change version
between two ticks with no deploy and no diff. That is the trade being made when
this is set: reaching a model the pin cannot reach, at the price of the CLI
version no longer being a property of the lock. Left unset — the default — every
role session is byte-for-byte the bundled behaviour it had before this module
existed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["cli_selection_kwargs"]


def cli_selection_kwargs(
    *,
    model: str | None = None,
    cli_path: str | Path | None = None,
) -> dict[str, Any]:
    """``ClaudeAgentOptions`` kwargs selecting the model / CLI, omitting what is unset.

    Omitting rather than passing ``None`` means a caller that chose neither
    builds the same options object it built before this existed, and means this
    helper never has to restate what the SDK's defaults are in order to express
    "no choice made".
    """
    kwargs: dict[str, Any] = {}
    if model is not None:
        kwargs["model"] = model
    if cli_path is not None:
        kwargs["cli_path"] = str(cli_path)
    return kwargs
