"""settled_close: close a thread whose head is a proposer's ``STOP: done`` (boundary 6).

T-done-disposition-auto-close (Bohr msg-8455 §2). Disabled by default: a disabled sweep tick
makes no MCP call, and the dry-run (``mindwire settled-close --dry-run``) only reads. See
:mod:`.classify` for the prediction (magickit decides), :mod:`.runner` for the pass, and
:mod:`.config` for the ``sweep.json`` key.
"""

from __future__ import annotations

from .classify import OwnerKind, SettledClass, ThreadHead, Verdict, classify, refusal_class
from .config import SettledCloseConfig, SettledCloseConfigError, parse_settled_close_config
from .runner import CLOSE_TOOLS, READ_TOOLS, PassReport, render_table, run_pass

__all__ = [
    "CLOSE_TOOLS",
    "READ_TOOLS",
    "OwnerKind",
    "PassReport",
    "SettledClass",
    "SettledCloseConfig",
    "SettledCloseConfigError",
    "ThreadHead",
    "Verdict",
    "classify",
    "parse_settled_close_config",
    "refusal_class",
    "render_table",
    "run_pass",
]
