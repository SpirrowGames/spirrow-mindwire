"""ledger_drift — detect and report where the Magickit task ledger lags reality. Read only.

T-ledger-writeback-on-merge-and-thread-close-on-task-done, design v0.3 (Bohr msg-9524 /
msg-9526, accepted by Einstein's go after msg-9526). Nothing here writes to the ledger or to a
chatroom thread: status changes and note appends were cut from this thread (msg-9524 Obj-1-3 /
Obj-1-4) and moved to ``spirrow-mindwire/T-ledger-writeback-after-conditional-update``.
"""
