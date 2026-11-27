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
| every 15 min | sync Billz / 1C (SAP arrives by the gateway push); system mismatches, old holds, overdue transfers and receipts; recovery task to IT when a critical source is stale | `omai/routine.py`, `omai/connectors/` |
| 09:00 | opening, access and hazard checks → the location lead confirms or reports | `routine.opening` |
| 09:15 | assign receiving, put-away, dispatch and due counts | `routine.assign` |
| 11:00, 15:00 | progress review to each person | `routine.progress` |
| 17:30 / 17:50 | reconcile movements; closing check (locks, keys, handovers) and one short digest | `routine.reconcile_day`, `routine.closing` |
| Mon 08:30 | ageing facts and risks (slow, dead, excess, expired, damaged) → AIPRO | `insights.send_ageing_to_aipro` |

Each scheduled slot is keyed (`jobs` table): a restart never runs a slot twice; a failed slot is retried.

## How it fits together

```
 SAP gateway push → POST /sap/data/{kind} ┐                     ┌── Staff workspace (/)  — mobile, scanner-friendly
                                     │                          │   blind count form, Start / Need help / Problem / Submit
 1C OData (GET only) ────────────────┼─▶ connectors/ingest ─▶ ledger (movements, Postgres) ─▶ tasks, counts, approvals
 Billz 2.0 (sales, returns, stock) ──┘   (authority + dedupe)   │
 Approver release → POST /orders/{ref}/release ───────────────────┤── Owner dashboard (/owner), Excel reports
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
| manager | approve within `ADJUSTMENT_LIMIT_MANAGER` (constant in `omai/config.py`), accept work, approve returns to sale, manual release fallback |
| accountant | approve within `ADJUSTMENT_LIMIT_ACCOUNTANT`; release orders |
| owner | everything; receives owner-decision exceptions |
| it | integrations, recovery tasks |

Nobody approves their own adjustment; costs and values are shown only to manager / accountant / owner.

## Connections

| System | How | Code |
| --- | --- | --- |
| SAP Business One | **Now:** read from mgmg's database every 15 min as `omai_reader` (`scripts/mgmg_reader.sql`, `MGMG_SAP_READER_URL`): SAP's item list and latest stock picture; freshness = the gateway's last push. | `omai/connectors/mgmg_feed.py` |
| SAP (other way, off) | The office SAP gateway's push script (mgmg-agents `scripts/sap-gateway-push/push-ar-aging.ps1`, `$OmaiHost`) sends the rows of `stock_value`, `goods_receipts`, `stock_transfers`, `inventory_moves` to `POST /sap/data/{kind}` (header `X-Omai-Secret: $SAP_PUSH_WEBHOOK_SECRET`) — SAP's HANA column names as they are. A complete `stock_value` push zeroes what SAP no longer lists; cancellation documents undo their original. No serials / batches, deliveries or returns from the gateway yet. OMAI never posts to SAP. | `omai/connectors/sap_b1.py` |
| 1C | OData, GET only; compared, never booked. This 1C has no trade stock register: stock = `AccountingRegister_Хозрасчетный/Balance` `КоличествоBalance` on accounts 29xx / 10xx / 28xx (`ONEC_STOCK_ACCOUNTS`), item and склад read from the subconto by type. Item → OMAI by `Item.onec_ref`, else 1C «Артикул» = SAP `ItemCode`; склад by `Location.onec_ref`. Chart and catalog kept 30 min. | `omai/connectors/onec.py` |
| Billz 2.0 | `/v1/auth/login`, `/v1/product-general-table` by position, one day per call (sales, returns → quarantine). Stock: `/v2/products` → `shop_measurement_values` (checked live 23.11.2026: 1451 products, `active_measurement_value` per shop). | `omai/connectors/billz.py` |
| Release approval | the accountant / owner (CASH's role) approves in OMAI | `POST /orders/{ref}/release` |
| AIPRO, MIYA | rows in the shared `outbox` table (channel `aipro` / `miya`) for those agents to read — minimum data | `omai/notify.py` |
| Telegram | task messages with buttons — **OMAI's own bot** (`OMAI_TELEGRAM_BOT_TOKEN`, on the OMAI service only). The Admin / OPS Manager bots belong to mgmg-api: their tokens are refused. | `omai/notify.py`, `/telegram/webhook/{secret}` |

## Run it

```bash
uv venv .venv && uv pip install -r requirements.txt --python .venv/bin/python
cp .env.example .env
.venv/bin/python -m scripts.setup mapping.xlsx          # locations, bins, items, users
.venv/bin/python -m scripts.setup --pin otabek 4821
.venv/bin/uvicorn omai.api.app:app --reload
.venv/bin/python -m pytest -q
```

Deploy: Render Blueprint (`render.yaml`) — Postgres + one always-on API that runs the schedule, with only the variables OMAI reads set on the service (no `mgmg-shared` group): `ENVIRONMENT`, `TZ`, `DRY_RUN`, `OMAI_TELEGRAM_BOT_TOKEN`, `SAP_PUSH_WEBHOOK_SECRET`, `MGMG_SAP_READER_URL`, `ONEC_ODATA_URL`, `ONEC_LOGIN`, `ONEC_PASSWORD`, `BILLZ_SECRET_TOKEN`; `DATABASE_URL` comes from OMAI's own database. Or
`docker compose up`.

## Rehearse count day on your computer (real data, test bot)

```bash
DATABASE_URL=sqlite:///rehearsal.db .venv/bin/python -m scripts.rehearsal
```

Builds a local `rehearsal.db` from the live systems (read-only): warehouses, items, SAP and 1C stock, the four 1C
links; three test people `boss` / `ombor` / `tekshir` (one PIN, asked, never shown); 01 switched to OMAI, shelves
G01–G10 + FLOOR, and the baseline count sent to `ombor`. The local `.env` needs a **test** bot's token, the 1C
variables and `MGMG_SAP_READER_URL` (omai_reader with mgmg-db's External host). Then run the web app and the bot
loop with the same `DATABASE_URL=sqlite:///rehearsal.db` in front. Production is not touched.

