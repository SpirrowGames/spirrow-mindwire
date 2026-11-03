"""1c: fire the first PR-gate of an open PR (chatroom ``T-gate-fires-on-pr-open``).

Pure decision in :mod:`.decide`, I/O in :mod:`.runner`, CLI in :mod:`.__main__`
(``python -m spirrow_mindwire.pr_event_advance.auto_gate``), called once per tick by
``deploy/run-conductor-scheduled.ps1``.
"""
