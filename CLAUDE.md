# OMAI — read this first

OMAI (Virtual Warehouse Manager) is a new service next to the company's running system **mgmg-agents**
(`/home/ulugbekai/Projects/mgm-command-centre`, its own `CLAUDE.md`). Same owner, same Render workspace, same
`mgmg-shared` env group, same SAP / 1C / Billz. **The backend facts (how data really arrives, entity names,
warehouse codes, limits, what OMAI's code gets wrong today) are in `docs/backend.md`, loaded below.** Read it before
touching a connector, and check the mgmg-agents code before guessing anything about SAP, 1C, Billz or Telegram.

@docs/backend.md

## How to work with me
- **Don't guess — verify.** Read the code, the docs, mgmg-agents, or the live API (read-only) before claiming anything.
  Results first, short.
- Every bot and user-facing text is **Uzbek Cyrillic**, polite «сиз». Code, comments, docs and commits are in English.
- **Never touch the company's bots.** Admin Bot and OPS Manager Bot belong to mgmg-api (webhooks). OMAI uses its own
  bot token.
- Production checks are **read-only** (`default_transaction_read_only=on`). Print counts and error types only, never
  figures, names or secrets.
- **SAP:** never connect to HANA directly or ask for its credentials. Data leaves SAP only through the gateway's
  fixed tools.
- **1C** is GET-only: at most 3 reads at once, catalogs cached.
- Open design decisions are the owner's. Examples: the 1C ↔ SAP item mapping, which 1C склад is which SAP warehouse,
  the pilot. Write the options, don't pick one in code. (Decided: SAP reaches OMAI by reading mgmg's database, 24.11.)

## Rules that must not be undone
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
