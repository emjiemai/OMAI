"""SAP Business One — through the SAP gateway push (as in mgmg-agents).

SAP is not reached from the cloud. The gateway machine (HANA behind it, read-only) pushes item stock and
inventory documents to ``POST /sap/push`` with the header ``X-Omai-Secret: $SAP_PUSH_WEBHOOK_SECRET``:

* ``{"kind": "stock", "rows": [{"ItemCode", "WhsCode", "OnHand"}]}`` — compared, never booked;
* ``{"kind": "documents", "entity": "<SAP document set>", "rows": [<document with DocumentLines>]}`` — mapped by
  ``doc_from_sap`` and booked only for locations whose ``movement_source`` is ``sap``.

Field names are SAP's own (OPDN/PDN1, ODLN, ORDN, OIGN, OIGE, OWTR as the Service Layer names them). OMAI never
posts to SAP: an approved adjustment is posted by IT/accounting, who confirm it.
"""

from __future__ import annotations


from datetime import datetime

from sqlalchemy.orm import Session

from omai.connectors.ingest import Doc, DocLine, ingest, set_external_balance
from omai.sources import mark_success

from omai.config import settings

DOC_SETS = {
    # entity set: OMAI document type
    "PurchaseDeliveryNotes": "receipt",
    "InventoryGenEntries": "receipt_adj",
    "InventoryGenExits": "issue_adj",
    "DeliveryNotes": "delivery",
    "Returns": "return",
    "StockTransfers": "transfer",
}


class SapError(RuntimeError):
    pass


def _dt(date_s: str | None, time_s: str | None = None) -> datetime:
    d = datetime.fromisoformat((date_s or "1970-01-01")[:10])
    if time_s and ":" in str(time_s):
        h, m = str(time_s).split(":")[:2]
        d = d.replace(hour=int(h), minute=int(m))
    return d.replace(tzinfo=settings.tz)


def doc_from_sap(entity: str, d: dict) -> Doc:
    """One SAP marketing / inventory document → ``Doc``. Serial and batch numbers become one line each."""
    dtype = DOC_SETS[entity]
    doc = Doc(system="sap", doc_type=dtype, doc_id=f"{entity}:{d['DocEntry']}",
              event_time=_dt(d.get("DocDate") or d.get("TaxDate"), d.get("DocTime")),
              doc_ref=f"SAP {entity} {d.get('DocNum')}",
              shared_ref=(f"order:{d['NumAtCard']}" if d.get("NumAtCard") else None))
    for ln in d.get("DocumentLines") or d.get("StockTransferLines") or []:
        base = dict(sku=ln["ItemCode"], loc=ln.get("WarehouseCode") or d.get("FromWarehouse"),
                    to_loc=ln.get("WarehouseCode") if dtype == "transfer" else None)
        if dtype == "transfer":
            base["loc"] = ln.get("FromWarehouseCode") or d.get("FromWarehouse")
        serials = ln.get("SerialNumbers") or []
        batches = ln.get("BatchNumbers") or []
        n = int(ln.get("LineNum", 0)) * 1000
        if serials:
            for i, sn in enumerate(serials):
                doc.lines.append(DocLine(qty=1, lot=sn.get("InternalSerialNumber") or sn.get("ManufacturerSerialNumber")
                                         or "", line_no=n + i, **base))
        elif batches:
            for i, b in enumerate(batches):
                doc.lines.append(DocLine(qty=float(b["Quantity"]), lot=b["BatchNumber"], line_no=n + i, **base))
        else:
            doc.lines.append(DocLine(qty=float(ln.get("InventoryQuantity") or ln["Quantity"]), line_no=n, **base))
    return doc


def ingest_payload(s: Session, payload: dict) -> dict:
    """The gateway push: ``{"kind": "documents", "entity": "...", "rows": [...]}`` or
    ``{"kind": "stock", "rows": [{"ItemCode", "WhsCode", "OnHand"}]}``."""
    kind = payload.get("kind")
    out = {"rows": 0, "booked": 0}
    if kind == "stock":
        for r in payload.get("rows", []):
            out["rows"] += set_external_balance(s, "sap", r["WhsCode"], r["ItemCode"], float(r.get("OnHand") or 0))
    elif kind == "documents":
        entity = payload["entity"]
        if entity not in DOC_SETS:
            raise SapError(f"unsupported entity {entity}")
        for d in payload.get("rows", []):
            out["rows"] += 1
            out["booked"] += ingest(s, doc_from_sap(entity, d))["booked"]
    else:
        raise SapError("kind must be stock or documents")
    mark_success(s, "sap")
    return out
