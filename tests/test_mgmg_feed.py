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
