"""1C «Бухгалтерия для Узбекистана» on Clobus (1C:Fresh), standard OData — read-only, GET only (docs/backend.md §4).

1C is compared, not booked. This configuration has **no trade stock registers** (no
``AccumulationRegister_ТоварыНаСкладах`` — checked 23.11.2026 against all 1438 published sets); stock lives in the
accounting register:

* ``AccountingRegister_Хозрасчетный/Balance(Period=datetime'…')`` — ``Account_Key``, ``ExtDimension1..3`` with their
  ``…_Type``, ``КоличествоBalance`` (quantity). Sums (``СуммаBalance``) are never read here.
* Stock accounts (``ChartOfAccounts_Хозрасчетный.Code``, NSBU 21): 29xx товары, 10xx материалы, 28xx готовая
  продукция (``ONEC_STOCK_ACCOUNTS``). On them the subconto is item (``Catalog_Номенклатура``), batch (a document) and
  warehouse (``Catalog_Склады``) — found by ``_Type``, not by position.
* Item → OMAI: ``Item.onec_ref`` (the 1C ``Ref_Key``) when set, else 1C's **«Артикул» = the SAP ``ItemCode``**
  (``Item.sap_code``; 583 of SAP's 813 stocked items matched on 23.11 — the rest have another or no Артикул).
  Warehouse → OMAI: ``Location.onec_ref`` (the 1C склад ``Ref_Key``, set in the setup sheet; 15 склады).

Clobus limits (learned in mgmg-agents): reads are sequential here (more than ~3 at once → 429); the chart and the
item catalog are kept ``KEEP_SECONDS``; a catalog that hangs gives up after ``CATALOG_TIMEOUT`` and the older copy is
used. «410 Gone» = OData switched off in Clobus — only the owner can turn it back on.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from omai.config import ONEC_STOCK_ACCOUNTS, settings
from omai.connectors.ingest import set_external_balance
from omai.models import Item, Location
from omai.sources import mark_failure, mark_success

CHART = "ChartOfAccounts_Хозрасчетный"
REGISTER = "AccountingRegister_Хозрасчетный"
ITEMS = "Catalog_Номенклатура"
WAREHOUSES = "Catalog_Склады"
KEEP_SECONDS = 1800
CATALOG_TIMEOUT = 20.0
_kept: dict[str, tuple[float, list[dict]]] = {}


class OneCError(RuntimeError):
    pass


class OneC:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        if not settings.onec_configured:
            raise OneCError("1C is not configured (ONEC_ODATA_URL / ONEC_LOGIN / ONEC_PASSWORD)")
        self.http = httpx.Client(base_url=settings.onec_odata_url.rstrip("/") + "/",
                                 auth=(settings.onec_login, settings.onec_password.get_secret_value()),
                                 timeout=90, transport=transport)

    def close(self) -> None:
        self.http.close()

    def get(self, resource: str, params: dict[str, str] | None = None, timeout: float | None = None) -> Any:
        r = self.http.get(quote(resource, safe="/()=',$:"), params={"$format": "json", **(params or {})},
                          **({"timeout": timeout} if timeout else {}))
        if r.status_code == 410:
            raise OneCError(f"{resource[:60]}: HTTP 410 — OData is switched off in Clobus (the owner turns it on)")
        if r.status_code >= 400:
            raise OneCError(f"{resource[:60]}: HTTP {r.status_code}")
        return r.json()

    def listing(self, entity: str, select_: str, timeout: float | None = None) -> list[dict]:
        """A catalog / the chart: kept ``KEEP_SECONDS``; a failed or slow read falls back to the older copy."""
        key = f"{entity}?{select_}"
        kept = _kept.get(key)
        if kept and time.monotonic() - kept[0] < KEEP_SECONDS:
            return kept[1]
        try:
            rows = (self.get(entity, {"$select": select_}, timeout) or {}).get("value", [])
        except (httpx.HTTPError, OneCError):
            if kept:
                return kept[1]
            raise
        _kept[key] = (time.monotonic(), rows)
        return rows


def _dim(row: dict, catalog: str) -> str | None:
    """The subconto value whose type is ``catalog`` (``StandardODATA.Catalog_Склады`` …)."""
    for n in (1, 2, 3):
        if str(row.get(f"ExtDimension{n}_Type") or "").endswith(catalog):
            return str(row.get(f"ExtDimension{n}") or "") or None
    return None


def stock_rows(client: OneC, at: datetime) -> dict[tuple[str, str], float]:
    """(1C склад key, 1C item key) → quantity on the stock accounts at ``at``."""
    chart = client.listing(CHART, "Ref_Key,Code")
    stock_keys = {str(a["Ref_Key"]) for a in chart
                  if str(a.get("Code") or "").strip().startswith(ONEC_STOCK_ACCOUNTS)}
    period = at.astimezone(settings.tz).strftime("%Y-%m-%dT%H:%M:%S")
    rows = (client.get(f"{REGISTER}/Balance(Period=datetime'{period}')") or {}).get("value", [])
    qty: dict[tuple[str, str], float] = defaultdict(float)
    for r in rows:
        if str(r.get("Account_Key")) not in stock_keys:
            continue
        item, whs = _dim(r, ITEMS), _dim(r, WAREHOUSES)
        if item:
            qty[(whs or "", item)] += float(r.get("КоличествоBalance") or 0)
    return dict(qty)


def sync(s: Session, client: OneC | None = None, at: datetime | None = None) -> dict:
    own = client is None
    stats = {"positions": 0, "mapped": 0, "unmapped_items": 0, "unmapped_warehouses": 0, "no_warehouse": 0}
    try:
        client = client or OneC()
        positions = stock_rows(client, at or datetime.now(settings.tz))
        stats["positions"] = len(positions)
        by_ref = {i.onec_ref: i for i in s.scalars(select(Item).where(Item.onec_ref.is_not(None)))}
        by_sap = {i.sap_code.strip(): i for i in s.scalars(select(Item).where(Item.sap_code.is_not(None)))}
        articles = {str(r["Ref_Key"]): str(r.get("Артикул") or "").strip()
                    for r in client.listing(ITEMS, "Ref_Key,Артикул", CATALOG_TIMEOUT)}
        known_whs = {loc.onec_ref for loc in s.scalars(select(Location).where(Location.onec_ref.is_not(None)))}
        missing_items: set[str] = set()
        missing_whs: set[str] = set()
        totals: dict[tuple[str, str], float] = defaultdict(float)   # several 1C cards can be one OMAI item
        for (whs, ref), q in positions.items():
            item = by_ref.get(ref) or by_sap.get(articles.get(ref, "") or "\0")
            if not whs:
                stats["no_warehouse"] += 1
                continue
            if whs not in known_whs:
                missing_whs.add(whs)
                continue
            if item is None:
                missing_items.add(ref)
                continue
            totals[(whs, item.sku)] += q
        for (whs, sku), q in totals.items():
            stats["mapped"] += set_external_balance(s, "1c", whs, sku, q)
        stats["unmapped_items"], stats["unmapped_warehouses"] = len(missing_items), len(missing_whs)
        mark_success(s, "1c")
        return stats
    except Exception as exc:
        mark_failure(s, "1c", str(exc))
        raise
    finally:
        if own and client is not None:
            client.close()
