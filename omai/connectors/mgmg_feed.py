"""SAP Business One, read from mgmg's database — option (b) of docs/backend.md §3, chosen by the owner 24.11.2026.

mgmg-api already receives the SAP gateway's push every 30 min and keeps it (``stock_history``,
``sap_gateway_snapshots``). OMAI reads it as the login role ``omai_reader``, which may only call four fixed functions
in the schema ``omai_feed`` (``scripts/mgmg_reader.sql``): no tables, no writes, read-only transactions, 20 s per
query. They return codes, names, warehouse codes and quantities — never prices or values.

Every 15 min (``routine.every_15``):

* **Items:** SAP is the item master. Every inventory item of the latest products push, and every item in SAP's stock,
  exists in OMAI with ``sku = sap_code = ItemCode``; SAP's name and barcode are kept up to date. Items are never
  deleted here.
* **Stock:** SAP's latest stock picture → ``external_balances`` (source ``sap``), compared, never booked. The picture
  is complete (zeros are left out), so whatever SAP no longer lists is 0.
* **Freshness:** the time of the gateway's last stock push, not the time OMAI read it — if the gateway PC stops, OMAI
  sees SAP go stale even though mgmg's database still answers.

Movement lines (``omai_feed.sap_moves``) are not booked yet: mgmg's ``stock_moves`` is empty until the gateway PC runs
``history.js``, and booking SAP movements needs an opening balance for each ``movement_source = sap`` location first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from omai.config import settings
from omai.connectors.sap_b1 import ingest_stock
from omai.models import Item
from omai.sources import mark_success

STOCK_TOOLS = ("stock_items", "stock_value")


class FeedError(RuntimeError):
    pass


def read() -> dict[str, Any]:
    import psycopg

    url = settings.mgmg_sap_reader_url.get_secret_value()
    if not url:
        raise FeedError("MGMG_SAP_READER_URL is not set")
    try:
        with psycopg.connect(url, connect_timeout=10, options="-c default_transaction_read_only=on") as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT tool, last_at FROM omai_feed.sap_pushed()")
                pushed = dict(cur.fetchall())
                cur.execute("SELECT item_code, item_name, whs_code, on_hand FROM omai_feed.sap_stock()")
                stock = cur.fetchall()
                cur.execute("SELECT item_code, item_name, group_name, barcode, uom, inventory_item, active "
                            "FROM omai_feed.sap_items()")
                items = cur.fetchall()
                cur.execute("SELECT count(*) FROM omai_feed.sap_moves(current_date - 45)")
                moves = cur.fetchone()[0]     # a count only: SAP movements arrive once the gateway runs history.js
    except psycopg.Error as exc:   # the class only: a libpq message can carry the host or the user
        raise FeedError(f"mgmg database: {type(exc).__name__}") from None
    return {"pushed": pushed, "stock": stock, "items": items, "moves_45d": moves}


def _text(value: object, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


def upsert_items(s: Session, items: list[tuple], stock: list[tuple]) -> dict:
    out = {"created": 0, "updated": 0}
    wanted: dict[str, dict] = {}
    for code, name, _group, barcode, _uom, inventory_item, _active in items:
        code = _text(code, 60)
        if code and inventory_item is not False:
            wanted[code] = {"name": _text(name, 300), "barcode": _text(barcode, 60)}
    for code, name, _whs, _qty in stock:
        code = _text(code, 60)
        if code and code not in wanted:
            wanted[code] = {"name": _text(name, 300), "barcode": None}
    by_sap = {i.sap_code: i for i in s.scalars(select(Item).where(Item.sap_code.is_not(None)))}
    for code, w in wanted.items():
        item = by_sap.get(code) or s.get(Item, code)
        if item is None:
            s.add(Item(sku=code, name=w["name"] or code, sap_code=code, barcode=w["barcode"]))
            out["created"] += 1
            continue
        new = {"sap_code": item.sap_code or code, "name": w["name"] or item.name, "barcode": w["barcode"] or item.barcode}
        if any(getattr(item, k) != v for k, v in new.items()):
            for k, v in new.items():
                setattr(item, k, v)
            out["updated"] += 1
    s.flush()
    return out


def sync(s: Session, data: dict[str, Any] | None = None) -> dict:
    data = data if data is not None else read()
    pushes = [t for k, t in data["pushed"].items() if k in STOCK_TOOLS and t]
    if not pushes:
        raise FeedError("no SAP stock push in mgmg's database yet")
    out = {"items": upsert_items(s, data["items"], data["stock"])}
    rows = [{"ItemCode": c, "WhsCode": w, "OnHand": float(q or 0)} for c, _n, w, q in data["stock"] if c and w]
    # an empty picture is a failed push, not "SAP has nothing": never zero everything on it
    out["stock"] = ingest_stock(s, rows, complete=bool(rows))
    last = max(pushes)
    last = last if last.tzinfo else last.replace(tzinfo=UTC)
    mark_success(s, "sap", at=last.astimezone(UTC))
    out["pushed_minutes_ago"] = round((datetime.now(UTC) - last).total_seconds() / 60)
    return out


def main() -> None:
    """``python -m omai.connectors.mgmg_feed`` in the Render Shell: counts only. ``--sync`` also runs one sync."""
    import sys

    data = read()
    ages = {k: round((datetime.now(UTC) - v).total_seconds() / 60) for k, v in data["pushed"].items() if v}
    print(f"stock rows={len(data['stock'])} items={len(data['items'])} SAP movement lines (45 days)="
          f"{data.get('moves_45d', '?')} last push (min ago)={ages}")
    if "--sync" in sys.argv:
        from omai.db import init_db, session_scope

        init_db()
        with session_scope() as s:
            print(sync(s, data))


if __name__ == "__main__":
    main()
