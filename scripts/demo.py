"""Load demo data into a local test database (never production).

    python -m scripts.demo            # refuses if ENVIRONMENT=production

Demo logins (test only) — login / PIN / role:
    otabek  1111  staff (Main warehouse lead)
    store   2222  custodian (Garmin store)
    checker 3333  checker
    manager 4444  manager
    acc     5555  accountant
    owner   6666  owner

Creates: warehouse WH1 with bins, a Garmin store, Garmin / Tanita / Primus / detergent items, stock, an
expected PO (12 Tanita), an order waiting for release, a transfer to the store, and today's tasks
(opening check, receiving, a blind count).
"""

from __future__ import annotations

from datetime import time

from omai import routine, warehouse
from omai.auth import hash_pin
from omai.config import settings
from omai.db import init_db, session_scope
from omai.models import Bin, DispatchOrder, ExpectedReceipt, Item, Location, User, now_utc

PEOPLE = [("otabek", "Otabek", "staff", "1111", "WH1"), ("store", "Store custodian", "custodian", "2222", "ST1"),
          ("checker", "Checker", "checker", "3333", None), ("manager", "Manager", "manager", "4444", None),
          ("acc", "Accountant", "accountant", "5555", None), ("owner", "Owner", "owner", "6666", None)]


def main() -> None:
    if settings.environment == "production":
        raise SystemExit("ENVIRONMENT=production — demo data is for a local test database only")
    init_db()
    with session_scope() as s:
        if s.get(User, "otabek"):
            print("demo data already loaded")
            return
        for uid, name, role, pin, loc in PEOPLE:
            s.add(User(id=uid, name=name, role=role, login=uid, pin_hash=hash_pin(pin), location_code=loc,
                       manager_id="manager" if role in ("staff", "custodian") else None,
                       shift_start=time(0, 0), shift_end=time(23, 59), workdays="7", on_call=uid == "manager"))
        s.add_all([Location(code="WH1", name="Main warehouse", kind="warehouse", sap_whs="01", custodian_id="otabek",
                            movement_source="omai"),
                   Location(code="ST1", name="Garmin store", kind="store", sap_whs="G01", custodian_id="store",
                            movement_source="omai"),
                   Location(code="TRANSIT", name="In transit", kind="transit")])
        s.flush()
        s.add_all([Bin(location_code="WH1", code="G01", pick_rank=10), Bin(location_code="WH1", code="G02", pick_rank=40),
                   Bin(location_code="WH1", code="F01", size_class="XL", max_load_kg=2000, floor_level=True),
                   Bin(location_code="WH1", code="C01", chemicals_ok=True), Bin(location_code="ST1", code="S01"),
                   Item(sku="FENIX8", name="Garmin Fenix 8", brand="Garmin", category="equipment",
                        serial_tracked=True, abc_group="A", unit_cost=9_000_000),
                   Item(sku="TANITA1", name="Tanita BC-545N", brand="Tanita", abc_group="B", unit_cost=3_000_000),
                   Item(sku="PRIMUS", name="Primus FX180 washer", brand="Primus", category="equipment",
                        heavy_handling=True, size_class="XL", weight_kg=600, unit_cost=150_000_000, abc_group="A"),
                   Item(sku="DET5", name="Detergent 5L", brand="LONDRY", category="detergent", hazardous=True,
                        batch_tracked=True, expiry_tracked=True, unit_cost=100_000, abc_group="C")])
        s.flush()
        ot = s.get(User, "otabek")
        warehouse.receive(s, receipt_id="demo1", po_ref="OPENING", sku="TANITA1", location="WH1", good_qty=10,
                          actor=ot)
        warehouse.putaway(s, task_ref="putaway:demo1:TANITA1", sku="TANITA1", qty=10, location="WH1", scanned_bin="G01", actor=ot)
        warehouse.receive(s, receipt_id="demo2", po_ref="OPENING", sku="FENIX8", location="WH1", good_qty=3,
                          serials=["F8-001", "F8-002", "F8-003"], actor=ot)
        warehouse.putaway(s, task_ref="putaway:demo2:FENIX8", sku="FENIX8", qty=3, location="WH1", scanned_bin="G02", actor=ot)
        s.add(ExpectedReceipt(po_ref="PO-1001", sku="TANITA1", location_code="WH1", qty_expected=12, due_at=now_utc()))
        s.add(DispatchOrder(order_ref="SO-501", location_code="WH1", customer="Hilton laundry",
                            lines=[{"sku": "FENIX8", "qty": 1}]))
        s.flush()
        warehouse.allocate(s, s.get(DispatchOrder, s.query(DispatchOrder.id).scalar()))
        warehouse.create_transfer(s, from_loc="WH1", to_loc="ST1", lines=[{"sku": "TANITA1", "qty": 2}],
                                  assignee_id="otabek")
        routine.opening(s)
        routine.assign(s)
    print(__doc__)


if __name__ == "__main__":
    main()
