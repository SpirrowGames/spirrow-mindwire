"""Run the implementer's turn as a separate low-privilege Windows user (ADR-2026-10-09-23
unit 2a: S7 turn isolation, the owner-side checks of S8, the as-user launcher).

Off unless ``[turn.isolation] user`` is set. In 2a nothing in the loop calls into this package
yet: pointing the adapters at the launcher and running the check from the sweep wait for the
operator spike (T-executor-pool-under-one-conductor msg-7100 item 5).
"""
