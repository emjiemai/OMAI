# OMAI — rules that must not be undone

- Stock changes only through `ledger.record` (movements with a unique `idem_key`). Never add an editable quantity column.
- One authoritative system per location (`Location.movement_source`); the others are compared via `external_balances`.
  Conflicts open an exception — never average, never "fix" stock to make systems agree.
- Counters never see book quantities (`counting.blind_form`). Recounts are by a different person; results are kept.
- Adjustments/write-offs only via `approvals` (not the requester, not a counter, within limit, payload-hash bound).
- OMAI never approves, writes off, disposes, purchases or disciplines. AIPRO owns purchasing; CASH owns release.
- Costs are visible only to manager / accountant / owner (`COST_ROLES`).
- Outgoing messages go through the outbox (`notify.queue`), in the same transaction as the change.
- Scheduled slots go through `routine.run_slot` (restart-safe).
- Run `python -m pytest -q` before every push; the 8 spec acceptance tests live in `tests/test_acceptance.py`.
