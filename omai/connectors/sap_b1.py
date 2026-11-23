"""SAP Business One — the gateway's own rows, as the company's SAP gateway pushes them (docs/backend.md §3).

SAP (HANA, schema ``MGM``) is never reached from the cloud. The gateway's fixed read-only tools return rows with
SAP's **table column names** (not Service Layer JSON); ``push-ar-aging.ps1`` on the gateway PC sends each tool's rows
every 30 min as ``{"rows": [...], "complete": bool}`` — to mgmg-api, and to OMAI too once ``$OmaiHost`` is filled in
there (mgmg-agents ``scripts/sap-gateway-push/``):

    POST /sap/data/{kind}    header X-Omai-Secret: $SAP_PUSH_WEBHOOK_SECRET

Kinds OMAI reads (the gateway tool → its columns):

* ``stock_value`` (``get_stock_value``, OITW × OITM × OWHS): ``ItemCode, ItemName, WhsCode, WhsName, OnHand, …`` —
  rows with ``OnHand <> 0`` only, so a **complete** push sets every other SAP balance OMAI holds to 0. Compared, never
  booked.
* ``goods_receipts`` (OPDN + PDN1) → ``receipt``; ``stock_transfers`` (OWTR + WTR1: ``FromWhsCod`` → ``WhsCode``) →
  ``transfer``; ``inventory_moves`` (OIGN ``ObjType`` 59 → ``receipt_adj``, OIGE 60 → ``issue_adj``). One row = one
  document line (``DocEntry``, ``LineNum``, ``DocNum``, ``DocDate``, ``CANCELED``, ``ItemCode``, ``Quantity``,
  ``WhsCode``). Booked only where the location's ``movement_source`` is ``sap``.

Cancellations: SAP keeps the original (``CANCELED='Y'``) and adds a cancellation document (``'C'``) that undoes it.
Both are booked — the ``'C'`` one in the opposite direction — so the pair nets to zero whatever order they arrive in.

Not available from the gateway (no tool yet): serial / batch numbers (OSRI / OBTN), deliveries ODLN, returns ORDN.
OMAI never posts to SAP: an approved adjustment is posted by IT/accounting, who confirm it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from omai.config import settings
from omai.connectors.ingest import (
    Doc,
    DocLine,
    ingest,
    map_item,
    map_location,
    set_external_balance,
)
from omai.models import ExternalBalance
from omai.sources import mark_success

STOCK_KIND = "stock_value"
MOVE_KINDS = ("goods_receipts", "stock_transfers", "inventory_moves")
KINDS = (STOCK_KIND, *MOVE_KINDS)
# A cancellation document ('C') undoes the original: the opposite direction.
REVERSED = {"receipt": "issue", "receipt_adj": "issue_adj", "issue_adj": "receipt_adj"}


class SapError(RuntimeError):
    pass


def _dt(value: object) -> datetime:
    """``DocDate`` as HANA sends it (``2026-11-20`` or ``2026-11-20T00:00:00.000Z``) → that day, local time."""
    text = str(value or "1970-01-01")[:10]
    return datetime.fromisoformat(text).replace(tzinfo=settings.tz)


def _doc_type(kind: str, row: dict) -> str:
    if kind == "goods_receipts":
        return "receipt"
    if kind == "stock_transfers":
        return "transfer"
    obj = str(row.get("ObjType") or "").strip()
    if obj == "59":
        return "receipt_adj"
    if obj == "60":
        return "issue_adj"
    raise SapError(f"inventory_moves: unknown ObjType {obj!r}")


def docs_from_rows(kind: str, rows: list[dict]) -> list[Doc]:
    """Gateway rows (one per document line) → one ``Doc`` per SAP document."""
    if kind not in MOVE_KINDS:
        raise SapError(f"not a movement kind: {kind}")
    docs: dict[str, Doc] = {}
    for r in rows:
        dtype = _doc_type(kind, r)
        cancel = str(r.get("CANCELED") or "N").strip().upper() == "C"
        if cancel and dtype != "transfer":
            dtype = REVERSED[dtype]
        doc_id = f"{kind}:{r['DocEntry']}"
        doc = docs.get(doc_id)
        if doc is None:
            doc = Doc(system="sap", doc_type=dtype, doc_id=doc_id, event_time=_dt(r.get("DocDate")),
                      doc_ref=f"SAP {kind} {r.get('DocNum') or r['DocEntry']}" + (" (бекор)" if cancel else ""))
            docs[doc_id] = doc
        qty = abs(float(r.get("Quantity") or 0))
        if qty == 0:
            continue
        whs = str(r.get("WhsCode") or "").strip() or None
        if dtype == "transfer":
            src = str(r.get("FromWhsCod") or r.get("Filler") or "").strip() or None
            dst = whs or (str(r.get("ToWhsCode") or "").strip() or None)
            if cancel:
                src, dst = dst, src
            line = DocLine(sku=str(r["ItemCode"]).strip(), qty=qty, loc=src, to_loc=dst, line_no=int(r.get("LineNum") or 0))
        else:
            line = DocLine(sku=str(r["ItemCode"]).strip(), qty=qty, loc=whs, line_no=int(r.get("LineNum") or 0))
        doc.lines.append(line)
    return list(docs.values())


def ingest_stock(s: Session, rows: list[dict], complete: bool) -> dict:
    out = {"rows": len(rows), "mapped": 0, "zeroed": 0}
    seen: set[tuple[str, str]] = set()
    for r in rows:
        whs, code = str(r["WhsCode"]).strip(), str(r["ItemCode"]).strip()
        if set_external_balance(s, "sap", whs, code, float(r.get("OnHand") or 0)):
            out["mapped"] += 1
            seen.add((map_location(s, "sap", whs).code, map_item(s, "sap", code).sku))
    if complete:
        # the gateway leaves out zero stock: whatever SAP no longer lists is 0 now
        for b in s.scalars(select(ExternalBalance).where(ExternalBalance.source == "sap")):
            if (b.location_code, b.sku) not in seen and b.qty != 0:
                b.qty = 0
                out["zeroed"] += 1
    return out


def ingest_kind(s: Session, kind: str, body: dict) -> dict:
    """One gateway push: ``{"rows": [...], "complete": bool}`` for ``kind``."""
    if kind not in KINDS:
        raise SapError(f"unsupported kind {kind}; OMAI reads {', '.join(KINDS)}")
    rows = body.get("rows")
    if not isinstance(rows, list):
        raise SapError("body must be {\"rows\": [...], \"complete\": bool}")
    if kind == STOCK_KIND:
        out = ingest_stock(s, rows, bool(body.get("complete")))
    else:
        out = {"rows": len(rows), "documents": 0, "booked": 0, "compared_only": 0, "unmapped": 0}
        for doc in docs_from_rows(kind, rows):
            out["documents"] += 1
            res = ingest(s, doc)
            for k in ("booked", "compared_only", "unmapped"):
                out[k] += res[k]
    mark_success(s, "sap")
    return out


def ingest_payload(s: Session, payload: dict) -> dict:
    """``POST /sap/push`` (kept): ``{"kind": "<kind>", "rows": [...], "complete": bool}``."""
    return ingest_kind(s, str(payload.get("kind") or ""), payload)
