"""1C over the standard OData interface (ref [3]) — read-only, GET only.

1C is compared, not booked: posted stock movements, returns, adjustments and balances are read so differences
with SAP and the physical ledger show up. Finance confirms which 1C fields are authoritative; the entity names
are therefore settings (defaults fit «Бухгалтерия для Узбекистана», the same base mgmg-agents reads):

* ``ONEC_BALANCE_ENTITY`` — a stock balance virtual table, e.g.
  ``AccumulationRegister_ТоварыНаСкладах/Balance`` (trade configurations) or the accounting register
  ``AccountingRegister_Хозрасчетный/Balance`` filtered to the stock accounts;
* ``ONEC_ITEM_FIELD`` / ``ONEC_WAREHOUSE_FIELD`` / ``ONEC_QTY_FIELD`` — which columns hold item, warehouse, qty.

Inventory *values* (sums) are read only for permitted reviewers and never shown to warehouse staff.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from omai.config import settings
from omai.connectors.ingest import set_external_balance
from omai.sources import mark_failure, mark_success


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

    def get(self, resource: str, params: dict[str, str] | None = None) -> Any:
        r = self.http.get(quote(resource, safe="/()=',$:"), params={"$format": "json", **(params or {})})
        if r.status_code >= 400:
            raise OneCError(f"{resource[:80]}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()


def sync(s: Session, client: OneC | None = None) -> dict:
    entity = os.getenv("ONEC_BALANCE_ENTITY", "AccumulationRegister_ТоварыНаСкладах/Balance")
    f_item = os.getenv("ONEC_ITEM_FIELD", "Номенклатура_Key")
    f_whs = os.getenv("ONEC_WAREHOUSE_FIELD", "Склад_Key")
    f_qty = os.getenv("ONEC_QTY_FIELD", "КоличествоBalance")
    own = client is None
    try:
        client = client or OneC()
        rows = (client.get(entity) or {}).get("value", [])
        n = 0
        for r in rows:
            n += set_external_balance(s, "1c", str(r.get(f_whs)), str(r.get(f_item)), float(r.get(f_qty) or 0))
        mark_success(s, "1c")
        return {"rows": len(rows), "mapped": n}
    except Exception as exc:
        mark_failure(s, "1c", str(exc))
        raise
    finally:
        if own and client is not None:
            client.close()
