"""Receiving, put-away, picking, dispatch, transfers, returns, technicians — the physical flows.

OMAI plans and checks; people scan. Every function here takes what staff actually scanned and either
records it as a movement or refuses it with a reason they can act on.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from omai import tasks
from omai.ledger import (TRANSIT, Line, StockError, allocated, audit, balance, positions, raise_exception, record,
                         resolve_exception)
from omai.models import (Allocation, Bin, Condition, DispatchOrder, ExpectedReceipt, Item, Location, Movement,
                         MovementLine, Ownership, ReturnCase, Role, Task, Transfer, User, now_utc)
from omai.sources import stale_critical

DOCK = "DOCK"     # receiving area bin, before put-away
PACK = "PACK"     # picked and waiting for handover
S, D, Q, E = (Condition.saleable.value, Condition.damaged.value, Condition.quarantine.value, Condition.expired.value)


def _item(s: Session, sku: str) -> Item:
    item = s.get(Item, sku)
    if item is None:
        raise StockError(f"номаълум артикул {sku}")
    return item


def serial_location(s: Session, sku: str, serial: str) -> list[dict]:
    return [p for p in positions(s, sku=sku) if p["lot"] == serial and p["qty"] > 0]


# ----------------------------------------------------------------- receiving
def receive(s: Session, *, receipt_id: str, po_ref: str, sku: str, location: str, good_qty: float,
            damaged_qty: float = 0, serials: list[str] | None = None, batch: str = "",
            expiry: datetime | None = None, actor: User, ownership: str = Ownership.own.value,
            event_time: datetime | None = None) -> dict:
    """Acknowledge what physically arrived. Good units → DOCK (saleable, awaiting put-away); damaged → quarantine.

    The expected PO quantity is never booked; only scans are. Shortages and overages become exceptions.
    """
    item = _item(s, sku)
    serials = serials or []
    total = good_qty + damaged_qty
    if total <= 0:
        raise StockError("ҳеч нарса қабул қилинмади")
    if item.serial_tracked:
        if len(serials) != total or len(set(serials)) != len(serials):
            raise StockError(f"{sku} серия рақами билан юритилади: {total:g} та турли серия рақамини сканерланг ({len(serials)} та келди)")
        for sn in serials:
            if serial_location(s, sku, sn):
                raise StockError(f"{sn} серия рақами аллақачон қолдиқда бор — ёрлиқни текширинг")
    if item.batch_tracked and not batch:
        raise StockError(f"{sku} учун партия рақами керак")
    if item.expiry_tracked and expiry is None:
        raise StockError(f"{sku} учун яроқлилик муддати керак")

    lines: list[Line] = []
    if item.serial_tracked:
        for i, sn in enumerate(serials):
            cond = S if i < good_qty else Q
            lines.append(Line(sku=sku, qty=1, to_loc=location, to_bin=DOCK, to_cond=cond, lot=sn,
                              ownership=ownership))
    else:
        if good_qty:
            lines.append(Line(sku=sku, qty=good_qty, to_loc=location, to_bin=DOCK, to_cond=S, lot=batch,
                              ownership=ownership, expiry=expiry))
        if damaged_qty:
            lines.append(Line(sku=sku, qty=damaged_qty, to_loc=location, to_bin=DOCK, to_cond=Q, lot=batch,
                              ownership=ownership, expiry=expiry))
    res = record(s, idem_key=f"omai:receipt:{receipt_id}:{sku}", source="omai", kind="receipt", lines=lines,
                 doc_ref=po_ref, actor=actor.id, event_time=event_time)
    exp = s.scalar(select(ExpectedReceipt).where(ExpectedReceipt.po_ref == po_ref, ExpectedReceipt.sku == sku))
    outstanding = None
    if res.created and exp is not None:
        exp.qty_received += total
        exp.qty_damaged += damaged_qty
        outstanding = max(0.0, exp.qty_expected - exp.qty_received)
        exp.status = "complete" if outstanding == 0 else "partial"
        if outstanding:
            raise_exception(s, "short_receipt", f"{po_ref}:{sku}",
                            f"{po_ref} {sku}: {exp.qty_expected:g} тадан {exp.qty_received:g} таси қабул қилинди, "
                            f"{outstanding:g} таси келмади", severity="medium")
        if exp.qty_received > exp.qty_expected:
            raise_exception(s, "over_receipt", f"{po_ref}:{sku}",
                            f"{po_ref} {sku}: {exp.qty_expected:g} та буюртма қилинган, {exp.qty_received:g} та келди")
    elif exp is not None:
        outstanding = max(0.0, exp.qty_expected - exp.qty_received)
    if res.created and damaged_qty:
        raise_exception(s, "damaged_receipt", f"{po_ref}:{sku}",
                        f"{po_ref}: {damaged_qty:g} × {sku} шикастланган келди — карантинга қўйилди", severity="medium")
    if res.created and good_qty:
        target = suggest_bin(s, sku, location)
        tasks.create(s, kind="putaway", title=f"{good_qty:g} × {sku} ни қабул жойидан {target or 'ячейка'}га жойлаштириш",
                     deadline=now_utc() + timedelta(hours=2), assignee_id=actor.id, location=location,
                     ref=f"putaway:{receipt_id}:{sku}", result_required="Товар ва ячейкани сканерланг.", notify=False)
    return {"movement": res.movement.id, "created": res.created, "received": total, "good": good_qty,
            "quarantined": damaged_qty, "outstanding": outstanding,
            "erp_posted": exp.erp_posted if exp else None}


# ----------------------------------------------------------------- slotting
def suggest_bin(s: Session, sku: str, location: str) -> str | None:
    """Bin by size, load, access, compatibility and demand. Same-SKU bins first (keeps FIFO in one place)."""
    item = _item(s, sku)
    order = ["S", "M", "L", "XL"]
    bins = s.scalars(select(Bin).where(Bin.location_code == location, Bin.active.is_(True))).all()
    ok = []
    for b in bins:
        if order.index(b.size_class) < order.index(item.size_class):
            continue
        if b.max_load_kg < item.weight_kg:
            continue
        if item.heavy_handling and not b.floor_level:
            continue
        if item.hazardous != b.chemicals_ok:   # chemicals only in chemical bins, and nothing else there
            continue
        ok.append(b)
    if not ok:
        return None
    holding = {p["bin"] for p in positions(s, sku=sku, loc=location)}
    demand_rank = {"A": 0, "B": 1, "C": 2}[item.abc_group or "C"]
    ok.sort(key=lambda b: (b.code not in holding, abs(b.pick_rank // 34 - demand_rank), b.pick_rank, b.code))
    return ok[0].code


def putaway(s: Session, *, task_ref: str, sku: str, qty: float, location: str, scanned_bin: str, actor: User,
            lot: str = "", from_bin: str = DOCK) -> dict:
    item = _item(s, sku)
    b = s.scalar(select(Bin).where(Bin.location_code == location, Bin.code == scanned_bin, Bin.active.is_(True)))
    if b is None:
        raise StockError(f"{location} да {scanned_bin} ячейкаси йўқ")
    if item.hazardous and not b.chemicals_ok:
        raise StockError(f"{sku} — кимёвий модда: {scanned_bin} ячейкаси унга рухсат этилмаган")
    if item.heavy_handling and not b.floor_level:
        raise StockError(f"{sku} оғир: фақат пол сатҳидаги ячейкалар")
    src = [p for p in positions(s, sku=sku, loc=location, cond=S) if p["bin"] == from_bin and (not lot or p["lot"] == lot)]
    lines, left = [], qty
    for p in src:
        take = min(left, p["qty"])
        if take <= 0:
            continue
        lines.append(Line(sku=sku, qty=take, from_loc=location, from_bin=from_bin, from_cond=S, to_loc=location,
                          to_bin=scanned_bin, to_cond=S, lot=p["lot"], ownership=p["ownership"],
                          expiry=p["expiry"], received_at=p["received_at"]))
        left -= take
    if left > 1e-9:
        raise StockError(f"{from_bin} да бор-йўғи {qty - left:g} × {sku} кутмоқда")
    res = record(s, idem_key=f"omai:putaway:{task_ref}:{scanned_bin}", source="omai", kind="putaway", lines=lines,
                 actor=actor.id)
    for t in tasks.open_for_ref(s, task_ref, "putaway"):
        t.state, t.accepted_by = "accepted", None  # scan of item + bin is the evidence (approved auto-accept rule)
        t.evidence = [*t.evidence, {"bin": scanned_bin, "qty": qty, "by": actor.id, "auto": True}]
    return {"movement": res.movement.id, "created": res.created}


# ----------------------------------------------------------------- shelving (after the pilot's baseline count)
UNSHELVED = ""    # the pilot's opening stock arrives without a shelf


def _count_open(s: Session, location: str) -> bool:
    from omai.models import Count
    return s.scalar(select(Count.id).where(Count.location_code == location,
                                           Count.status.in_(["open", "counting"]))) is not None


def unshelved(s: Session, location: str) -> list[dict]:
    """Items still without a shelf at ``location`` — codes and names only, never quantities."""
    skus = sorted({p["sku"] for p in positions(s, loc=location, cond=S) if p["bin"] == UNSHELVED and p["qty"] > 0})
    names = {i.sku: i.name for i in s.scalars(select(Item).where(Item.sku.in_(skus)))} if skus else {}
    return [{"sku": k, "name": names.get(k, "")} for k in skus]


def shelve(s: Session, *, location: str, sku: str, qty: float, scanned_bin: str, actor: User, scan_id: str,
           lot: str = "") -> dict:
    """One scan: ``qty`` × ``sku`` from "no shelf" onto ``scanned_bin``. ``scan_id`` (from the page) makes a resent
    scan count once while a second real scan of the same item to the same shelf still counts."""
    if _count_open(s, location):
        raise StockError(f"{location} да санаш очиқ: жойлаш санаш ёпилгандан кейин")
    if not scan_id or len(scan_id) > 64:
        raise StockError("сканер рақами йўқ")
    done = s.scalar(select(Movement).where(
        Movement.idem_key == f"omai:putaway:shelve:{location}:{scan_id}:{scanned_bin}"))
    if done is not None:                     # the page resent this scan: it already counted
        return {"movement": done.id, "created": False, "items_left": len(unshelved(s, location))}
    try:
        res = putaway(s, task_ref=f"shelve:{location}:{scan_id}", sku=sku, qty=qty, location=location,
                      scanned_bin=scanned_bin, actor=actor, lot=lot, from_bin=UNSHELVED)
    except StockError as exc:
        if "кутмоқда" in str(exc):   # putaway's message carries the book quantity: not shown here
            raise StockError(f"Ячейкасиз {sku} сиз ёзгандан кам. Миқдорни текширинг ёки менежерга айтинг.") from None
        raise
    left = unshelved(s, location)
    if not left:
        for t in tasks.open_for_ref(s, f"shelve:{location}", "shelving"):
            t.state, t.accepted_by = "accepted", None   # nothing left without a shelf: the scans are the evidence
            t.evidence = [*t.evidence, {"done": True, "by": actor.id, "auto": True}]
    return {**res, "items_left": len(left)}


def shelving_task(s: Session, location: str, assignee: User, deadline: datetime) -> Task:
    if _count_open(s, location):
        raise StockError(f"{location} да санаш очиқ: аввал уни ёпинг")
    if not s.scalar(select(Bin.id).where(Bin.location_code == location, Bin.active.is_(True))):
        raise StockError(f"{location} да ячейка йўқ: аввал ячейкаларни қўшинг")
    if tasks.open_for_ref(s, f"shelve:{location}", "shelving"):
        raise StockError(f"{location} учун жойлаш топшириғи аллақачон бор")
    n = len(unshelved(s, location))
    if not n:
        raise StockError(f"{location} да ячейкасиз товар йўқ")
    return tasks.create(s, kind="shelving", title=f"{location}: товарларни ячейкаларга жойлаш ({n} та товар)",
                        deadline=deadline, assignee_id=assignee.id, location=location, ref=f"shelve:{location}",
                        result_required="Товарни ва ячейкани сканерланг, қўйган миқдорингизни ёзинг. Ҳамма товар "
                                        "жойлашгач, топшириқ ўзи ёпилади.")


# ----------------------------------------------------------------- allocation, picking, dispatch
def allocate(s: Session, order: DispatchOrder) -> list[Allocation]:
    out = []
    for ln in order.lines:
        free = balance(s, sku=ln["sku"], loc=order.location_code, cond=S, ownership=Ownership.own.value) \
            - allocated(s, ln["sku"], order.location_code)
        if free + 1e-9 < ln["qty"]:
            raise_exception(s, "allocation_short", f"{order.order_ref}:{ln['sku']}",
                            f"{order.order_ref}: {ln['qty']:g} × {ln['sku']} буюртма қилинган, бўш қолдиқ {max(free, 0):g}")
            continue
        a = Allocation(sku=ln["sku"], location_code=order.location_code, qty=ln["qty"], order_ref=order.order_ref)
        s.add(a)
        out.append(a)
    s.flush()
    return out


def cancel_allocation(s: Session, order_ref: str, actor: User | None = None) -> int:
    rows = s.scalars(select(Allocation).where(Allocation.order_ref == order_ref, Allocation.status == "active")).all()
    for a in rows:
        a.status = "cancelled"
    audit(s, actor.id if actor else None, "allocation.cancel", order_ref, n=len(rows))
    return len(rows)


def pick_plan(s: Session, sku: str, location: str, qty: float) -> list[dict]:
    """FEFO for dated stock, else FIFO by receipt. Damaged/expired/quarantined are never offered."""
    plan, left = [], qty
    for p in positions(s, sku=sku, loc=location, cond=S):
        if p["bin"] in (PACK,) or p["ownership"] != Ownership.own.value or left <= 0:
            continue
        exp = p["expiry"]
        if exp is not None and (exp if exp.tzinfo else exp.replace(tzinfo=now_utc().tzinfo)) < now_utc():
            continue
        take = min(left, p["qty"])
        plan.append({"bin": p["bin"], "lot": p["lot"], "qty": take})
        left -= take
    return plan


def set_release(s: Session, order_ref: str, approved: bool, by: str) -> DispatchOrder:
    """Release authorisation from CASH or the designated approver."""
    order = s.scalar(select(DispatchOrder).where(DispatchOrder.order_ref == order_ref))
    if order is None:
        raise StockError(f"номаълум буюртма {order_ref}")
    order.release_status = "approved" if approved else "refused"
    order.release_by, order.release_at = by, now_utc()
    audit(s, None, "release.set", order_ref, approved=approved, by=by)
    return order


def pick(s: Session, *, order_ref: str, scans: list[dict], actor: User) -> dict:
    """``scans``: [{sku, qty, bin, serial?}] as scanned. Wrong serials and unavailable stock are refused."""
    order = s.scalar(select(DispatchOrder).where(DispatchOrder.order_ref == order_ref))
    if order is None:
        raise StockError(f"номаълум буюртма {order_ref}")
    if order.release_status == "refused":
        raise StockError(f"{order_ref}: чиқаришга рухсат берилмади — термасдан туринг")
    loc = order.location_code
    wanted = {ln["sku"]: ln for ln in order.lines}
    lines = []
    for sc in scans:
        sku, qty = sc["sku"], float(sc.get("qty", 1))
        if sku not in wanted:
            raise StockError(f"{sku} {order_ref} буюртмасида йўқ")
        item = _item(s, sku)
        lot = sc.get("serial") or sc.get("batch") or ""
        if item.serial_tracked:
            listed = wanted[sku].get("serials")
            if listed and lot not in listed:
                raise StockError(f"{lot} серия рақами {order_ref} буюртмасидагиси эмас")
            at = serial_location(s, sku, lot)
            if not at or at[0]["loc"] != loc or at[0]["cond"] != S or at[0]["bin"] != sc["bin"]:
                where = f"{at[0]['loc']}/{at[0]['bin']} ({at[0]['cond']})" if at else "nowhere in stock"
                raise StockError(f"{lot}: {sc['bin']} ячейкаси учун нотўғри серия рақами (wrong serial) — у {where} да")
        have = balance(s, sku=sku, loc=loc, bin_=sc["bin"], cond=S, lot=lot, ownership=Ownership.own.value)
        if have + 1e-9 < qty:
            raise StockError(f"{sku} {sc['bin']} да: сотувга {have:g}, сканерланди {qty:g}")
        own_alloc = sum(a.qty for a in s.scalars(select(Allocation).where(
            Allocation.order_ref == order_ref, Allocation.sku == sku, Allocation.status == "active")))
        free = balance(s, sku=sku, loc=loc, cond=S, ownership=Ownership.own.value) - allocated(s, sku, loc) + own_alloc
        if free + 1e-9 < qty:
            raise StockError(f"{sku}: бор-йўғи {max(free, 0):g} та бўш (available; қолгани банд қилинган)")
        lines.append(Line(sku=sku, qty=qty, from_loc=loc, from_bin=sc["bin"], from_cond=S, to_loc=loc, to_bin=PACK,
                          to_cond=S, lot=lot))
    picked_key = ",".join(sorted(f"{l.sku}:{l.from_bin}:{l.lot}:{l.qty:g}" for l in lines))
    res = record(s, idem_key=f"omai:pick:{order_ref}:{picked_key}", source="omai", kind="pick", lines=lines,
                 doc_ref=order_ref, actor=actor.id)
    packed = {ln["sku"]: balance(s, sku=ln["sku"], loc=loc, bin_=PACK, cond=S) for ln in order.lines}
    if all(packed[ln["sku"]] + 1e-9 >= ln["qty"] for ln in order.lines):
        order.status = "picked"
    return {"movement": res.movement.id, "created": res.created, "status": order.status}


def dispatch(s: Session, *, order_ref: str, actor: User, handover: dict, manual_override_by: User | None = None
             ) -> dict:
    """Digital release at handover. Blocked for: no authorisation, missing picks, conflicting stock, stale
    critical data (unless a manager uses the controlled manual fallback — logged)."""
    order = s.scalar(select(DispatchOrder).where(DispatchOrder.order_ref == order_ref))
    if order is None:
        raise StockError(f"номаълум буюртма {order_ref}")
    problems = []
    if order.release_status != "approved":
        problems.append("чиқаришга CASH / тасдиқловчи рухсат бермаган (release not authorised)")
    stale = stale_critical(s)
    if stale:
        if manual_override_by is not None and Role(manual_override_by.role) in (Role.manager, Role.owner) \
                and manual_override_by.id != actor.id:
            audit(s, manual_override_by.id, "dispatch.manual_fallback", order_ref, stale=stale)
        else:
            problems.append(f"{', '.join(stale)} маълумоти эскирган (stale) — қўлда чиқариш учун менежер керак")
    loc = order.location_code
    lines = []
    for ln in order.lines:
        packed = [p for p in positions(s, sku=ln["sku"], loc=loc, cond=S) if p["bin"] == PACK]
        if sum(p["qty"] for p in packed) + 1e-9 < ln["qty"]:
            problems.append(f"{ln['sku']}: {ln['qty']:g} та буюртма, {sum(p['qty'] for p in packed):g} та терилди")
            continue
        left = ln["qty"]
        for p in packed:
            take = min(left, p["qty"])
            if take > 0:
                lines.append(Line(sku=ln["sku"], qty=take, from_loc=loc, from_bin=PACK, from_cond=S, lot=p["lot"]))
                left -= take
    if not handover.get("received_by"):
        problems.append("топширилгани ҳақида далил йўқ (ким қабул қилди)")
    if problems:
        order.status = "blocked"
        exc = raise_exception(s, "release_blocked", order_ref, f"{order_ref} чиқарилмади: " + "; ".join(problems),
                              severity="high", problems=problems)
        if order.release_status != "approved":
            t = tasks.create(s, kind="release_check", title=f"{order_ref}: рухсатсиз чиқаришга уриниш",
                             deadline=now_utc() + timedelta(minutes=30), assignee_id=None, location=loc,
                             ref=order_ref, urgent=True)
            exc.data = {**exc.data, "task": t.id}
        raise StockError("; ".join(problems))
    res = record(s, idem_key=f"omai:dispatch:{order_ref}", source="omai", kind="dispatch", lines=lines,
                 doc_ref=order_ref, actor=actor.id, shared_ref=f"order:{order_ref}")
    for a in s.scalars(select(Allocation).where(Allocation.order_ref == order_ref, Allocation.status == "active")):
        a.status = "fulfilled"
    order.status, order.handover_evidence = "dispatched", {**handover, "by": actor.id, "at": now_utc().isoformat()}
    resolve_exception(s, "release_blocked", order_ref, "dispatched")
    return {"movement": res.movement.id, "created": res.created}


# ----------------------------------------------------------------- transfers
def create_transfer(s: Session, *, from_loc: str, to_loc: str, lines: list[dict], assignee_id: str | None,
                    due_at: datetime | None = None) -> Transfer:
    for code in (from_loc, to_loc):
        if s.get(Location, code) is None:
            raise StockError(f"номаълум жой {code}")
    t = Transfer(from_loc=from_loc, to_loc=to_loc, lines=lines, due_at=due_at or now_utc() + timedelta(days=1))
    s.add(t)
    s.flush()
    tasks.create(s, kind="transfer_out", title=f"Кўчириш {t.id[:8]}: териб {to_loc} га жўнатиш",
                 deadline=now_utc() + timedelta(hours=4), assignee_id=assignee_id, location=from_loc,
                 ref=f"transfer:{t.id}")
    return t


def dispatch_transfer(s: Session, *, transfer_id: str, scans: list[dict], actor: User) -> dict:
    """Source → transit, once. ``scans``: [{sku, qty, bin, serial?}]."""
    t = s.get(Transfer, transfer_id)
    if t is None:
        raise StockError("номаълум кўчириш")
    if t.status != "planned":
        existing = s.scalar(select(Movement).where(Movement.idem_key == f"omai:transfer_out:{transfer_id}"))
        return {"movement": existing.id if existing else None, "created": False, "status": t.status}
    lines = [Line(sku=sc["sku"], qty=float(sc.get("qty", 1)), from_loc=t.from_loc, from_bin=sc["bin"], from_cond=S,
                  to_loc=TRANSIT, to_bin=transfer_id, to_cond=S, lot=sc.get("serial") or sc.get("batch") or "")
             for sc in scans]
    res = record(s, idem_key=f"omai:transfer_out:{transfer_id}", source="omai", kind="transfer_out", lines=lines,
                 transfer_id=transfer_id, actor=actor.id, shared_ref=f"transfer:{transfer_id}:out")
    t.status, t.dispatched_at = "in_transit", now_utc()
    for task in tasks.open_for_ref(s, f"transfer:{transfer_id}", "transfer_out"):
        task.state = "accepted"
    dest_custodian = s.get(Location, t.to_loc).custodian_id
    tasks.create(s, kind="transfer_in", title=f"{t.from_loc} дан {transfer_id[:8]} кўчиришни қабул қилиш",
                 deadline=t.due_at, assignee_id=dest_custodian, location=t.to_loc, ref=f"transfer:{transfer_id}")
    return {"movement": res.movement.id, "created": res.created, "status": t.status}


def receive_transfer(s: Session, *, transfer_id: str, scans: list[dict], actor: User) -> dict:
    """Transit → destination, only what the destination scanned. Anything left stays in transit, flagged."""
    t = s.get(Transfer, transfer_id)
    if t is None or t.status not in ("in_transit", "partial"):
        raise StockError("кўчириш йўлда эмас")
    lines = []
    for sc in scans:
        lot = sc.get("serial") or sc.get("batch") or ""
        qty = float(sc.get("qty", 1))
        held = balance(s, sku=sc["sku"], loc=TRANSIT, bin_=transfer_id, lot=lot, cond=S)
        if held + 1e-9 < qty:
            raise StockError(f"{sc['sku']} {lot}: {qty:g} та сканерланди, бу кўчиришда эса бор-йўғи {held:g} та")
        cond = Q if sc.get("damaged") else S
        lines.append(Line(sku=sc["sku"], qty=qty, from_loc=TRANSIT, from_bin=transfer_id, from_cond=S,
                          to_loc=t.to_loc, to_bin=DOCK, to_cond=cond, lot=lot))
    scan_key = ",".join(sorted(f"{l.sku}:{l.lot}:{l.qty:g}" for l in lines))
    res = record(s, idem_key=f"omai:transfer_in:{transfer_id}:{scan_key}", source="omai", kind="transfer_in",
                 lines=lines, transfer_id=transfer_id, actor=actor.id)
    left = balance(s, loc=TRANSIT, bin_=transfer_id)
    t.status = "received" if left <= 1e-9 else "partial"
    if t.status == "received":
        t.received_at = now_utc()
        for task in tasks.open_for_ref(s, f"transfer:{transfer_id}", "transfer_in"):
            task.state = "accepted"
        resolve_exception(s, "transfer_incomplete", transfer_id, "fully received")
    else:
        raise_exception(s, "transfer_incomplete", transfer_id,
                        f"кўчириш {transfer_id[:8]} {t.from_loc}→{t.to_loc}: {left:g} дона ҳали йўлда")
    return {"movement": res.movement.id, "created": res.created, "status": t.status, "in_transit": left}


# ----------------------------------------------------------------- returns and technicians
def open_return(s: Session, *, original_doc: str, sku: str, qty: float, location: str, actor: User,
                serial: str = "", kind: str = "customer") -> ReturnCase:
    """Linked to the original document and serial; quarantined until inspected."""
    item = _item(s, sku)
    orig = s.scalars(select(MovementLine).join(Movement).where(Movement.doc_ref == original_doc,
                                                              MovementLine.sku == sku)).all()
    if not orig:
        raise StockError(f"{original_doc} да {sku} йўқ — қайтариш асл ҳужжатга боғланиши керак")
    if item.serial_tracked:
        if not serial or serial not in {l.lot for l in orig}:
            raise StockError(f"{serial or '(йўқ)'} серия рақами {original_doc} да бўлмаган")
        if serial_location(s, sku, serial):
            raise StockError(f"{serial} серия рақами аллақачон қолдиқда бор")
    rc = ReturnCase(original_doc=original_doc, sku=sku, lot=serial, qty=qty, location_code=location, kind=kind)
    s.add(rc)
    s.flush()
    record(s, idem_key=f"omai:return:{rc.id}", source="omai", kind="return", doc_ref=original_doc, actor=actor.id,
           lines=[Line(sku=sku, qty=qty, to_loc=location, to_bin=DOCK, to_cond=Q, lot=serial)])
    tasks.create(s, kind="inspect_return", title=f"Қайтарилган {sku} {serial} ни текшириш ({original_doc})",
                 deadline=now_utc() + timedelta(days=1), assignee_id=actor.id, location=location,
                 ref=f"return:{rc.id}", accepting_role=Role.manager.value)
    return rc


def decide_return(s: Session, *, return_id: str, accept: bool, approver: User) -> ReturnCase:
    """Saleable availability comes back only after an approved acceptance."""
    rc = s.get(ReturnCase, return_id)
    if rc is None or rc.status != "quarantine":
        raise StockError("бу қайтариш қарор кутмаяпти")
    if Role(approver.role) not in (Role.manager, Role.owner):
        raise StockError("қайтарилган товарни сотувга қайтариш учун менежер керак")
    to = S if accept else D
    record(s, idem_key=f"omai:return_decision:{rc.id}", source="omai", kind="condition", actor=approver.id,
           doc_ref=rc.original_doc, lines=[Line(sku=rc.sku, qty=rc.qty, from_loc=rc.location_code, from_bin=DOCK,
                                                 from_cond=Q, to_loc=rc.location_code, to_bin=DOCK, to_cond=to,
                                                 lot=rc.lot)])
    rc.status, rc.decided_by = ("accepted" if accept else "rejected"), approver.id
    return rc


def issue_to_technician(s: Session, *, sku: str, qty: float, from_loc: str, from_bin: str, technician_loc: str,
                        expected_back: datetime, actor: User, lot: str = "", ref: str) -> dict:
    loc = s.get(Location, technician_loc)
    if loc is None or loc.kind != "technician":
        raise StockError(f"{technician_loc} техник жойи эмас")
    res = record(s, idem_key=f"omai:tech_issue:{ref}", source="omai", kind="tech_issue", actor=actor.id, doc_ref=ref,
                 lines=[Line(sku=sku, qty=qty, from_loc=from_loc, from_bin=from_bin, from_cond=S,
                             to_loc=technician_loc, to_cond=S, lot=lot)])
    tasks.create(s, kind="tech_return", title=f"{loc.name}: {qty:g} × {sku} ни қайтариш ёки ҳисоботини бериш",
                 deadline=expected_back, assignee_id=loc.custodian_id, location=technician_loc, ref=f"tech:{ref}",
                 notify=False)
    return {"movement": res.movement.id, "created": res.created}


def change_condition(s: Session, *, sku: str, qty: float, location: str, bin_: str, to: str, actor: User,
                     ref: str, lot: str = "", from_cond: str = S) -> dict:
    """Mark units damaged / expired / quarantined (they stop being available at once)."""
    res = record(s, idem_key=f"omai:condition:{ref}", source="omai", kind="condition", actor=actor.id,
                 lines=[Line(sku=sku, qty=qty, from_loc=location, from_bin=bin_, from_cond=from_cond,
                             to_loc=location, to_bin=bin_, to_cond=to, lot=lot)])
    return {"movement": res.movement.id, "created": res.created}
