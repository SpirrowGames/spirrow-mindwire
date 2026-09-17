"""PR-review sweep (``T-pr-review-threads-outlive-their-prs``).

Phase 0 only. The sweep's three phases are staged deliberately (msg-2155 D-8):

* **Phase 0** — write-zero measurement. Runs S-pre (is this thread in the sweep's
  population at all?), then S0 (is the PR terminal?) and S1 (is the thread still in
  use?), and reports the size of ``a_union_b`` — the set of threads that are neither
  live nor attached to a live PR. Its whole output is a go/no-go number.
* **Phase 1** — the A/B split, which needs the ledger's ``can_close()`` predicate and
  therefore a different repository. Not in this package yet.
* **Phase 2** — the actual close. Irreversible, gated behind both earlier phases.

Only Phase 0 exists here, and it performs **no writes of any kind**: not to the
chatroom, not to the ledger, not to GitHub. That is not a convention to be observed
by careful coding — :mod:`spirrow_mindwire.pr_review_sweep.phase0` enforces it with a
read-only tool allowlist that raises on anything else.
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

__all__ = [
    "MARGIN_LADDER_SECONDS",
    "PROVISIONAL_MARGIN_SECONDS",
    "Bucket",
    "Classification",
    "Excluded",
    "GateActiveSince",
    "Phase0Report",
    "ProjectEntry",
    "SweepConfig",
    "SweepConfigError",
    "ThreadFacts",
    "Verdict",
    "build_report",
    "classify",
    "intake_exclusion_reason",
    "load_sweep_config",
    "measurement_offsets_seconds",
    "parse_sweep_config",
    "sensitivity_table",
    "split_intake",
    "thread_prefix_for",
]