## Test it locally (before production)

Nothing here touches production: a local SQLite file, demo data, and a separate **test** Telegram bot.

1. Create a test bot in @BotFather. Put its token in a local `.env` as `OMAI_TELEGRAM_BOT_TOKEN`, and add
   `ENVIRONMENT=development`. Do **not** use a production bot's token: the polling loop stops if Telegram says the
   bot already belongs to a server.
2. Load the demo data. The demo logins and PINs are listed at the top of `scripts/demo.py`.

   ```bash
   .venv/bin/python -m scripts.demo
   ```
3. Start the web app: the staff workspace is http://localhost:8000 and the owner dashboard is
   http://localhost:8000/owner.

   ```bash
   .venv/bin/uvicorn omai.api.app:app --port 8000
   ```
4. In another terminal, start the Telegram test loop. It handles messages and buttons, sends reminders, and needs
   no public URL.

   ```bash
   .venv/bin/python -m omai.telegram_bot
   ```
5. In Telegram, open the test bot and send `/login otabek <PIN>`, then `/tasks`. Press Бошлаш, Ёрдам керак or
   Report problem. Submit sends a link to the workspace, where the blind count form is.
6. To run the scheduled steps by hand (09:00 opening, 09:15 assign, 17:50 digest):

   ```bash
   .venv/bin/python -c "from omai.routine import run_now; print(run_now('closing'))"
   ```

To start again from scratch, delete `omai.db` and repeat step 2. `WORKSPACE_URL` in `omai/config.py` is
`http://localhost:8000`, so the Submit link opens only on the computer running the app.

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

## People and access (`/admin`)

The owner and IT open `<OMAI address>/admin` and sign in with their login and PIN. There they add people (login,
name, role, warehouse, PIN), change a role or warehouse, reset a PIN, disconnect someone's Telegram, or **block**
them: a blocked person can use neither the bot nor the pages, and the bot forgets their chat. Only the owner gives
the roles that approve or see costs (manager, accountant, owner); nobody blocks or demotes themselves; one active
owner always stays. Every change is in `audit_log` (what changed, never the PIN).

## The bot (@mgmomaibot)

`/login <login> <PIN>` once; then buttons under the message box: **📋 Топшириқларим** (open tasks with Бошлаш /
Ёрдам керак / Муаммо / Топшириш) and, for manager / accountant / owner / IT, **📊 Ҳолат** (`/status`): SAP, 1C and
Billz freshness, open differences per system, other open problems, overdue tasks — counts only. Free text gets a
short pointer to the buttons (no AI). A Telegram resend of the same update is answered once.

## Start the pilot (01)

On the morning of the first count, in the omai-api Shell:

```bash
python -m scripts.load_master --pilot 01            # look only
python -m scripts.load_master --pilot 01 --apply    # OMAI's book starts from SAP's stock at 01; staff scans rule 01
```

From then on every movement in or out of 01 is scanned in OMAI; the baseline blind count checks the opening book:

```bash
python -m scripts.count_day count 01 otabek dilshod 2026-11-28T18:00   # counter, checker (not the counter), deadline
python -m scripts.count_day show 01                                   # where the counts stand (counts only)
python -m scripts.count_day bins 01 G01-G20 FLOOR                     # shelves, for putting the stock away after
python -m scripts.count_day shelve 01 otabek 2026-11-29T18:00         # after the count closes: shelving task
```

Shelving (workspace → the task → Топшириш): scan the item and the shelf, type the quantity put there. Each scan is
one movement from "no shelf" to the shelf (a resent scan counts once); only codes and names are shown, never a
quantity, and shelving is refused while a count is open. The task closes itself when nothing is left without a shelf.

The baseline is counted **by item across the whole warehouse** ("бутун омбор"): after the switch nothing is on a shelf
yet. The form shows each item's code and name, never a quantity.

## Load warehouses and items

In the Render Shell of omai-api (where the 1C / Billz variables are):

```bash
python -m scripts.load_master                       # look only: counts, 1C склады, Billz shops
python -m scripts.load_master --apply               # the 18 SAP warehouses + the 1C items with an Артикул
python -m scripts.load_master --link 01 onec=cce7ceea     # warehouse 01 = this 1C склад
python -m scripts.load_master --link G.A._01 billz=<shop id>
```

## Still to do before go-live

* Fill the location & SKU mapping (SAP WhsCode, Billz shop id, 1C Ref_Key) and the ABC groups.
* SAP: run `scripts/mgmg_reader.sql` on mgmg-db and set `MGMG_SAP_READER_URL`; for serials / batches, deliveries and returns a new gateway tool is needed (+ HANA SELECT grants).
* 1C: set each location's `onec_ref` (the 1C склад `Ref_Key`); finance confirms `ONEC_STOCK_ACCOUNTS` and fixes the «Артикул» of items that don't carry the SAP code.
* Confirm the Billz stock endpoint for our plan.
* Baseline count of the pilot warehouse, then one store transfer route; expand after acceptance.
* Camera/sensor counting and ML ranking are not built: start with these rule-based controls, as the spec says.
