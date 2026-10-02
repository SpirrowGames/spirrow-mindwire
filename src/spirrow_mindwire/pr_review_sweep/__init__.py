"""PR-review sweep (``T-pr-review-threads-outlive-their-prs``).

The sweep's three phases are staged deliberately (msg-2155 D-8):

* **Phase 0** — write-zero measurement. Runs S-pre (is this thread in the sweep's
  population at all?), then S0 (is the PR terminal?) and S1 (is the thread still in
  use?), and reports the size of ``a_union_b`` — the set of threads that are neither
  live nor attached to a live PR. Its whole output is a go/no-go number.
* **Phase 1** — write-zero sorting (:mod:`.phase1`). Each in-scope ledger goes to one of
  ``TERMINAL_MERGED`` / ``TERMINAL_CLOSED`` / ``OPEN`` / ``SKIP`` / ``UNPARSEABLE`` by
  its PR's state, read with mindwire's own GitHub client. It does **not** use the
  ledger's ``can_close()``: the 1a restart (msg-5808) withdrew that plan, because a 1:1
  PR-review ledger is closed unconditionally once its PR ends, and whether a close is
  *allowed* stays with magickit's own policy rather than a mindwire copy of it.
* **Phase 2** — the actual close of the ``TERMINAL_*`` rows. Irreversible, and not in
  this package yet.

Phases 0 and 1 perform **no writes of any kind**: not to the chatroom, not to the
ledger, not to GitHub. That is not a convention to be observed by careful coding — their
CLIs (``scripts/pr_review_sweep_phase0.py`` / ``..._phase1.py``) call the chatroom only
through a read-only tool allowlist that raises on anything else.
"""

from __future__ import annotations

from .config import (
    GateActiveSince,
    ProjectEntry,
    SweepConfig,
    SweepConfigError,
    load_sweep_config,
    parse_sweep_config,
    thread_prefix_for,
)
from .phase0 import (
    MARGIN_LADDER_SECONDS,
    PROVISIONAL_MARGIN_SECONDS,
    Bucket,
    Classification,
    Excluded,
    Phase0Report,
    ThreadFacts,
    Verdict,
    build_report,
    classify,
    intake_exclusion_reason,
    measurement_offsets_seconds,
    sensitivity_table,
    split_intake,
)
from .phase1 import (
    CLOSE_CANDIDATE_CLASSES,
    LedgerRow,
    Phase1Class,
    Phase1Report,
    classify_ledger,
)

__all__ = [
    "CLOSE_CANDIDATE_CLASSES",
    "MARGIN_LADDER_SECONDS",
    "PROVISIONAL_MARGIN_SECONDS",
    "Bucket",
    "Classification",
    "Excluded",
    "GateActiveSince",
    "LedgerRow",
    "Phase0Report",
    "Phase1Class",
    "Phase1Report",
    "ProjectEntry",
    "SweepConfig",
    "SweepConfigError",
    "ThreadFacts",
    "Verdict",
    "build_report",
    "classify",
    "classify_ledger",
    "intake_exclusion_reason",
    "load_sweep_config",
    "measurement_offsets_seconds",
    "parse_sweep_config",
    "sensitivity_table",
    "split_intake",
    "thread_prefix_for",
]
