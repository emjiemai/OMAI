"""SAP Business One — Service Layer (the supported connector, ref [2]) and the gateway push.

Two SAP users, two permissions:
* ``SAP_READ_USER`` — reads items, warehouses, bins, stock, orders, receipts, issues, transfers, serials,
  batches and counting documents;
* ``SAP_POST_USER`` — used only by ``SapPoster`` to post an adjustment that a human approved in OMAI.

If the Service Layer is not reachable from the cloud (the mgmg-agents situation: HANA behind a gateway on the
office machine), the gateway machine pushes the same documents to ``POST /sap/push`` and they go through the
same mapping (``ingest_payload``).

Service Layer facts relied on (SAP B1 SL API reference, 10.0): ``POST /Login`` → cookie ``B1SESSION``
(30 min idle); OData v4 paging with ``Prefer: odata.maxpagesize`` and ``@odata.nextLink``; entity sets
``Items``, ``Warehouses``, ``BinLocations``, ``Orders``, ``PurchaseOrders``, ``PurchaseDeliveryNotes``,
``DeliveryNotes``, ``Returns``, ``InventoryGenEntries``, ``InventoryGenExits``, ``StockTransfers``,
``InventoryCountings``; ``UpdateDate`` for incremental reads.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterator

import httpx
from sqlalchemy.orm import Session

from omai.config import settings
from omai.connectors.ingest import Doc, DocLine, ingest, set_external_balance, upsert_expected, upsert_order
from omai.ledger import audit
from omai.models import Approval, Item, Location
from omai.sources import mark_failure, mark_success

PAGE = 200
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


class ServiceLayer:
    def __init__(self, user: str | None = None, password: str | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.user = user or settings.sap_read_user
        self.password = password if password is not None else settings.sap_read_password.get_secret_value()
        self.http = httpx.Client(base_url=settings.sap_sl_url.rstrip("/") + "/", timeout=120,
                                 verify=settings.sap_verify_tls, transport=transport)
        self._logged_in = False

    def __enter__(self) -> "ServiceLayer":
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self._logged_in:
                self.http.post("Logout")
        finally:
            self.http.close()

    def login(self) -> None:
        r = self.http.post("Login", json={"CompanyDB": settings.sap_company_db, "UserName": self.user,
                                          "Password": self.password})
        if r.status_code >= 400:
            raise SapError(f"SAP login failed: HTTP {r.status_code} {_msg(r)}")
        self._logged_in = True

    def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        if not self._logged_in:
            self.login()
        r = self.http.request(method, path, **kw)
        if r.status_code == 401:   # session timed out
            self.login()
            r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise SapError(f"{method} {path}: HTTP {r.status_code} {_msg(r)}")
        return r

    def pages(self, path: str, params: dict[str, str] | None = None) -> Iterator[dict]:
        headers = {"Prefer": f"odata.maxpagesize={PAGE}"}
        url: str | None = path
        first = True
        while url:
            r = self._req("GET", url, params=params if first else None, headers=headers)
            first = False
            body = r.json()
            yield from body.get("value", [])
            nxt = body.get("@odata.nextLink") or body.get("odata.nextLink")
            url = nxt.split("/b1s/v1/")[-1].split("/b1s/v2/")[-1] if nxt else None

    def post(self, path: str, body: dict) -> dict:
        return self._req("POST", path, json=body).json()


def _msg(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error", {}).get("message", {}).get("value") or r.text[:200])
    except ValueError:
        return r.text[:200]


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


def sync(s: Session, sl: ServiceLayer | None = None, since: datetime | None = None) -> dict:
    """Pull master data, stock, open orders/POs and documents changed since the cursor."""
    from omai.models import SourceSync

    row = s.get(SourceSync, "sap")
    since = since or (datetime.fromisoformat(row.cursor) if row and row.cursor else datetime.now(settings.tz)
                      - timedelta(days=2))
    stats: dict[str, Any] = {"docs": {}, "balances": 0}
    started = datetime.now(settings.tz)
    own = sl is None
    sl = sl or ServiceLayer()
    try:
        # master: map items that already exist in OMAI, add new ones (category set later by a reviewer)
        for it in sl.pages("Items", {"$select": "ItemCode,ItemName,BarCode,ManageSerialNumbers,ManageBatchNumbers,"
                                                "InventoryUOM,ItemWarehouseInfoCollection",
                                     "$filter": f"UpdateDate ge '{since:%Y-%m-%d}'"}):
            item = s.get(Item, it["ItemCode"])
            if item is None:
                item = Item(sku=it["ItemCode"], name=it["ItemName"] or it["ItemCode"], sap_code=it["ItemCode"])
                s.add(item)
            item.barcode = it.get("BarCode") or item.barcode
            item.serial_tracked = it.get("ManageSerialNumbers") == "tYES"
            item.batch_tracked = it.get("ManageBatchNumbers") == "tYES"
            item.unit = it.get("InventoryUOM") or item.unit
            for w in it.get("ItemWarehouseInfoCollection") or []:
                stats["balances"] += set_external_balance(s, "sap", w["WarehouseCode"], it["ItemCode"],
                                                          float(w.get("InStock") or 0))
        s.flush()
        for entity in DOC_SETS:
            n = 0
            for d in sl.pages(entity, {"$filter": f"UpdateDate ge '{since:%Y-%m-%d}'"}):
                if d.get("Cancelled") == "tYES":
                    continue
                r = ingest(s, doc_from_sap(entity, d))
                n += r["booked"]
            stats["docs"][entity] = n
        for o in sl.pages("Orders", {"$filter": "DocumentStatus eq 'bost_Open'"}):
            whs = (o.get("DocumentLines") or [{}])[0].get("WarehouseCode")
            upsert_order(s, "sap", f"SO{o['DocNum']}", whs, o.get("CardName") or "",
                         [{"sku": l["ItemCode"], "qty": l.get("RemainingOpenQuantity") or l["Quantity"]}
                          for l in o.get("DocumentLines") or []])
        for po in sl.pages("PurchaseOrders", {"$filter": "DocumentStatus eq 'bost_Open'"}):
            for l in po.get("DocumentLines") or []:
                upsert_expected(s, "sap", f"PO{po['DocNum']}", l.get("WarehouseCode"), l["ItemCode"],
                                float(l["Quantity"]), _dt(po.get("DocDueDate")),
                                posted_qty=float(l["Quantity"]) - float(l.get("RemainingOpenQuantity") or 0))
        mark_success(s, "sap", cursor=started.isoformat())
    except Exception as exc:
        mark_failure(s, "sap", str(exc))
        raise
    finally:
        if own:
            sl.__exit__(None, None, None)
    return stats


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


class SapPoster:
    """Posts an approved adjustment with the separate posting user. Returns True when SAP accepted it."""

    def __init__(self, s: Session, transport: httpx.BaseTransport | None = None) -> None:
        self.s = s
        self.transport = transport

    def __call__(self, approval: Approval) -> bool | None:
        if not settings.sap_configured or not settings.sap_post_user:
            return None   # not connected: OMAI records it; IT posts by hand and confirms
        p = approval.payload
        loc = self.s.get(Location, p["loc"])
        item = self.s.get(Item, p["sku"])
        entity = "InventoryGenEntries" if p["diff"] > 0 else "InventoryGenExits"
        line: dict[str, Any] = {"ItemCode": item.sap_code or item.sku, "Quantity": abs(p["diff"]),
                                "WarehouseCode": loc.sap_whs or loc.code}
        if p.get("lot") and item.serial_tracked:
            line["SerialNumbers"] = [{"InternalSerialNumber": p["lot"]}]
        elif p.get("lot") and item.batch_tracked:
            line["BatchNumbers"] = [{"BatchNumber": p["lot"], "Quantity": abs(p["diff"])}]
        body = {"Comments": f"OMAI approval {approval.id} count {p.get('count_id', '')[:8]}: {approval.reason}"[:250],
                "DocumentLines": [line]}
        try:
            with ServiceLayer(settings.sap_post_user, settings.sap_post_password.get_secret_value(),
                              self.transport) as sl:
                res = sl.post(entity, body)
            audit(self.s, None, "sap.post", approval.id, entity=entity, doc=res.get("DocEntry"))
            return True
        except SapError as exc:
            audit(self.s, None, "sap.post_failed", approval.id, error=str(exc))
            return False
