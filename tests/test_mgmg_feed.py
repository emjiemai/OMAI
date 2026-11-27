"""SAP from mgmg's database (option b): items from SAP, stock compared, freshness = the gateway's last push."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from omai.connectors import mgmg_feed
from omai.models import ExternalBalance, Item, SourceSync, now_utc
from omai.sources import is_stale


def _data(pushed_ago_min=10, stock=None):
    return {"pushed": {"stock_items": now_utc() - timedelta(minutes=pushed_ago_min), "products": None},
            "stock": stock if stock is not None else [("010-1", "Fenix 8", "01", 3), ("SVC", "Service", "01", 1)],
            "items": [("010-1", "Fenix 8 Pro", "Garmin", "4711", "pcs", True, True),
                      ("SVC-0", "Delivery", "Services", None, None, False, True),
                      ("010-2", "Strap", "Garmin", None, "pcs", True, True)]}


def test_items_stock_and_freshness(s):
    s.add(Item(sku="010-1", name="from 1C", sap_code="010-1", onec_ref="a"))
    s.flush()
    out = mgmg_feed.sync(s, _data())
    assert out["items"] == {"created": 2, "updated": 1}          # 010-2 and SVC (in stock); SVC-0 is no stock item
    assert s.get(Item, "010-1").name == "Fenix 8 Pro" and s.get(Item, "010-1").barcode == "4711"
    assert s.get(Item, "010-1").onec_ref == "a" and s.get(Item, "SVC-0") is None
    b = s.scalar(select(ExternalBalance).where(ExternalBalance.source == "sap", ExternalBalance.sku == "010-1"))
    assert (b.location_code, b.qty) == ("WH1", 3)     # the seed's WH1 is SAP 01
    assert not is_stale(s, "sap")
    assert mgmg_feed.sync(s, _data())["items"] == {"created": 0, "updated": 0}


def test_old_push_is_stale_and_empty_picture_zeroes_nothing(s):
    mgmg_feed.sync(s, _data())
    out = mgmg_feed.sync(s, _data(pushed_ago_min=180, stock=[]))
    assert out["stock"]["zeroed"] == 0
    assert s.scalar(select(ExternalBalance).where(ExternalBalance.sku == "010-1")).qty == 3
    assert is_stale(s, "sap")


def test_no_push_yet_fails(s):
    with pytest.raises(mgmg_feed.FeedError):
        mgmg_feed.sync(s, {"pushed": {}, "stock": [], "items": []})
    assert s.get(SourceSync, "sap") is None


def test_open_sap_orders_become_dispatch_orders(s):
    """SAP open sales-order lines → one dispatch order per order × warehouse, waiting for release; an order SAP no
    longer lists is closed in OMAI if not started, flagged if already released (27.11.2026)."""
    from omai.models import DispatchOrder, Exception_, Item

    s.add_all([Item(sku="TAN9", name="Tanita", sap_code="TAN9")])
    s.flush()
    rows = [(501, 1501, 0, "01", "GAR-F8", 2), (501, 1501, 1, "01", "TAN9", 1),      # one order, one warehouse
            (502, 1502, 0, "01", "TAN9", 1), (502, 1502, 1, "G05", "TAN9", 3),       # one order, two warehouses
            (503, 1503, 0, "01", "TAN9", 0)]                                          # nothing left to deliver
    data = {**_data(), "orders": rows}
    out = mgmg_feed.sync(s, data)["orders"]
    refs = {o.order_ref: o for o in s.scalars(select(DispatchOrder))}
    assert out["orders"] == 3 and set(refs) == {"SAP 1501", "SAP 1502-01", "SAP 1502-G05"}
    assert refs["SAP 1501"].lines == [{"sku": "FENIX8", "qty": 2.0}, {"sku": "TAN9", "qty": 1.0}]
    assert refs["SAP 1501"].release_status == "pending" and refs["SAP 1501"].customer == ""
    assert refs["SAP 1502-G05"].location_code == "ST2"

    refs["SAP 1502-01"].release_status = "approved"
    out = mgmg_feed.sync(s, {**data, "orders": rows[:2]})["orders"]                   # 1502 delivered in SAP
    assert out == {"orders": 1, "closed_in_sap": 1, "flagged": 1}
    assert refs["SAP 1502-G05"].status == "closed_in_sap" and refs["SAP 1502-01"].status == "open"
    assert s.scalar(select(Exception_).where(Exception_.kind == "order_closed_in_sap")).ref == "SAP 1502-01"


def test_orders_function_not_installed_skips_orders(s):
    out = mgmg_feed.sync(s, {**_data(), "orders": None})
    assert "orders" not in out
