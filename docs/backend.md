# The backend OMAI plugs into — facts checked against the live systems (2026-11-23)

OMAI is a new service next to the company's existing system, **mgmg-agents**
(`github.com/emjiemai/mgmg-agents`, local clone `/home/ulugbekai/Projects/mgm-command-centre`). Everything below was
verified in that repo's code, its production database (read-only), or the live 1C service. Where OMAI's code assumes
something else, that is listed under **Known gaps**. If a fact here is not enough, read the mgmg-agents code before
guessing. Its `CLAUDE.md` and `docs/agent-specs/*.md` are the reference.

## 1. The company and the people

- EMJIEM / MGMG has three businesses:
  - **GARMIN**: Garmin watches, plus Tanita. Shops: Abay, Minor, Bodomzor, Ambasador.
  - **PRIMUS**: B2B equipment (Alliance Laundry and others), parts and rentals.
  - **LONDRY**: self-service laundries Юнусобод and Вузгородок (Olmazor in SAP), tokens and cash.
- "Director" = "the Boss" = the owner. Ulugbek (IT) builds and runs both systems alone.
- The SAP gateway's old admin Abdulbosit left in 11.2026. The gateway PC is now updated by Remote Desktop.
- **Every bot and user-facing text is Uzbek Cyrillic** (names too, Latin → Cyrillic). Code, comments, docs and commits
  are English. **Always the polite «сиз».**
