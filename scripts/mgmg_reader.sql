-- OMAI's read-only door into mgmg's SAP data (docs/backend.md §3, option (b), chosen by the owner 24.11.2026).
--
-- Run ONCE on mgmg-db as its owner user (Render → mgmg-db → Connect → External Database URL):
--     psql "<mgmg-db External Database URL>"
--     \i scripts/mgmg_reader.sql
--     \password omai_reader                -- letters and digits only, so it fits in a URL
-- Safe to run again (it only replaces OMAI's own schema and functions).
--
-- What omai_reader can do: connect (read-only, 3 connections, 20 s per query) and call the five functions below.
-- Nothing else: no table, no view, no write. The functions return item codes, names, warehouse codes and quantities
-- only — never prices, values, suppliers, customers or comments (IT sees no company figures).
--
-- Functions, not views, on purpose: a SQL function with a quoted body is not a dependency of mgmg's tables, so this
-- never blocks a change in mgmg's database/schema.sql. If mgmg renames a column, only OMAI's sync fails (and says so).

CREATE SCHEMA IF NOT EXISTS omai_feed;
REVOKE ALL ON SCHEMA omai_feed FROM PUBLIC;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'omai_reader') THEN
        CREATE ROLE omai_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 3;
    END IF;
END $$;
ALTER ROLE omai_reader SET default_transaction_read_only = on;
ALTER ROLE omai_reader SET statement_timeout = '20s';
ALTER ROLE omai_reader SET search_path = omai_feed;
GRANT USAGE ON SCHEMA omai_feed TO omai_reader;

-- When each SAP push last arrived (the gateway PC pushes every 30 min). OMAI's freshness = this, not its own read.
CREATE OR REPLACE FUNCTION omai_feed.sap_pushed()
RETURNS TABLE (tool text, last_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT s.tool, max(s.captured_at) FROM sap_gateway_snapshots s
    WHERE s.tool IN ('stock_items', 'stock_value', 'products') GROUP BY s.tool
$$;

-- SAP's latest stock picture: item × warehouse, OnHand <> 0 only (the gateway leaves out zeros). No values.
CREATE OR REPLACE FUNCTION omai_feed.sap_stock()
RETURNS TABLE (day date, item_code text, item_name text, whs_code text, on_hand numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT h.day, h.item_code, h.item_name, h.whs_code, h.on_hand FROM stock_history h
    WHERE h.day = (SELECT max(day) FROM stock_history)
$$;

-- SAP's item list (OITM) from the latest products push. No prices.
CREATE OR REPLACE FUNCTION omai_feed.sap_items()
RETURNS TABLE (item_code text, item_name text, group_name text, barcode text, uom text, inventory_item boolean,
               active boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT s.raw->>'ItemCode', s.raw->>'ItemName', s.raw->>'ItmsGrpNam', s.raw->>'CodeBars', s.raw->>'InvntryUom',
           coalesce(s.raw->>'InvntItem', 'Y') = 'Y',
           coalesce(s.raw->>'validFor', 'Y') <> 'N' AND coalesce(s.raw->>'frozenFor', 'N') <> 'Y'
    FROM sap_gateway_snapshots s
    WHERE s.tool = 'products'
      AND s.snapshot_date = (SELECT max(snapshot_date) FROM sap_gateway_snapshots WHERE tool = 'products')
$$;

-- SAP stock movement lines (OPDN receipts, OWTR transfers, OIGN / OIGE) since a day. No supplier, totals, comments.
-- mgmg marks both a cancelled document and its cancellation as cancelled.
CREATE OR REPLACE FUNCTION omai_feed.sap_moves(since date)
RETURNS TABLE (kind text, doc_entry bigint, line_num integer, doc_num bigint, doc_date date, item_code text,
               quantity numeric, from_whs text, to_whs text, cancelled boolean, updated_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT m.kind, m.doc_entry, m.line_num, m.doc_num, m.doc_date, m.item_code, m.quantity, m.from_whs, m.to_whs,
           m.cancelled, m.updated_at
    FROM stock_moves m WHERE m.doc_date >= since
$$;

-- Open customer sales-order LINES (ORDR + RDR1, the latest so_open_lines push; 27.11.2026): what is still to deliver,
-- from which warehouse. No customer, prices or totals — OMAI shows the SAP order number.
CREATE OR REPLACE FUNCTION omai_feed.sap_orders()
RETURNS TABLE (doc_entry bigint, doc_num bigint, line_num integer, doc_date date, due_date date, ship_date date,
               item_code text, item_name text, whs_code text, quantity numeric, open_qty numeric,
               captured_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT nullif(s.raw->>'DocEntry', '')::numeric::bigint, nullif(s.raw->>'DocNum', '')::numeric::bigint,
           nullif(s.raw->>'LineNum', '')::numeric::integer,
           nullif(left(s.raw->>'DocDate', 10), '')::date, nullif(left(s.raw->>'DocDueDate', 10), '')::date,
           nullif(left(s.raw->>'ShipDate', 10), '')::date,
           s.raw->>'ItemCode', s.raw->>'Dscription', s.raw->>'WhsCode',
           nullif(s.raw->>'Quantity', '')::numeric, nullif(s.raw->>'OpenQty', '')::numeric, s.captured_at
    FROM v_sap_gateway_latest s WHERE s.tool = 'so_open_lines'
$$;

REVOKE ALL ON FUNCTION omai_feed.sap_pushed(), omai_feed.sap_stock(), omai_feed.sap_items(),
                       omai_feed.sap_moves(date), omai_feed.sap_orders() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION omai_feed.sap_pushed(), omai_feed.sap_stock(), omai_feed.sap_items(),
                          omai_feed.sap_moves(date), omai_feed.sap_orders() TO omai_reader;

-- Check (counts only):
SELECT (SELECT count(*) FROM omai_feed.sap_stock()) AS stock_rows,
       (SELECT count(*) FROM omai_feed.sap_items()) AS items,
       (SELECT count(*) FROM omai_feed.sap_moves(current_date - 45)) AS move_lines,
       (SELECT count(*) FROM omai_feed.sap_orders()) AS open_order_lines;
