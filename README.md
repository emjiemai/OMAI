# OMAI — Virtual Warehouse Manager

OMAI coordinates EMJIEM warehouse and store stock (Garmin, Tanita, Primus equipment, parts, detergents, display
units, LONDRY supplies): it tracks every movement, compares SAP Business One, 1C and Billz with physical evidence,
schedules counts, assigns work and follows exceptions to closure. Built to the *OMAI Virtual Warehouse Manager — IT
team task and build specification*, in the same style as `emjiemai/mgmg-agents`.

**OMAI plans and checks; people do the physical work.** Otabek and site staff receive, scan, move, inspect and
count. OMAI reports what is *verified*, what is *pending* and what is *unknown* (stale sources are shown as such).
It never writes off stock, approves credit, disposes of goods, disciplines staff or makes purchases.

## What runs, and when (Asia/Tashkent)

| When | What | Code |
| --- | --- | --- |
| every movement | validate the scan; refuse wrong serials, unavailable stock, frozen bins | `omai/warehouse.py`, `omai/ledger.py` |
| every 5 min | reminders (one before, one after the deadline, inside shifts) and manager escalation; send the outbox | `omai/tasks.py`, `omai/notify.py` |
| every 15 min | sync SAP / Billz / 1C; system mismatches, old holds, overdue transfers and receipts; recovery task to IT when a critical source is stale | `omai/routine.py`, `omai/connectors/` |
| 09:00 | opening, access and hazard checks → the location lead confirms or reports | `routine.opening` |
| 09:15 | assign receiving, put-away, dispatch and due counts | `routine.assign` |
| 11:00, 15:00 | progress review to each person | `routine.progress` |
| 17:30 / 17:50 | reconcile movements; closing check (locks, keys, handovers) and one short digest | `routine.reconcile_day`, `routine.closing` |
| Mon 08:30 | ageing facts and risks (slow, dead, excess, expired, damaged) → AIPRO | `insights.send_ageing_to_aipro` |

Each scheduled slot is keyed (`jobs` table): a restart never runs a slot twice; a failed slot is retried.

## How it fits together

```
 SAP B1 Service Layer ──(read user)──┐                          ┌── Staff workspace (/)  — mobile, scanner-friendly
 SAP gateway push → POST /sap/push ──┤                          │   blind count form, Start / Need help / Problem / Submit
 1C OData (GET only) ────────────────┼─▶ connectors/ingest ─▶ ledger (movements, Postgres) ─▶ tasks, counts, approvals
 Billz 2.0 (sales, returns, stock) ──┘   (authority + dedupe)   │
 CASH release → POST /webhooks/cash/release ────────────────────┤── Owner dashboard (/owner), Excel reports
 Telegram buttons → POST /telegram/webhook/{secret} ────────────┘── Outbox → Telegram, AIPRO, MIYA
```

### The stock rules (`omai/ledger.py`)

* Stock is never an editable number: a balance is the sum of **movement lines**. A position is
  location × bin × SKU × serial/batch × **condition** (saleable / damaged / quarantine / expired) × **ownership**
  (own / customer / consignment).
* Every movement has a unique **idempotency key** — the same SAP, 1C or Billz event processed twice, or a failed
  sync retried, changes stock once. A `shared_ref` links one event mirrored in two systems.
* **Authority per location** (`Location.movement_source`): only one system books movements for a location
  (`omai` = staff scan there, `sap`, or `billz`); the others are *compared* (`external_balances`), so a store sale in
  Billz and the same sale in SAP never count twice. Conflicting balances raise an exception — never an average.
* **Transfers**: source → `TRANSIT` (bin = transfer id) → destination only on the destination's scan. Units are
  never available in two places. Partial receipts leave the rest in transit, flagged.
* **Available** = saleable own on-hand − active allocations. Allocations are a separate table (a subset of stock).
* Planned arrivals (POs, `expected_receipts`) are never stock; physical arrival is separate from ERP posting.

### Counting (`omai/counting.py`)

