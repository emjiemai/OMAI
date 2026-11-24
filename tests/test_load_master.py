"""scripts/load_master: SAP warehouses and 1C items; several 1C cards with one Артикул are one item."""

from __future__ import annotations

import httpx
from sqlalchemy import select

from omai.config import settings
from omai.models import ExternalBalance, Item, Location
from scripts import load_master


def test_warehouses_load_once_with_sap_codes(s):
    assert load_master.load_warehouses(s)["created"] == len(load_master.WAREHOUSES)
    assert load_master.load_warehouses(s) == {"created": 0, "kept": len(load_master.WAREHOUSES)}
    loc = s.get(Location, "04")
    assert (loc.sap_whs, loc.kind, loc.movement_source) == ("04", "store", "sap")


def test_items_one_per_article_and_rerun_safe(s):
    positions = {("w1", "a"): 2, ("w1", "b"): 1, ("w2", "c"): 1, ("w1", "d"): 5}
    catalog = [{"Ref_Key": "a", "Code": "1", "Description": "Fenix 8", "Артикул": "010-1"},
               {"Ref_Key": "b", "Code": "2", "Description": "Fenix 8 (2)", "Артикул": "010-1"},
               {"Ref_Key": "c", "Code": "3", "Description": "Strap", "Артикул": "010-2"},
               {"Ref_Key": "d", "Code": "4", "Description": "Detergent", "Артикул": ""},
               {"Ref_Key": "e", "Code": "5", "Description": "No stock", "Артикул": "010-3"}]
    good, plan = load_master.plan_items(positions, catalog)
    assert plan["loadable"] == 2 and plan["no_article"] == 1 and plan["items_several_1c_cards"] == 1
    assert load_master.load_items(s, good)["created"] == 2
    s.flush()
    assert s.get(Item, "010-1").onec_ref is None and s.get(Item, "010-1").name == "Fenix 8"
    assert s.get(Item, "010-2").onec_ref == "c" and s.get(Item, "010-2").sap_code == "010-2"
    assert load_master.load_items(s, good)["kept"] == 2


def test_onec_sync_adds_cards_that_are_one_item(s, monkeypatch):
    from omai.connectors import onec

    monkeypatch.setattr(settings, "onec_odata_url", "https://1c.test/odata/")
    monkeypatch.setattr(settings, "onec_login", "u")
    onec._kept.clear()
    s.get(Location, "WH1").onec_ref = "whs-main"
    s.add(Item(sku="010-1", name="Fenix 8", sap_code="010-1"))
    s.add(Item(sku="OLD", name="Sold out", sap_code="OLD"))
    s.flush()
    s.add(ExternalBalance(source="1c", location_code="WH1", sku="OLD", qty=2))   # 1C no longer lists it
    s.flush()

    def row(item, qty):
        return {"Account_Key": "acc", "ExtDimension1": item, "ExtDimension1_Type": "StandardODATA.Catalog_Номенклатура",
                "ExtDimension2": "whs-main", "ExtDimension2_Type": "StandardODATA.Catalog_Склады",
                "КоличествоBalance": qty}

    def handler(req: httpx.Request) -> httpx.Response:
        if "ChartOfAccounts" in req.url.path:
            return httpx.Response(200, json={"value": [{"Ref_Key": "acc", "Code": "2910"}]})
        if "Balance" in req.url.path:
            return httpx.Response(200, json={"value": [row("a", 2), row("b", 3)]})
        return httpx.Response(200, json={"value": [{"Ref_Key": "a", "Артикул": "010-1"},
                                                   {"Ref_Key": "b", "Артикул": "010-1"}]})

    onec.sync(s, onec.OneC(transport=httpx.MockTransport(handler)))
    b = s.scalar(select(ExternalBalance).where(ExternalBalance.source == "1c", ExternalBalance.sku == "010-1"))
    assert b.qty == 5
    assert s.scalar(select(ExternalBalance).where(ExternalBalance.sku == "OLD")).qty == 0


def test_pilot_opens_from_sap_and_switches(s, monkeypatch):
    from contextlib import contextmanager

    from omai.ledger import balance
    from omai.sources import mark_success

    load_master.load_warehouses(s)
    s.add_all([Item(sku="A", name="A"), Item(sku="B", name="B")])
    s.flush()
    s.add_all([ExternalBalance(source="sap", location_code="01", sku="A", qty=3),
               ExternalBalance(source="sap", location_code="01", sku="B", qty=-1)])
    mark_success(s, "sap")
    s.flush()

    @contextmanager
    def same():
        yield s
    monkeypatch.setattr(load_master, "session_scope", same)
    load_master.pilot("01", apply=False)
    assert s.get(Location, "01").movement_source == "sap"
    load_master.pilot("01", apply=True)
    assert s.get(Location, "01").movement_source == "omai"
    assert balance(s, sku="A", loc="01") == 3 and balance(s, sku="B", loc="01") == 0
    import pytest
    with pytest.raises(SystemExit):
        load_master.pilot("01", apply=True)
