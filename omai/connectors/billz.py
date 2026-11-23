"""Billz 2.0 — store sales and returns, store stock (read-only).

Documented and used in production by mgmg-agents (docs.billz.io, read 2026-10-01):
``POST /v1/auth/login {"secret_token"}`` → ``data.access_token``; ``GET /v1/shop``;
``GET /v1/product-general-table`` with ``detalization_by_position=true`` → one row per cheque line
(``order_id``, ``order_number``, ``shop_id``, ``product_sku``/``product_barcode``,
``net_sold_measurement_value`` — negative for returns). 2 requests/second.

Stock per shop: the path is a setting (``BILLZ_STOCK_PATH``, default ``/v2/products``) — **verify with Billz for
your plan** before relying on it (the spec requires vendor verification).

Each cheque line becomes a ``sale`` / ``sale_return`` keyed by the Billz order id. A store whose
``movement_source`` is ``billz`` books only these; the shop's SAP A/R invoice for the same cheque is compared, not
booked, so the sale never counts twice.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from omai.config import settings
from omai.connectors.ingest import Doc, DocLine, ingest, set_external_balance
from omai.models import Location
from omai.sources import mark_failure, mark_success

PAGE, MAX_PAGES, PAUSE = 100, 50, 0.6


class BillzError(RuntimeError):
    pass


class Billz:
    def __init__(self, transport: httpx.BaseTransport | None = None, pause: float = PAUSE) -> None:
        if not settings.billz_configured:
            raise BillzError("Billz is not configured (BILLZ_SECRET_TOKEN)")
        self.http = httpx.Client(base_url=settings.billz_base_url, timeout=60, transport=transport)
        self.token: str | None = None
        self.pause = pause

    def close(self) -> None:
        self.http.close()

    def _login(self) -> None:
        r = self.http.post("/v1/auth/login", json={"secret_token": settings.billz_secret_token.get_secret_value()})
        if r.status_code >= 400:
            raise BillzError(f"login: HTTP {r.status_code}")
        self.token = ((r.json() or {}).get("data") or {}).get("access_token")
        if not self.token:
            raise BillzError("login: no access_token")

    def get(self, path: str, params: dict[str, Any]) -> dict:
        if self.token is None:
            self._login()
        for attempt in range(4):
            r = self.http.get(path, params=params, headers={"Authorization": f"Bearer {self.token}"})
            if r.status_code == 401 and attempt == 0:
                self._login()
                continue
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            if r.status_code >= 400:
                raise BillzError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
            return r.json()
        raise BillzError(f"{path}: rate-limited")

    def pages(self, path: str, params: dict[str, Any], key: str) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            if page > 1 and self.pause:
                time.sleep(self.pause)
            body = self.get(path, {**params, "page": page, "limit": PAGE})
            batch = body.get(key) or []
            rows.extend(batch)
            if not batch or len(rows) >= int(body.get("count") or 0) or len(batch) < PAGE:
                return rows
        raise BillzError(f"{path}: more than {MAX_PAGES} pages")

    def positions(self, day: date, shop_ids: list[str]) -> list[dict]:
        d = day.strftime("%Y-%m-%d")
        return self.pages("/v1/product-general-table",
                          {"start_date": d, "end_date": d, "shop_ids": ",".join(shop_ids), "currency": "UZS",
                           "detalization": "day", "detalization_by_position": "true"}, "products_stats_by_date")


def docs_from_positions(rows: list[dict], day: date) -> list[Doc]:
    by_order: dict[str, Doc] = {}
    for i, r in enumerate(rows):
        qty = float(r.get("net_sold_measurement_value") or 0)
        if qty == 0:
            continue
        oid = str(r.get("order_id") or r.get("order_number"))
        kind = "sale" if qty > 0 else "sale_return"
        key = f"{oid}:{kind}"
        doc = by_order.get(key)
        if doc is None:
            doc = Doc(system="billz", doc_type=kind, doc_id=key, shared_ref=f"billz:{oid}",
                      event_time=_event_time(r, day), doc_ref=f"Billz {r.get('order_number') or oid}")
            by_order[key] = doc
        doc.lines.append(DocLine(sku=str(r.get("product_sku") or r.get("product_barcode")), qty=abs(qty),
                                 loc=str(r.get("shop_id") or r.get("shop_name")), line_no=len(doc.lines),
                                 lot=str(r.get("product_serial") or "")))
    return list(by_order.values())


def _event_time(r: dict, day: date) -> datetime:
    for k in ("order_created_at", "created_at", "sold_at"):
        if r.get(k):
            try:
                t = datetime.fromisoformat(str(r[k]).replace("Z", "+00:00"))
                return t if t.tzinfo else t.replace(tzinfo=settings.tz)
            except ValueError:
                pass
    return datetime.combine(day, datetime.min.time(), settings.tz).replace(hour=12)


def sync(s: Session, client: Billz | None = None, days: int = 2) -> dict:
    from sqlalchemy import select

    own = client is None
    stats = {"sales_docs": 0, "booked": 0, "balances": 0}
    try:
        client = client or Billz()
        shops = [l.billz_shop_id for l in s.scalars(select(Location).where(Location.billz_shop_id.is_not(None)))]
        today = datetime.now(settings.tz).date()
        for back in range(days - 1, -1, -1):
            day = today - timedelta(days=back)
            if not shops:
                break
            for doc in docs_from_positions(client.positions(day, shops), day):
                stats["sales_docs"] += 1
                stats["booked"] += ingest(s, doc)["booked"]
        stock_path = os.getenv("BILLZ_STOCK_PATH", "/v2/products")
        if stock_path and shops:
            try:
                for p in client.pages(stock_path, {}, "products"):
                    for m in p.get("shop_measurement_values") or []:
                        if str(m.get("shop_id")) in shops:
                            stats["balances"] += set_external_balance(
                                s, "billz", str(m["shop_id"]), str(p.get("sku") or p.get("id")),
                                float(m.get("active_measurement_value") or 0))
            except BillzError as exc:
                stats["stock_error"] = str(exc)
        mark_success(s, "billz")
        return stats
    except Exception as exc:
        mark_failure(s, "billz", str(exc))
        raise
    finally:
        if own and client is not None:
            client.close()