Count ID, SKU/bin list (zero-stock bins included as “should be empty” lines; unlisted goods can be added),
counter ≠ checker, deadline → cutoff timestamp (optional freeze) → **blind form** (no expected quantities) →
compare with the ledger **at the cutoff** (event time, so sales during the count and backdated postings land
correctly) → material difference or serial mismatch → **independent recount** (not the first counter; both results
kept, never averaged) → confirmed variance → investigation hints (late postings, other locations, other systems,
units) → cause + corrective task → **approval** (not the requester, not a counter, within the role's written limit,
bound to the exact payload hash) → posting with the separate SAP posting user → close → next date from the
accepted count (A weekly, B monthly, C quarterly; original deadlines kept).

### Who may do what

| Role | Can |
| --- | --- |
| staff, custodian | scan, receive, move, count; see own tasks and own location's stock (no costs) |
| checker | independent recounts, evaluate counts, record causes, request corrections |
| manager | approve within `ADJUSTMENT_LIMIT_MANAGER`, accept work, approve returns to sale, manual release fallback |
| accountant | approve within `ADJUSTMENT_LIMIT_ACCOUNTANT` |
| owner | everything; receives owner-decision exceptions |
| it | integrations, recovery tasks |

Nobody approves their own adjustment; costs and values are shown only to manager / accountant / owner.

## Connections

| System | How | Code |
| --- | --- | --- |
| SAP Business One | Service Layer (`/Login`, OData paging) with a **read** user; a separate **posting** user only for approved adjustments (`InventoryGenEntries/Exits`). Or the office gateway pushes to `/sap/push` (as in mgmg-agents). | `omai/connectors/sap_b1.py` |
| 1C | OData, GET only; balances compared, never booked. Entity/field names are settings — finance confirms the authoritative fields. | `omai/connectors/onec.py` |
| Billz 2.0 | `/v1/auth/login`, `/v1/product-general-table` by position (sales, returns → quarantine). Stock path is a setting — **verify with Billz for your plan**. | `omai/connectors/billz.py` |
| CASH | release authorisation webhook | `POST /webhooks/cash/release` |
| AIPRO, MIYA | outbox webhooks (stock facts, ageing; tasks) — minimum data, no salaries or supplier costs to staff | `omai/notify.py` |
| Telegram | task messages with buttons | `omai/notify.py`, `/telegram/webhook/{secret}` |

## Run it

```bash
uv venv .venv && uv pip install -r requirements.txt --python .venv/bin/python
cp .env.example .env
.venv/bin/python -m scripts.setup mapping.xlsx          # locations, bins, items, users
.venv/bin/python -m scripts.setup --pin otabek 4821
.venv/bin/uvicorn omai.api.app:app --reload
.venv/bin/python -m pytest -q
```

Deploy: Render Blueprint (`render.yaml`) — Postgres + one always-on API that runs the schedule. Or
`docker compose up`.

## Acceptance tests (`tests/test_acceptance.py`)

1. Receive 10 against 12 → 10 received, 2 outstanding; damaged quarantined.
2. Transfer 5 → source −5 once, transit 5, destination +5 only on receipt; company total constant.
3. The same SAP / 1C / Billz event twice → one change; retries don't duplicate.
4. Book 10, blind count 8 → independent recount, investigation; no silent adjustment.
5. Sale during a count and a backdated posting reconcile to the cutoff.
6. Wrong serials, missing release, unavailable stock blocked; returns, cancelled allocations, zero vs blank.
7. Missed task → reminder and escalation; accepted evidence stops reminders; shift boundaries.
8. Outage → stale data, blocked release, recovery task, manual fallback; costs hidden; no self-approval;
   scheduled slots survive restarts.

## Still to do before go-live

* Fill the location & SKU mapping (SAP WhsCode, Billz shop id, 1C Ref_Key) and the ABC groups.
* Confirm with SAP the installed Service Layer version, and create the two SAP users.
* Confirm with finance which 1C register is authoritative; set `ONEC_*` names.
* Confirm the Billz stock endpoint for our plan.
* Baseline count of the pilot warehouse, then one store transfer route; expand after acceptance.
* Camera/sensor counting and ML ranking are not built: start with these rule-based controls, as the spec says.