- **IT sees no company figures** (the Director's order of 07.10.2026): nothing sent to IT/admin carries amounts,
  customers, or other people's data. Logs keep counts and error types, never data. Costs only to `COST_ROLES`.

## 2. Render

- **mgmg-api**: Starter, always on. It runs the Admin Bot and OPS Manager Bot webhooks, the SAP push receiver, and
  the daily schedule (its own loop, no cron services).
- **mgmg-db**: Postgres basic-256mb.
- **Plans are settled**: never suggest raising them.
- **Env group `mgmg-shared`** holds:
  - flags: `TZ`, `ENVIRONMENT=production`, `DRY_RUN`, `AGENT_WRITES_ENABLED`, `BOTS_FROZEN`;
  - SAP: `SAP_PUSH_WEBHOOK_SECRET`;
  - search and AI: `SERPAPI_API_KEY`, `TAVILY_API_KEY`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_FALLBACK_MODELS`;
  - Google: `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_LEADS_SHEET_ID`;
  - leads: `LEAD_AGENT_ENABLED`, `GARMIN_LEADS_SECRET`;
  - reports and permissions: `DAILY_REPORTS_ENABLED`, `PERMISSION_DEPUTY_TELEGRAM_IDS`;
  - Billz: `BILLZ_SECRET_TOKEN`;
  - 1C: `ONEC_ODATA_URL`, `ONEC_LOGIN`, `ONEC_PASSWORD`;
  - Verifix: `VERIFIX_CLIENT_ID`, `VERIFIX_CLIENT_SECRET`, `VERIFIX_LOGIN`, `VERIFIX_PASSWORD`, `VERIFIX_FILIAL_ID`;
  - Admin Bot: `ADMIN_BOT_TELEGRAM_BOT_TOKEN`, `ADMIN_BOT_TELEGRAM_CHAT_ID`, `ADMIN_BOT_WEBHOOK_SECRET`, `ADMIN_BOT_ADMIN_USER_ID`;
  - OPS Manager Bot: `OPS_MANAGER_BOT_TELEGRAM_BOT_TOKEN`, `OPS_MANAGER_BOT_WEBHOOK_SECRET`, `OPS_MANAGER_BOT_MODEL`, `OPS_MANAGER_BOT_FALLBACK_MODELS`;
  - backups: `BACKUP_SECRET`, `BACKUP_PUBLIC_KEY`.
- `DATABASE_URL` is **per service** (`fromDatabase`), not in the group, so OMAI's own database URL doesn't clash.
- OMAI does **not** use `fromGroup: mgmg-shared` (it would hand OMAI every secret above). `render.yaml` sets only the
  9 variables OMAI reads on the OMAI service. Without the group, the company bot tokens are absent, so
  `telegram_token_is_company_bot` can't compare against them: never paste a company bot token into
  `OMAI_TELEGRAM_BOT_TOKEN`.
- OMAI's own bot token `OMAI_TELEGRAM_BOT_TOKEN` is set on the OMAI service, not in the group. See §6.

## 3. SAP Business One

- SAP B1 runs on **SAP HANA, schema `MGM`**. Local currency is **USD**; the system currency is **UZS** (so'm). Item
  codes and warehouse codes are SAP's own.
- **There is no Service Layer access and no direct DB access from the cloud.** The gateway owner's rule: data leaves
  SAP only through the **gateway's fixed read-only tools** (Node.js, source in mgmg-agents `sap-gateway/src/`). Each
  tool runs fixed SQL with bound parameters.
- `scripts/sap-gateway-push/push-ar-aging.ps1` on the gateway PC calls the tools every 30 min (Windows task
  "MGMG SAP push"). It POSTs **only to mgmg-api**:
  - `/webhooks/sap-data/{kind}/{secret}`
  - `/webhooks/sap-push/{secret}`
  - `/webhooks/sap-gateway-push/{tool}/{secret}`
- **Nothing is pushed to OMAI's `POST /sap/push` today.** Making it arrive means one of these, and **the owner decides
  which** (don't pick one in code):
  - (a) add OMAI as a second target in the push script and the gateway PC;
  - (b) OMAI reads mgmg's Postgres read-only (needs a login role on a view, the pattern of mgmg's `analyst.*` views and
    `mgmg_analyst_*` roles).
- **Row shapes are HANA column names, not Service Layer JSON:**
  - document lines: `DocEntry`, `DocNum`, `DocDate`, `CANCELED`, `LineNum`, `ItemCode`, `Dscription`, `Quantity`,
    `WhsCode`, `LineTotal`, `Currency`, `CardCode`, `CardName`, `CreateDate`, `Comments`;
  - transfers add `FromWhsCod` (line) and `Filler` / `ToWhsCode` (header);
  - goods receipts / issues carry `ObjType` 59 / 60.
- There is no `DocumentLines`, `StockTransferLines`, `WarehouseCode`, `SerialNumbers` or `BatchNumbers`.
- **Serial and batch numbers are not exported by any tool** (OSRI / OSRN / OBTN aren't read). A new gateway tool is
  needed, and so is a HANA SELECT grant on the gateway PC.
- Tools that exist (`sap-gateway/src/`):
  - inventory / stock: `stock-value.js` = OITW per item × warehouse with `OnHand`, `AvgPrice`, `StockValue`;
  - warehouses, products;
  - sales lines (A/R invoices + credit notes);
  - open invoices, payments, customers, supplier balances, orders;
  - `history.js`:
    - goods receipts OPDN, transfers OWTR, goods receipts / issues OIGN / OIGE (45 days each);
    - open sales-order lines.
- **Not exported:** deliveries ODLN and returns ORDN.
- In mgmg's DB (read-only check, 23.11):
  - **`stock_history`** holds the daily stock picture per item × warehouse (`on_hand`, `stock_value_usd`). It starts
    16.11.2026 and covers 813 items.
  - **`stock_moves` is EMPTY.** The OPDN/OWTR/OIGN/OIGE tools aren't arriving yet: either the gateway PC wasn't
    updated with `history.js`, or the HANA user lacks SELECT on those 9 tables (`docs/sap-gateway-tools.md` §F4).
- **Warehouse codes (SAP, live):**
  - `01` Asosiy omborxona (main)
  - `02` Bojxona (customs)
  - `03` Garmin-Bodomzor
  - `04` Garmin-Abay
  - `05` Garmin-Minor
  - `06` Faulty Product Garmin
  - `07` Parts
  - `08` Show room
  - `09` Garmin zahiralar (Tanita)
  - `10` Garmin Ambasador
  - `11` Londry Yunusobod
  - `12` Londry Olmazor
  - `13` Spotx
  - `15` a person's name (a salesperson's own stock)
  - `16` Primus Arendadagi tovarlar (rented-out)
  - `20` Инвентарлар
  - `G.A._01` Garmin Abay
  - `G.A._02` Garmin Tanita
- **Store sales in SAP:** the Abay shop enters **one A/R invoice per Billz cheque** (warehouses `G.A._01`, `05`, SAP
  user «Гармин (филиал Абай)»), often **a day or more late**. So an SAP sale can trail the Billz sale by days. mgmg's
  Billz → SAP check (spec 17) matches them cheque by cheque.

## 4. 1C — «Бухгалтерия для Узбекистана» on Clobus (1C:Fresh)

- OData: `https://clobus.uz/a/acc313/61458/odata/standard.odata/`, HTTP Basic, service user `mgmg_bot_odata`.
  **GET only.**
- The whole database is published, payroll and personal data included. Read only the entities you need.
- **There is no `AccumulationRegister_ТоварыНаСкладах`** (verified 23.11: 1438 entity sets; the trade registers don't
  exist in this configuration). Stock lives in the accounting register:
  - Entity: `AccountingRegister_Хозрасчетный/Balance(Period=datetime'YYYY-MM-DDTHH:MM:SS')`.
  - Fields:
    - `Account_Key`, `ExtDimension1/2/3` with `ExtDimension1/2/3_Type`;
    - **`КоличествоBalance`** (quantity), `СуммаBalance` (so'm), `ВалютнаяСуммаBalance`;
    - `Валюта_Key`, `Организация_Key`, `Подразделение_Key`.
  - Accounts (`ChartOfAccounts_Хозрасчетный`, by `Code`, NSBU 21): **29xx** товары (2900–2950), **10xx** материалы,
    **28xx** готовая продукция.
  - Item and warehouse are subconto: read `ExtDimensionN_Type` to see which N is `Catalog_Номенклатура` and which is
    `Catalog_Склады`. Both catalogs are published.
  - 1C keys are GUIDs (`Ref_Key`). **Mapping a 1C item to an SAP `ItemCode` is not established.** Check
    `Catalog_Номенклатура`'s `Code` / `Артикул` with finance before relying on it.
- **Clobus limits learned in production:**
  - more than ~3 reads at once → **429** (keep a semaphore at 3);
  - a catalog read sometimes hangs **~60 s** (keep catalogs in memory ~30 min, cap name reads at 20 s);
  - **«410 Gone»** = OData was switched off in Clobus (20.10). Only the owner can re-enable it.
- Money classes for reference: 50xx cash, 51xx–57xx bank. That is finance data: never to warehouse staff or IT.

## 5. Billz 2.0 (shop tills)

- Base `https://api-admin.billz.ai`. Login: `POST /v1/auth/login {"secret_token"}` → `data.access_token`.
- Endpoints mgmg uses in production:
  - `/v1/shop`
  - `/v1/general-report-table`
  - `/v1/seller-general-table`
  - `/v1/product-general-table` with `detalization_by_position=true`
- **Only one shop sells: GARMIN ABAY.**
- `/v1/product-general-table` is **slow**: on 04.11 a 14-day read timed out 3 × 60 s. Read **one day per call**, and
  keep past days (they don't change). mgmg keeps them 3 h.
- `/v2/products` (OMAI's `BILLZ_STOCK_PATH`) is **not used or verified** by mgmg. Confirm with Billz before relying on
  it.

## 6. Telegram — do not touch the company's bots

- mgmg-api owns two bots by **webhook**: Admin Bot (IT) and OPS Manager Bot (every employee and the Director).
- Using either token in OMAI breaks the company system:
  - `getUpdates` polling gets 409 while their webhooks are set;
  - `setWebhook` would silently steal every employee's messages;
  - `sendMessage` from OMAI would mix into their chats.
- **OMAI needs its own bot** (@BotFather) and its own variable, set on the OMAI service, not in `mgmg-shared`.
  OMAI refuses an Admin / OPS Manager token (`settings.telegram_token_is_company_bot`).
- People's Telegram ids are in mgmg's `employees` table (with role, department, and ака/опа for the polite address).
  Reuse it only with the owner's decision.

## 7. Other systems (not wired into OMAI)

- **Verifix** attendance: licence expired 07.11, the API answers 401 «Нет лицензии».
- **AI**: OpenRouter only (`OPENROUTER_API_KEY`).
- **AmoCRM, Didox**: later.
- **AIPRO** (purchasing), **CASH** (release), **MIYA**: owner-spec agents. AIPRO / OMAI / AI phone operator plan:
  mgmg-agents `docs/plans/2026-11-09-new-specs-plan.md`.

## OMAI against these facts (fixed 2026-11-23)

1. SAP: `POST /sap/data/{kind}` reads the gateway's own rows (HANA columns) for `stock_value`, `goods_receipts`,
   `stock_transfers`, `inventory_moves`; the push script in mgmg-agents has `$OmaiHost` (off until filled in on the
   gateway PC). Still missing from the gateway: serials / batches, deliveries ODLN, returns ORDN.
2. 1C: `AccountingRegister_Хозрасчетный/Balance` on 29xx / 10xx / 28xx, item / склад by subconto type, item by
   `onec_ref` or «Артикул» = SAP code (583 of 813 matched on 23.11), catalogs kept 30 min, «410» explained.
   To do by hand: each location's `onec_ref`.
3. Telegram: `OMAI_TELEGRAM_BOT_TOKEN` (OMAI's own bot, on the service only); a company bot's token is refused.
4. Billz `/v2/products` checked live (23.11): `products[].shop_measurement_values[].active_measurement_value`.
5. User-facing texts are Uzbek Cyrillic (task titles, reminders, buttons, refusals, exceptions, both pages); the
   codes (states, kinds) stay English with labels `STATE_UZ`, `ACTION_UZ`, `KIND_UZ`.
