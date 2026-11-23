"""The eight acceptance tests from the IT task, in order."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

from omai import approvals, counting, tasks, warehouse
from omai.connectors.ingest import Doc, DocLine, ingest
from omai.ledger import TRANSIT, Line, StockError, available, balance, company_total, record
from omai.models import (DispatchOrder, Exception_, ExpectedReceipt, Outbox, Task, User, now_utc)
from omai.sources import mark_success


def stock_tanita(s, u, qty=10, bin_="G01"):
    warehouse.receive(s, receipt_id=f"seed{bin_}{qty}", po_ref="SEED", sku="TANITA1", location="WH1", good_qty=qty,
                      actor=u["otabek"])
    warehouse.putaway(s, task_ref=f"seed{bin_}", sku="TANITA1", qty=qty, location="WH1", scanned_bin=bin_,
                      actor=u["otabek"])


# 1 ---------------------------------------------------------------------------------------------------------
def test_1_receive_10_of_12_and_quarantine_damaged(s, u):
    s.add(ExpectedReceipt(po_ref="PO100", sku="TANITA1", location_code="WH1", qty_expected=12))
    s.flush()
    r = warehouse.receive(s, receipt_id="R1", po_ref="PO100", sku="TANITA1", location="WH1", good_qty=8,
                          damaged_qty=2, actor=u["otabek"])
    assert r["received"] == 10 and r["outstanding"] == 2
    assert balance(s, sku="TANITA1", loc="WH1") == 10            # physically received
    assert balance(s, sku="TANITA1", loc="WH1", cond="quarantine") == 2
    assert available(s, "TANITA1", "WH1") == 8                  # damaged are not available
    exp = s.scalar(select(ExpectedReceipt))
    assert exp.status == "partial" and not exp.erp_posted       # arrival ≠ ERP posting
    kinds = {e.kind for e in s.scalars(select(Exception_))}
    assert {"short_receipt", "damaged_receipt"} <= kinds
    # the same scan sent twice changes nothing
    again = warehouse.receive(s, receipt_id="R1", po_ref="PO100", sku="TANITA1", location="WH1", good_qty=8,
                              damaged_qty=2, actor=u["otabek"])
    assert again["created"] is False and balance(s, sku="TANITA1", loc="WH1") == 10


# 2 ---------------------------------------------------------------------------------------------------------
def test_2_transfer_5_units_moves_once(s, u):
    stock_tanita(s, u, 10)
    t = warehouse.create_transfer(s, from_loc="WH1", to_loc="ST1", lines=[{"sku": "TANITA1", "qty": 5}],
                                  assignee_id="otabek")
    warehouse.dispatch_transfer(s, transfer_id=t.id, scans=[{"sku": "TANITA1", "qty": 5, "bin": "G01"}],
                                actor=u["otabek"])
    assert balance(s, sku="TANITA1", loc="WH1") == 5
    assert balance(s, sku="TANITA1", loc=TRANSIT) == 5
    assert balance(s, sku="TANITA1", loc="ST1") == 0              # not before receipt
    assert available(s, "TANITA1", "ST1") == 0
    assert company_total(s, "TANITA1") == 10
    # a repeated dispatch does nothing
    warehouse.dispatch_transfer(s, transfer_id=t.id, scans=[{"sku": "TANITA1", "qty": 5, "bin": "G01"}],
                                actor=u["otabek"])
    assert balance(s, sku="TANITA1", loc="WH1") == 5
    r = warehouse.receive_transfer(s, transfer_id=t.id, scans=[{"sku": "TANITA1", "qty": 5}], actor=u["store"])
    assert r["status"] == "received"
    assert balance(s, sku="TANITA1", loc=TRANSIT) == 0 and balance(s, sku="TANITA1", loc="ST1") == 5
    assert company_total(s, "TANITA1") == 10


def test_2b_partial_transfer_receipt_flags_the_rest(s, u):
    stock_tanita(s, u, 10)
    t = warehouse.create_transfer(s, from_loc="WH1", to_loc="ST1", lines=[{"sku": "TANITA1", "qty": 5}],
                                  assignee_id="otabek")
    warehouse.dispatch_transfer(s, transfer_id=t.id, scans=[{"sku": "TANITA1", "qty": 5, "bin": "G01"}],
                                actor=u["otabek"])
    r = warehouse.receive_transfer(s, transfer_id=t.id, scans=[{"sku": "TANITA1", "qty": 3}], actor=u["store"])
    assert r["status"] == "partial" and r["in_transit"] == 2
    assert s.scalar(select(Exception_).where(Exception_.kind == "transfer_incomplete"))


# 3 ---------------------------------------------------------------------------------------------------------
def test_3_same_event_twice_from_each_system_counts_once(s, u):
    from omai.models import Location
    s.get(Location, "WH1").movement_source = "sap"
    day = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
    sap_doc = Doc(system="sap", doc_type="receipt", doc_id="PurchaseDeliveryNotes:55", event_time=day,
                  lines=[DocLine(sku="TAN1", qty=4, loc="01")])
    for _ in range(2):                 # repeated sync / failed-then-retried
        ingest(s, sap_doc)
    assert balance(s, sku="TANITA1", loc="WH1") == 4

    billz_doc = Doc(system="billz", doc_type="sale", doc_id="o9:sale", shared_ref="billz:o9", event_time=day,
                    lines=[DocLine(sku="tan1", qty=1, loc="shop-2")])
    ingest(s, Doc(system="billz", doc_type="receipt", doc_id="seed", event_time=day - timedelta(days=1),
                  lines=[DocLine(sku="tan1", qty=3, loc="shop-2")]))
    for _ in range(2):
        ingest(s, billz_doc)
    assert balance(s, sku="TANITA1", loc="ST2") == 2
    # the same sale arriving from SAP for a Billz-authoritative store is compared, not booked
    r = ingest(s, Doc(system="sap", doc_type="delivery", doc_id="DeliveryNotes:7", event_time=day,
                      lines=[DocLine(sku="TAN1", qty=1, loc="G05")]))
    assert r["compared_only"] == 1 and balance(s, sku="TANITA1", loc="ST2") == 2
    # 1C posting of the same receipt is never booked
    r = ingest(s, Doc(system="1c", doc_type="receipt", doc_id="x", event_time=day,
                      lines=[DocLine(sku="TANITA1", qty=4, loc="WH1")]))
    assert r["compared_only"] == 1 and balance(s, sku="TANITA1", loc="WH1") == 4
    # a mirrored event with a shared reference is linked, not doubled
    a = record(s, idem_key="sap:x:1", source="sap", kind="receipt", shared_ref="order:77",
               lines=[Line(sku="TANITA1", qty=1, to_loc="WH1", to_cond="saleable")], strict=False)
    b = record(s, idem_key="billz:y:1", source="billz", kind="receipt", shared_ref="order:77",
               lines=[Line(sku="TANITA1", qty=1, to_loc="WH1", to_cond="saleable")], strict=False)
    assert a.created and not b.created and b.mirrored_of == a.movement.id
    assert balance(s, sku="TANITA1", loc="WH1") == 5


# 4 ---------------------------------------------------------------------------------------------------------
def _count(s, u, qty_first, qty_recount=None):
    stock_tanita(s, u, 10)
    c = counting.create(s, location="WH1", counter=u["otabek"], checker=u["chk"],
                        deadline=now_utc() + timedelta(hours=2), skus=["TANITA1"], bins=["G01"])
    counting.start(s, c)
    form = counting.blind_form(c)
    assert all("book" not in f and "expected" not in f for f in form)
    line = next(f for f in form if f["sku"] == "TANITA1")
    counting.submit(s, c, u["otabek"], [{"line_id": line["line_id"], "qty": qty_first}])
    return c, line["line_id"]


def test_4_book_10_counted_8_needs_recount_and_no_silent_adjustment(s, u):
    c, lid = _count(s, u, 8)
    r = counting.evaluate(s, c)
    assert r["recount"] == 1 and c.status == "recount"
    assert balance(s, sku="TANITA1", loc="WH1") == 10            # nothing posted
    rt = s.scalar(select(Task).where(Task.kind == "recount"))
    assert rt.assignee_id == "chk"                                # independent person
    with pytest.raises(StockError):
        counting.submit(s, c, u["otabek"], [{"line_id": lid, "qty": 8}])  # first counter can't recount
    counting.submit(s, c, u["chk"], [{"line_id": lid, "qty": 8}])
    counting.evaluate(s, c)
    line = next(l for l in c.lines if l.id == lid)
    assert line.status == "investigating" and [r.qty for r in line.results] == [8, 8]  # both kept
    assert balance(s, sku="TANITA1", loc="WH1") == 10
    with pytest.raises(StockError):
        counting.close(s, c, u["mgr"])                          # can't close an unresolved variance
    with pytest.raises(StockError):
        counting.request_adjustment(s, c, lid, u["chk"])         # cause first
    counting.record_cause(s, c, lid, "two units sold without scan on 19.11", u["otabek"], u["chk"])
    ap = counting.request_adjustment(s, c, lid, u["chk"])
    with pytest.raises(StockError):
        approvals.decide(s, ap, u["chk"], True)                  # no self-approval
    with pytest.raises(StockError):
        approvals.decide(s, ap, u["otabek"], True)               # staff can't approve
    with pytest.raises(StockError):
        approvals.decide(s, ap, u["mgr"], True)                  # 6M above the manager's limit
    approvals.decide(s, ap, u["acc"], True)
    counting.apply_adjustment(s, c, ap, u["acc"])
    assert balance(s, sku="TANITA1", loc="WH1") == 8
    counting.close(s, c, u["mgr"])
    assert c.status == "closed"


def test_4b_disagreeing_recount_is_not_averaged(s, u):
    c, lid = _count(s, u, 8)
    counting.evaluate(s, c)
    counting.submit(s, c, u["chk"], [{"line_id": lid, "qty": 9}])
    counting.evaluate(s, c)
    line = next(l for l in c.lines if l.id == lid)
    assert line.status == "recount" and sorted(r.qty for r in line.results) == [8, 9]


def test_4c_approval_is_bound_to_the_exact_action(s, u):
    c, lid = _count(s, u, 8)
    counting.evaluate(s, c)
    counting.submit(s, c, u["chk"], [{"line_id": lid, "qty": 8}])
    counting.evaluate(s, c)
    counting.record_cause(s, c, lid, "theft suspected — investigate", None, u["chk"])
    ap = counting.request_adjustment(s, c, lid, u["chk"])
    approvals.decide(s, ap, u["own"], True)
    tampered = {**ap.payload, "diff": -5}
    with pytest.raises(StockError):
        approvals.execute(s, ap, u["own"], tampered)


# 5 ---------------------------------------------------------------------------------------------------------
def test_5_sale_during_count_and_backdated_posting_reconcile_to_cutoff(s, u):
    stock_tanita(s, u, 10)
    c = counting.create(s, location="WH1", counter=u["otabek"], checker=u["chk"],
                        deadline=now_utc() + timedelta(hours=2), skus=["TANITA1"], bins=["G01"])
    cutoff = now_utc()
    counting.start(s, c, cutoff)
    # a sale scanned after the cutoff while counting (the counter still saw all 10)
    record(s, idem_key="omai:sale:during", source="omai", kind="sale", event_time=cutoff + timedelta(minutes=10),
           lines=[Line(sku="TANITA1", qty=1, from_loc="WH1", from_bin="G01", from_cond="saleable")])
    # a backdated posting arriving now for a sale yesterday (before the cutoff); the counter saw 10 - 1 = 9
    record(s, idem_key="sap:late:1", source="sap", kind="sale", event_time=cutoff - timedelta(days=1),
           lines=[Line(sku="TANITA1", qty=1, from_loc="WH1", from_bin="G01", from_cond="saleable")], strict=False)
    lid = next(l.id for l in c.lines if l.sku == "TANITA1")
    counting.submit(s, c, u["otabek"], [{"line_id": lid, "qty": 9}])
    r = counting.evaluate(s, c)
    assert r["matched"] == 1 and r["recount"] == 0
    line = next(l for l in c.lines if l.id == lid)
    assert line.book_qty == 9                       # ledger at cutoff, not now (8)
    assert balance(s, sku="TANITA1", loc="WH1") == 8
    assert not s.scalar(select(Exception_).where(Exception_.kind == "count_variance"))


# 6 ---------------------------------------------------------------------------------------------------------
def _serial_stock(s, u):
    warehouse.receive(s, receipt_id="RS", po_ref="PO-F", sku="FENIX8", location="WH1", good_qty=2,
                      serials=["SN1", "SN2"], actor=u["otabek"])
    warehouse.putaway(s, task_ref="pf", sku="FENIX8", qty=2, location="WH1", scanned_bin="G01", actor=u["otabek"])


def test_6_blocks_wrong_serial_missing_release_and_unavailable_stock(s, u):
    _serial_stock(s, u)
    o = DispatchOrder(order_ref="SO1", location_code="WH1", lines=[{"sku": "FENIX8", "qty": 1}])
    s.add(o)
    s.flush()
    warehouse.allocate(s, o)
    with pytest.raises(StockError, match="wrong serial"):
        warehouse.pick(s, order_ref="SO1", scans=[{"sku": "FENIX8", "serial": "SN9", "bin": "G01"}], actor=u["otabek"])
    warehouse.pick(s, order_ref="SO1", scans=[{"sku": "FENIX8", "serial": "SN1", "bin": "G01"}], actor=u["otabek"])
    with pytest.raises(StockError, match="release not authorised"):
        warehouse.dispatch(s, order_ref="SO1", actor=u["otabek"], handover={"received_by": "Courier"})
    assert s.scalar(select(Task).where(Task.kind == "release_check")).urgent
    assert balance(s, sku="FENIX8", loc="WH1") == 2
    warehouse.set_release(s, "SO1", True, "CASH:director")
    warehouse.dispatch(s, order_ref="SO1", actor=u["otabek"], handover={"received_by": "Courier"})
    assert balance(s, sku="FENIX8", loc="WH1") == 1

    # unavailable: the last unit is reserved for another order
    o2 = DispatchOrder(order_ref="SO2", location_code="WH1", lines=[{"sku": "FENIX8", "qty": 1}])
    o3 = DispatchOrder(order_ref="SO3", location_code="WH1", lines=[{"sku": "FENIX8", "qty": 1}])
    s.add_all([o2, o3])
    s.flush()
    warehouse.allocate(s, o2)
    with pytest.raises(StockError, match="available"):
        warehouse.pick(s, order_ref="SO3", scans=[{"sku": "FENIX8", "serial": "SN2", "bin": "G01"}], actor=u["otabek"])
    # cancelled allocation frees it
    warehouse.cancel_allocation(s, "SO2")
    assert available(s, "FENIX8", "WH1") == 1
    warehouse.pick(s, order_ref="SO3", scans=[{"sku": "FENIX8", "serial": "SN2", "bin": "G01"}], actor=u["otabek"])


def test_6b_returns_quarantine_until_approved(s, u):
    _serial_stock(s, u)
    o = DispatchOrder(order_ref="SO1", location_code="WH1", lines=[{"sku": "FENIX8", "qty": 1}],
                      release_status="approved")
    s.add(o)
    s.flush()
    warehouse.pick(s, order_ref="SO1", scans=[{"sku": "FENIX8", "serial": "SN1", "bin": "G01"}], actor=u["otabek"])
    warehouse.dispatch(s, order_ref="SO1", actor=u["otabek"], handover={"received_by": "Client"})
    with pytest.raises(StockError):
        warehouse.open_return(s, original_doc="SO1", sku="FENIX8", qty=1, location="WH1", serial="SN2",
                              actor=u["otabek"])     # SN2 was not on SO1
    rc = warehouse.open_return(s, original_doc="SO1", sku="FENIX8", qty=1, location="WH1", serial="SN1",
                               actor=u["otabek"])
    assert available(s, "FENIX8", "WH1") == 1        # only SN2; the return is quarantined
    with pytest.raises(StockError):
        warehouse.decide_return(s, return_id=rc.id, accept=True, approver=u["otabek"])
    warehouse.decide_return(s, return_id=rc.id, accept=True, approver=u["mgr"])
    assert available(s, "FENIX8", "WH1") == 2


def test_6c_zero_versus_blank(s, u):
    stock_tanita(s, u, 10)
    c = counting.create(s, location="WH1", counter=u["otabek"], checker=u["chk"],
                        deadline=now_utc() + timedelta(hours=2), skus=["TANITA1"], bins=["G01", "G02"])
    counting.start(s, c)
    lines = {(l.bin, l.sku): l.id for l in c.lines}
    counting.submit(s, c, u["otabek"], [{"line_id": lines[("G01", "TANITA1")], "qty": 10},
                                        {"line_id": lines[("G02", "*")], "qty": None}])
    r = counting.evaluate(s, c)
    assert r["blank"] == 1                           # blank = not counted, count stays open
    with pytest.raises(StockError):
        counting.close(s, c, u["mgr"])
    counting.submit(s, c, u["otabek"], [{"line_id": lines[("G02", "*")], "qty": 0}])
    r = counting.evaluate(s, c)
    assert r["blank"] == 0 and r["matched"] == 2     # explicit 0 = counted empty
    counting.close(s, c, u["mgr"])


# 7 ---------------------------------------------------------------------------------------------------------
def test_7_missed_task_reminds_escalates_and_accepted_evidence_stops(s, u):
    base = now_utc()
    t = tasks.create(s, kind="count", title="Count bin G01", deadline=base + timedelta(minutes=20),
                     assignee_id="otabek", location="WH1")
    st = tasks.reminder_tick(s, base)
    assert st["before"] == 1
    st = tasks.reminder_tick(s, base + timedelta(minutes=25))
    assert st["after"] == 1
    st = tasks.reminder_tick(s, base + timedelta(minutes=90))
    assert st["escalated"] == 1
    assert s.scalar(select(Outbox).where(Outbox.dedupe_key == f"task:{t.id}:escalate")).body["user_id"] == "mgr"
    n = len(s.scalars(select(Outbox)).all())
    tasks.reminder_tick(s, base + timedelta(minutes=120))
    assert len(s.scalars(select(Outbox)).all()) == n  # one of each, no spam

    t2 = tasks.create(s, kind="count", title="Count bin G02", deadline=base + timedelta(minutes=20),
                      assignee_id="otabek", location="WH1")
    with pytest.raises(tasks.TaskError):
        tasks.act(s, t2, "submit", u["otabek"])             # evidence required
    tasks.act(s, t2, "submit", u["otabek"], evidence={"scan": "G02"})
    with pytest.raises(tasks.TaskError):
        tasks.act(s, t2, "accept", u["otabek"])             # can't accept own work
    tasks.act(s, t2, "accept", u["mgr"])
    st = tasks.reminder_tick(s, base + timedelta(minutes=200))
    assert not s.scalar(select(Outbox).where(Outbox.dedupe_key == f"task:{t2.id}:after"))


def test_7b_shift_boundaries(s, u):
    from omai.config import settings
    o = u["otabek"]
    o.shift_start, o.shift_end, o.workdays = time(9, 0), time(18, 0), "7"
    day = datetime(2026, 11, 24, tzinfo=settings.tz)
    deadline = day.replace(hour=19, minute=10)
    t = tasks.create(s, kind="pick", title="Pick SO9", deadline=deadline, assignee_id="otabek")
    st = tasks.reminder_tick(s, day.replace(hour=18, minute=50))      # after shift: deferred
    assert st["before"] == 0 and st["deferred"] >= 1
    st = tasks.reminder_tick(s, day.replace(hour=9, minute=5) + timedelta(days=1))
    assert st["after"] == 1                                            # next morning, inside the shift
    assert t.reminded_after


# 8 ---------------------------------------------------------------------------------------------------------
def test_8_outage_shows_stale_and_blocks_release_with_manual_fallback(s, u):
    from omai import routine
    _serial_stock(s, u)
    mark_success(s, "sap", at=now_utc() - timedelta(hours=5))
    o = DispatchOrder(order_ref="SO1", location_code="WH1", lines=[{"sku": "FENIX8", "qty": 1}],
                      release_status="approved")
    s.add(o)
    s.flush()
    warehouse.pick(s, order_ref="SO1", scans=[{"sku": "FENIX8", "serial": "SN1", "bin": "G01"}], actor=u["otabek"])
    with pytest.raises(StockError, match="stale"):
        warehouse.dispatch(s, order_ref="SO1", actor=u["otabek"], handover={"received_by": "Courier"})
    # recovery task for IT
    s.add(User(id="it", name="IT", role="it"))
    s.flush()
    routine.every_15(s)
    assert s.scalar(select(Task).where(Task.ref == "recover:sap"))
    assert "stale" in routine.digest(s).lower()
    # controlled manual fallback: a manager, not the picker
    with pytest.raises(StockError):
        warehouse.dispatch(s, order_ref="SO1", actor=u["otabek"], handover={"received_by": "C"},
                           manual_override_by=u["otabek"])
    warehouse.dispatch(s, order_ref="SO1", actor=u["otabek"], handover={"received_by": "C"},
                       manual_override_by=u["mgr"])
    assert o.status == "dispatched"


def test_8b_costs_hidden_and_no_self_approval(s, u):
    from omai import insights
    stock_tanita(s, u, 3)
    staff_rows = insights.stock_rows(s, u["otabek"])
    assert staff_rows and all("unit_cost" not in r and "value" not in r for r in staff_rows)
    assert "aged_value" not in insights.measures(s, u["otabek"])
    assert "value" in insights.stock_rows(s, u["mgr"])[0]
    ap = approvals.request(s, action="adjustment", payload={"sku": "TANITA1", "loc": "WH1", "diff": 1}, value=1,
                           reason="found", requester=u["mgr"])
    with pytest.raises(StockError):
        approvals.decide(s, ap, u["mgr"], True)


def test_8c_scheduled_slot_runs_once_after_restart(tmp_path):
    from sqlalchemy.orm import sessionmaker

    from omai import routine
    from omai.db import Base, make_engine
    eng = make_engine(f"sqlite:///{tmp_path}/r.db")
    Base.metadata.create_all(eng)
    f = sessionmaker(bind=eng, expire_on_commit=False)
    calls = []
    slot = now_utc()
    assert routine.run_slot("x", slot, lambda s: calls.append(1), f) == "done"
    assert routine.run_slot("x", slot, lambda s: calls.append(1), f) == "skipped"
    assert len(calls) == 1

    def boom(s):
        raise RuntimeError("down")
    assert routine.run_slot("y", slot, boom, f) == "failed"
    assert routine.run_slot("y", slot, lambda s: calls.append(2), f) == "done"   # retried


# extra rules --------------------------------------------------------------------------------------------------
def test_putaway_rules_and_fefo(s, u):
    assert warehouse.suggest_bin(s, "DET5", "WH1") == "C01"
    assert warehouse.suggest_bin(s, "PRIMUS", "WH1") == "F01"
    warehouse.receive(s, receipt_id="D1", po_ref="P", sku="DET5", location="WH1", good_qty=5, batch="B-late",
                      expiry=now_utc() + timedelta(days=200), actor=u["otabek"])
    warehouse.receive(s, receipt_id="D2", po_ref="P", sku="DET5", location="WH1", good_qty=5, batch="B-early",
                      expiry=now_utc() + timedelta(days=20), actor=u["otabek"])
    with pytest.raises(StockError):
        warehouse.putaway(s, task_ref="x", sku="DET5", qty=5, location="WH1", scanned_bin="G01", actor=u["otabek"])
    plan = warehouse.pick_plan(s, "DET5", "WH1", 6)
    assert plan[0]["lot"] == "B-early" and plan[0]["qty"] == 5


def test_freeze_blocks_movements_during_count(s, u):
    stock_tanita(s, u, 10)
    c = counting.create(s, location="WH1", counter=u["otabek"], checker=u["chk"],
                        deadline=now_utc() + timedelta(hours=1), skus=["TANITA1"], bins=["G01"], freeze=True)
    counting.start(s, c)
    with pytest.raises(StockError, match="frozen"):
        warehouse.change_condition(s, sku="TANITA1", qty=1, location="WH1", bin_="G01", to="damaged",
                                   actor=u["otabek"], ref="dmg1")
