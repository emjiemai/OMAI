"""scripts/count_day: shelves from a range, and the baseline count by item after the pilot switch."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omai.ledger import Line, record
from omai.models import Location, Task, now_utc
from scripts import count_day


def test_expand_ranges():
    assert count_day.expand(["G01-G03", "floor", "A8-10"]) == ["G01", "G02", "G03", "FLOOR", "A8", "A9", "A10"]


def test_bins_added_once(s):
    assert count_day.add_bins(s, "WH1", ["X01-X03"])["added"] == 3
    assert count_day.add_bins(s, "WH1", ["X02-X04"])["added"] == 1


def test_baseline_needs_the_switch_then_counts_by_item(s):
    s.add(Location(code="01", name="Асосий омборхона", kind="warehouse", sap_whs="01"))
    s.flush()
    from omai.models import User
    s.get(User, "chk").login = "chk"
    deadline = now_utc() + timedelta(days=1)
    with pytest.raises(SystemExit):
        count_day.baseline(s, "01", "otabek", "chk", deadline)          # still run by SAP
    s.get(Location, "01").movement_source = "omai"
    record(s, idem_key="opening:01:TANITA1", source="sap", kind="opening",
           lines=[Line(sku="TANITA1", qty=4, to_loc="01", to_cond="saleable")], strict=False)
    count_day.add_bins(s, "01", ["G01-G03"])                            # shelves exist, stock not on them yet
    with pytest.raises(SystemExit):
        count_day.baseline(s, "01", "otabek", "otabek", deadline)        # counter = checker
    c = count_day.baseline(s, "01", "otabek", "chk", deadline)
    assert [(line.bin, line.sku) for line in c.lines] == [("", "TANITA1")]   # by item, no "empty shelf" lines
    assert s.query(Task).filter(Task.ref == f"count:{c.id}", Task.assignee_id == "otabek").count() == 1
    with pytest.raises(SystemExit):
        count_day.baseline(s, "01", "otabek", "chk", deadline)          # one open count at a time
    assert "baseline" in count_day.show(s, "01")[0]
