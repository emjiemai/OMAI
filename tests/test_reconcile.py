"""Each location is compared against its own authority (movement_source), never against an empty ledger."""

from __future__ import annotations

from sqlalchemy import select

from omai import insights
from omai.ledger import raise_exception
from omai.models import Exception_, ExternalBalance, Location
from omai.sources import mark_success


def _open(s):
    return {e.ref for e in s.scalars(select(Exception_).where(Exception_.kind == "system_mismatch",
                                                             Exception_.status == "open"))}


def _eb(s, source, loc, sku, qty):
    s.add(ExternalBalance(source=source, location_code=loc, sku=sku, qty=qty))


def test_sap_location_compares_others_with_sap(s):
    s.add_all([Location(code="09", name="Танита", kind="warehouse", sap_whs="09")])   # movement_source sap
    s.flush()
    mark_success(s, "sap")
    raise_exception(s, "system_mismatch", "sap:09:TANITA1", "old: SAP vs empty ledger")
    _eb(s, "sap", "09", "TANITA1", 4)
    _eb(s, "1c", "09", "TANITA1", 4)
    _eb(s, "1c", "09", "FR265", 2)          # SAP lists nothing there: SAP's picture is complete → 0
    s.flush()
    insights.reconcile_systems(s)
    assert _open(s) == {"1c:09:FR265"}       # SAP itself is not compared; the old false one is closed


def test_omai_location_compares_with_the_ledger(s):
    mark_success(s, "sap")
    _eb(s, "sap", "WH1", "TANITA1", 3)       # WH1 is staff-scanned (omai); its ledger is empty here
    s.flush()
    insights.reconcile_systems(s)
    assert _open(s) == {"sap:WH1:TANITA1"}


def test_billz_abay_against_sap_warehouses_together(s):
    s.add_all([Location(code=c, name=c, kind="store", sap_whs=c) for c in ("G.A._01", "05", "G.A._02")])
    s.flush()
    mark_success(s, "sap")
    _eb(s, "sap", "G.A._01", "FR265", 2)
    _eb(s, "sap", "05", "FR265", 1)
    _eb(s, "sap", "G.A._02", "TANITA1", 1)
    _eb(s, "billz", "G.A._01", "FR265", 3)    # 2 + 1
    _eb(s, "billz", "G.A._01", "TANITA1", 2)  # SAP has 1
    s.flush()
    insights.reconcile_systems(s)
    assert _open(s) == {"billz:G.A._01:TANITA1"}


def test_nothing_compared_before_sap_ever_synced(s):
    s.add(Location(code="09", name="Танита", kind="warehouse", sap_whs="09"))
    _eb(s, "1c", "09", "TANITA1", 4)
    s.flush()
    insights.reconcile_systems(s)
    assert _open(s) == set()
