"""OPERATOR-TASK lane (T-operator-task-lane PR2: D1', D2 - D2⁷, D3 - D5, D3⁸).

A ``NEXT: operator`` head carries work a person would otherwise have to pick up by hand. This
package lets the conductor's scheduled wrapper hand ONE such task to ONE fresh, isolated Claude Code
session and post the outcome back into the thread as an ``operator`` report.

Module map:

* :mod:`.report` — the report format, ``validate_report`` (the single format check, D2⁷), the
  ``run_id`` marker and ``posted()`` (D2⁵ / D2⁶).
* :mod:`.state` — the three state files under ``state\\``: the mutex (D3⁸), the lock (D3''' - D3⁸)
  and the ``heads`` record (D3''), plus the atomic writer.
* :mod:`.guard` — the static firewall audit (D2⁗) and the GitHub branch-rule check (D2'' (a)).
* :mod:`.session` — the ``Spawner`` (detached runner) and the session launcher (``mw-operator``,
  Job Object).
* :mod:`.runner` — ``run-session``: one task, one session, then the post (D2'', D2⁶, D2⁷).
* :mod:`.lane_pass` — the conductor's operator pass: reclaim, completion judgement, dispatch.
* :mod:`.cli` — ``python -m spirrow_mindwire.operator_lane``.

What is a guarantee and what is not (D2' as amended by D2''): magickit is out of the session's
reach because the OS blocks it (a dedicated user + a firewall rule audited before every launch),
and ``main`` is out of reach because GitHub's branch rules require a pull request (checked before
every launch). Everything else — the deny rules in the session settings, the keyword pre-screen —
only reduces accidents; a breach is detected after the fact (``OPERATOR-LANE: audit-hit``), not
prevented.
"""
