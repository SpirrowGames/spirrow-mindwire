"""Run the implementer's turn as a separate low-privilege Windows user (ADR-2026-10-09-23
unit 2a: S7 turn isolation, the owner-side checks of S8, the as-user launcher).

Off unless ``[turn.isolation] user`` is set (INV-0: no check, no launcher, the direct spawn).
When set, each conductor launch runs the check in-process through :mod:`.gate` and the
implementer adapter takes the launcher route only if it passed; any failure is exit 10 and one
stop line. The route's ``cli_path`` shim and SDK environment wait for the operator spike
(T-executor-pool-under-one-conductor msg-8340), so until then the route refuses by name.
"""
