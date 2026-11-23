from __future__ import annotations

from datetime import time

import pytest
from sqlalchemy.orm import sessionmaker

from omai.db import Base, make_engine
from omai.models import Bin, Item, Location, User


@pytest.fixture()
def s():
    eng = make_engine("sqlite://")
    from omai import models  # noqa: F401
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng, expire_on_commit=False)()
    seed(session)
    yield session
    session.close()


def seed(s):
    allday = dict(shift_start=time(0, 0), shift_end=time(23, 59), workdays="7")
    s.add_all([
        User(id="otabek", name="Otabek", role="staff", login="otabek", location_code="WH1", manager_id="mgr", **allday),
        User(id="store", name="Store custodian", role="custodian", location_code="ST1", manager_id="mgr", **allday),
        User(id="chk", name="Checker", role="checker", **allday),
        User(id="mgr", name="Manager", role="manager", **allday),
        User(id="acc", name="Accountant", role="accountant", **allday),
        User(id="own", name="Owner", role="owner", **allday),
        Location(code="WH1", name="Main warehouse", kind="warehouse", sap_whs="01", custodian_id="otabek",
                 movement_source="omai"),
        Location(code="ST1", name="Garmin store", kind="store", sap_whs="G01", billz_shop_id="shop-1",
                 custodian_id="store", movement_source="omai"),
        Location(code="ST2", name="Billz store", kind="store", sap_whs="G05", billz_shop_id="shop-2",
                 movement_source="billz"),
        Location(code="TRANSIT", name="In transit", kind="transit"),
        Location(code="TECH1", name="Technician Aziz", kind="technician", custodian_id="mgr"),
    ])
    s.flush()
    s.add_all([
        Bin(location_code="WH1", code="G01", pick_rank=10),
        Bin(location_code="WH1", code="G02", pick_rank=60),
        Bin(location_code="WH1", code="F01", size_class="XL", max_load_kg=2000, floor_level=True),
        Bin(location_code="WH1", code="C01", chemicals_ok=True),
        Bin(location_code="ST1", code="S01"),
        Item(sku="FENIX8", name="Garmin Fenix 8", brand="Garmin", category="equipment", serial_tracked=True,
             abc_group="A", unit_cost=9_000_000, sap_code="GAR-F8", billz_id="gar-f8"),
        Item(sku="TANITA1", name="Tanita scale", brand="Tanita", abc_group="B", unit_cost=3_000_000, sap_code="TAN1",
             billz_id="tan1"),
        Item(sku="DET5", name="Detergent 5L", brand="LONDRY", category="detergent", hazardous=True,
             batch_tracked=True, expiry_tracked=True, unit_cost=100_000),
        Item(sku="PRIMUS", name="Primus washer", brand="Primus", category="equipment", heavy_handling=True,
             size_class="XL", weight_kg=600, unit_cost=150_000_000),
    ])
    s.flush()


@pytest.fixture()
def u(s):
    return {k: s.get(User, k) for k in ("otabek", "store", "chk", "mgr", "acc", "own")}
