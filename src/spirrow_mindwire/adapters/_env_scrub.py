"""Blank the daemon's ``MINDWIRE_*`` secrets in every agent subprocess (I-4, I-4b).

T-gate-records-submitted-review-id, msg-9923 I-4 (go: msg-9931 → Einstein). Phase 0
(msg-9922) observed that an implementer turn's shell held
``MINDWIRE_NAYSAYER_GITHUB_TOKEN`` — the review-side ``spirrowgames-ops`` token — because
the Claude Agent SDK builds its child's environment as ``{**os.environ, **options.env}``
(``claude_agent_sdk/_internal/transport/subprocess_cli.py``). ``options.env`` can override
a name, never remove it, so the only lever an adapter has is to override every secret
name with the empty string. Every reader in this package resolves these with
``os.environ.get(...) or ...``, so an empty value reads exactly as unset.

What this is NOT
----------------
It is one layer of defence in depth, not a boundary. The agent still runs as the same
OS user as the daemon, so it can read the daemon's process environment, the
Credential Manager, and any file the daemon can read. Removing the names from the
child's env stops casual and accidental use; it is **not** grounds for enabling the
gate's signed record (msg-9923 I-4 says so explicitly). That needs the turn-isolation
launcher, which builds the child env from scratch and is unaffected by this module.

I-4b (msg-9953, go: Einstein after msg-9953) widened the names from ``_TOKEN`` only to every
``MINDWIRE_*`` name ending in one of :data:`SECRET_ENV_NAME_SUFFIXES` — a Discord webhook
URL (``MINDWIRE_NOTIFY_DISCORD_WEBHOOK``) is a posting credential on its own. The suffix
set is a single constant pinned by a test, so changing it needs a reviewed diff.

``keep`` lets an adapter pass through a token that is its own identity. The implementer
keeps ``MINDWIRE_GITHUB_TOKEN``: it is the author identity its ``gh`` is already logged in
as, and ``mindwire pr-diff-size`` (which the fix-now rule makes it run) authenticates with
it. Keeping it widens nothing the turn did not already hold.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

__all__ = ["SECRET_ENV_NAME_RE", "SECRET_ENV_NAME_SUFFIXES", "scrubbed_secret_env"]

# The name endings that mark a ``MINDWIRE_*`` variable as a secret (I-4b). Pinned by
# ``tests/test_env_scrub.py``; widen it there and here together, under review.
SECRET_ENV_NAME_SUFFIXES: tuple[str, ...] = ("TOKEN", "SECRET", "KEY", "WEBHOOK", "PASSWORD")

# ``MINDWIRE_<anything>_<suffix>``. Windows env names are case-insensitive, so match that way.
SECRET_ENV_NAME_RE = re.compile(
    r"^MINDWIRE_\w+_(?:" + "|".join(SECRET_ENV_NAME_SUFFIXES) + r")$", re.IGNORECASE
)


def scrubbed_secret_env(
    environ: Mapping[str, str] | None = None, *, keep: Iterable[str] = ()
) -> dict[str, str]:
    """``{name: ""}`` for every ``MINDWIRE_*`` secret name in ``environ`` (default ``os.environ``).

    Put this FIRST in an adapter's ``options.env`` so an explicit value the composition root
    sets afterwards still wins. Names in ``keep`` (compared case-insensitively) are left out.
    """
    source = os.environ if environ is None else environ
    kept = {name.upper() for name in keep}
    return {
        name: "" for name in source if SECRET_ENV_NAME_RE.match(name) and name.upper() not in kept
    }
