"""Turning external documents into ledger movements, safely.

* Idempotent: the key is ``<system>:<doc type>:<doc entry>:<line>``. A retried or repeated sync changes nothing.
* Cross-system: a ``shared_ref`` (order number, Billz cheque, transfer id) links the same event in two systems.
* Authority: a document is booked only if the system is the location's ``movement_source``; otherwise it is
  matched/compared only. Planned arrivals (POs) are never stock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from omai.ledger import TRANSIT, Line, StockError, audit, raise_exception, record
from omai.models import (DispatchOrder, ExpectedReceipt, ExternalBalance, Item, Location, now_utc)


@dataclass
class DocLine:
    sku: str          # external item code; mapped through Item.sap_code / billz_id / sku
    qty: float
    loc: str | None = None    # external warehouse / shop code
    to_loc: str | None = None
    lot: str = ""
    line_no: int = 0


@dataclass
class Doc:
    system: str       # sap / 1c / billz
    doc_type: str     # receipt / issue / receipt_adj / issue_adj / transfer / delivery / return / sale / sale_return
    doc_id: str
    event_time: datetime
    lines: list[DocLine] = field(default_factory=list)
    shared_ref: str | None = None
    doc_ref: str | None = None


INBOUND = {"receipt", "receipt_adj", "return", "sale_return"}
OUTBOUND = {"issue", "issue_adj", "delivery", "sale"}


def map_item(s: Session, system: str, code: str) -> Item | None:
    col = {"sap": Item.sap_code, "1c": Item.onec_ref, "billz": Item.billz_id}.get(system)
    item = s.scalar(select(Item).where(col == code)) if col is not None else None
    return item or s.get(Item, code) or s.scalar(select(Item).where(Item.barcode == code))


def map_location(s: Session, system: str, code: str | None) -> Location | None:
    if not code:
        return None
    col = {"sap": Location.sap_whs, "1c": Location.onec_ref, "billz": Location.billz_shop_id}.get(system)
    loc = s.scalar(select(Location).where(col == code)) if col is not None else None
    return loc or s.get(Location, code)


def ingest(s: Session, doc: Doc) -> dict:
    """Book (or only match) one external document. Returns what happened, for the sync log."""
    out = {"booked": 0, "duplicate": 0, "compared_only": 0, "unmapped": 0, "mirrored": 0}
    for dl in doc.lines:
        item = map_item(s, doc.system, dl.sku)
        loc = map_location(s, doc.system, dl.loc)
        to = map_location(s, doc.system, dl.to_loc)
        if item is None or loc is None or (doc.doc_type == "transfer" and to is None):
            out["unmapped"] += 1
            raise_exception(s, "unmapped", f"{doc.system}:{dl.sku}:{dl.loc}",
                            f"{doc.system} {doc.doc_type} {doc.doc_id}: item {dl.sku} / location {dl.loc} not mapped "
                            f"to OMAI — add the mapping", severity="low")
            continue
        if loc.movement_source != doc.system:
            out["compared_only"] += 1
            continue
        if doc.doc_type in INBOUND:
            cond = "quarantine" if doc.doc_type in ("return", "sale_return") else "saleable"
            line = Line(sku=item.sku, qty=dl.qty, to_loc=loc.code, to_cond=cond, lot=dl.lot)
        elif doc.doc_type in OUTBOUND:
            line = Line(sku=item.sku, qty=dl.qty, from_loc=loc.code, from_cond="saleable", lot=dl.lot)
        elif doc.doc_type == "transfer":
            # an ERP transfer posts both sides at once; it goes through transit only if OMAI scans the
            # destination (then the destination custodian's receipt completes it)
            if to.movement_source == "omai":
                line = Line(sku=item.sku, qty=dl.qty, from_loc=loc.code, from_cond="saleable", to_loc=TRANSIT,
                            to_bin=f"{doc.system}:{doc.doc_id}", to_cond="saleable", lot=dl.lot)
            else:
                line = Line(sku=item.sku, qty=dl.qty, from_loc=loc.code, from_cond="saleable", to_loc=to.code,
                            to_cond="saleable", lot=dl.lot)
        else:
            raise StockError(f"unknown document type {doc.doc_type}")
        shared = f"{doc.shared_ref}:{item.sku}:{dl.line_no}" if doc.shared_ref else None
        res = record(s, idem_key=f"{doc.system}:{doc.doc_type}:{doc.doc_id}:{dl.line_no}", source=doc.system,
                     kind=doc.doc_type, lines=[line], event_time=doc.event_time, shared_ref=shared,
                     doc_ref=doc.doc_ref or f"{doc.system}:{doc.doc_id}", strict=False)
        if res.created:
            out["booked"] += 1
        elif res.mirrored_of:
            out["mirrored"] += 1
        else:
            out["duplicate"] += 1
    return out


def set_external_balance(s: Session, system: str, loc_code: str, item_code: str, qty: float) -> bool:
    item = map_item(s, system, item_code)
    loc = map_location(s, system, loc_code)
    if item is None or loc is None:
        return False
    row = s.scalar(select(ExternalBalance).where(ExternalBalance.source == system,
                                                 ExternalBalance.location_code == loc.code,
                                                 ExternalBalance.sku == item.sku))
    if row is None:
        s.add(ExternalBalance(source=system, location_code=loc.code, sku=item.sku, qty=qty))
    else:
        row.qty, row.read_at = qty, now_utc()
    return True


def upsert_order(s: Session, system: str, order_ref: str, whs: str, customer: str, lines: list[dict]) -> None:
    loc = map_location(s, system, whs)
    if loc is None:
        return
    mapped = []
    for ln in lines:
        item = map_item(s, system, ln["sku"])
        if item:
            mapped.append({"sku": item.sku, "qty": float(ln["qty"]), **({"serials": ln["serials"]} if ln.get("serials")
                                                                       else {})})
    order = s.scalar(select(DispatchOrder).where(DispatchOrder.order_ref == order_ref))
    if order is None:
        s.add(DispatchOrder(order_ref=order_ref, location_code=loc.code, customer=customer, lines=mapped))
    elif order.status == "open":
        order.lines = mapped


def upsert_expected(s: Session, system: str, po_ref: str, whs: str, item_code: str, qty: float,
                    due: datetime | None, posted_qty: float = 0) -> None:
    """A planned arrival. ``posted_qty`` = what the ERP already booked as received (ERP posting ≠ arrival)."""
    item = map_item(s, system, item_code)
    loc = map_location(s, system, whs)
    if item is None or loc is None:
        return
    row = s.scalar(select(ExpectedReceipt).where(ExpectedReceipt.po_ref == po_ref, ExpectedReceipt.sku == item.sku))
    if row is None:
        s.add(ExpectedReceipt(po_ref=po_ref, sku=item.sku, location_code=loc.code, qty_expected=qty, due_at=due))
    else:
        row.qty_expected, row.due_at = qty, due
        row.erp_posted = posted_qty >= row.qty_received > 0
    audit(s, None, "expected.upsert", po_ref, sku=item.sku, qty=qty)
