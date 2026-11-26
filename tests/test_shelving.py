"""Shelving after the baseline: scan item + shelf + qty; resends count once; no quantities leak; auto-closes."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omai import warehouse
from omai.ledger import Line, StockError, balance, record
from omai.models import Bin, Count, Location, Task, User, now_utc


def _setup(s):
    s.add(Location(code="01", name="Асосий омборхона", kind="warehouse", sap_whs="01", movement_source="omai"))
    s.flush()
    record(s, idem_key="opening:01:TANITA1", source="sap", kind="opening",
           lines=[Line(sku="TANITA1", qty=4, to_loc="01", to_cond="saleable")], strict=False)
    return s.get(User, "otabek")


def test_shelving_task_rules_and_scans(s):
    otabek = _setup(s)
    due = now_utc() + timedelta(days=1)
    with pytest.raises(StockError):
        warehouse.shelving_task(s, "01", otabek, due)                    # no shelves yet
    s.add_all([Bin(location_code="01", code="G01"), Bin(location_code="01", code="G02")])
    s.add(Count(location_code="01", kind="baseline", counter_id="otabek", checker_id="chk", deadline=due,
                original_deadline=due, status="counting"))
    s.flush()
    with pytest.raises(StockError):
        warehouse.shelving_task(s, "01", otabek, due)                    # a count is open
    s.query(Count).one().status = "closed"
    task = warehouse.shelving_task(s, "01", otabek, due)
    assert warehouse.unshelved(s, "01") == [{"sku": "TANITA1", "name": "Tanita scale"}]   # no quantity
    r = warehouse.shelve(s, location="01", sku="TANITA1", qty=3, scanned_bin="G01", actor=otabek, scan_id="a")
    assert r["items_left"] == 1
    assert not warehouse.shelve(s, location="01", sku="TANITA1", qty=3, scanned_bin="G01", actor=otabek,
                                scan_id="a")["created"]                 # the page resent it: counted once
    assert balance(s, sku="TANITA1", loc="01", bin_="G01") == 3
    with pytest.raises(StockError) as exc:
        warehouse.shelve(s, location="01", sku="TANITA1", qty=5, scanned_bin="G02", actor=otabek, scan_id="b")
    assert "бор-йўғи" not in str(exc.value) and "кам" in str(exc.value)   # the remaining quantity isn't told
    r = warehouse.shelve(s, location="01", sku="TANITA1", qty=1, scanned_bin="G01", actor=otabek, scan_id="c")
    assert r["items_left"] == 0 and balance(s, sku="TANITA1", loc="01", bin_="G01") == 4
    assert s.get(Task, task.id).state == "accepted"


def test_no_shelving_during_a_count(s):
    otabek = _setup(s)
    due = now_utc() + timedelta(days=1)
    s.add(Bin(location_code="01", code="G01"))
    s.add(Count(location_code="01", kind="baseline", counter_id="otabek", checker_id="chk", deadline=due,
                original_deadline=due, status="open"))
    s.flush()
    with pytest.raises(StockError):
        warehouse.shelve(s, location="01", sku="TANITA1", qty=1, scanned_bin="G01", actor=otabek, scan_id="x")
