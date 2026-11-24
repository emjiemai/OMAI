"""Ageing (frozen stock), cross-system reconciliation, owner measures and permission-filtered reports."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omai.ledger import TRANSIT, balance, positions, raise_exception, resolve_exception
from omai.models import (COST_ROLES, Allocation, Count, CountLine, ExpectedReceipt, ExternalBalance, Exception_,
                         Item, Movement, MovementLine, Role, Task, TaskState, Transfer, User, now_utc)
from omai.notify import queue

OUT_KINDS = ("dispatch", "sale")


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------- ageing
def last_out(s: Session, sku: str, loc: str) -> datetime | None:
    return _aware(s.scalar(select(func.max(Movement.event_time)).join(MovementLine).where(
        MovementLine.sku == sku, MovementLine.from_loc == loc, Movement.kind.in_(OUT_KINDS))))


def ageing(s: Session, now: datetime | None = None) -> list[dict]:
    """Every held position with its age class. Critical spares are kept, not proposed for clearance."""
    now = now or now_utc()
    out = []
    for p in positions(s):
        if p["qty"] <= 0 or p["loc"] == TRANSIT:
            continue
        item = s.get(Item, p["sku"])
        if item is None:
            continue
        received = _aware(p["received_at"]) or now
        sold = last_out(s, p["sku"], p["loc"])
        idle = (now - max(received, sold or received)).days
        flags = []
        if p["cond"] in ("damaged", "quarantine", "expired"):
            flags.append(p["cond"])
        exp = _aware(p["expiry"])
        if exp and exp < now:
            flags.append("expired")
        elif exp and exp < now + timedelta(days=30):
            flags.append("expiring")
        if idle >= item.dead_days:
            flags.append("dead")
        elif idle >= item.slow_days:
            flags.append("slow")
        if item.excess_qty is not None:
            here = balance(s, sku=p["sku"], loc=p["loc"])
            if here > item.excess_qty:
                flags.append("excess")
        if not flags:
            continue
        out.append({"sku": p["sku"], "name": item.name, "brand": item.brand, "category": item.category,
                    "loc": p["loc"], "bin": p["bin"], "lot": p["lot"], "cond": p["cond"], "qty": p["qty"],
                    "idle_days": idle, "flags": sorted(set(flags)), "critical_spare": item.critical_spare,
                    "value": round(p["qty"] * item.unit_cost, 2), "ownership": p["ownership"]})
    return out


def send_ageing_to_aipro(s: Session, now: datetime | None = None) -> int:
    """Facts and risks only; AIPRO decides on purchasing or clearance. Customer-owned and consignment goods are
    reported separately and never proposed for our clearance."""
    rows = ageing(s, now)
    day = (now or now_utc()).date().isoformat()
    props = [{**r, "proposal": "keep (critical spare)" if r["critical_spare"] else
              ("clearance" if {"dead", "excess"} & set(r["flags"]) else
               "review" if r["ownership"] == "own" else "return to owner")} for r in rows]
    if props:
        queue(s, "aipro", None, {"type": "stock_ageing", "date": day, "rows": props}, dedupe_key=f"aipro:ageing:{day}")
    for r in rows:
        if "expired" in r["flags"] and r["cond"] == "saleable":
            raise_exception(s, "expired_saleable", f"{r['loc']}:{r['bin']}:{r['sku']}:{r['lot']}",
                            f"{r['qty']:g} × {r['sku']} {r['loc']}/{r['bin']} да муддати ўтган, лекин ҳали сотувда — "
                            f"move to expired", severity="high")
    return len(props)


# ----------------------------------------------------------------- reconciliation
# A Billz shop linked to one of these locations is compared with SAP over all the listed warehouses together: the Abay
# shop sells from G.A._01, 05 and G.A._02 (mgmg-agents BILLZ_SAP_WAREHOUSES, spec 17 — the same rule as its check).
BILLZ_SHOP_SAP_WAREHOUSES: dict[str, tuple[str, ...]] = {"G.A._01": ("G.A._01", "05", "G.A._02")}
REF_LABEL = {"omai": "OMAI журналида", "sap": "SAP'да", "1c": "1С'да", "billz": "Billz'да"}


def reconcile_systems(s: Session) -> list[dict]:
    """Each location against its own authority (``Location.movement_source``), never an average.

    * ``omai`` (staff scan there): SAP, 1C and Billz are each compared with OMAI's verified ledger.
    * ``sap`` / ``billz``: the other systems are compared with that system's balance — not with OMAI's ledger, which
      holds nothing for such a location until its movements are booked from an opening balance.

    A pair is compared only where both sides know the item: the reference row exists, or the reference is SAP (whose
    picture is complete: an item it doesn't list there is 0). A difference opens an exception; agreement, or a pair
    no longer compared, closes it.
    """
    from collections import defaultdict

    from omai.models import Location, SourceSync

    locs = {loc.code: loc for loc in s.scalars(select(Location))}
    synced = {r.source for r in s.scalars(select(SourceSync)) if r.last_success}
    ext: dict[str, dict[tuple[str, str], ExternalBalance]] = defaultdict(dict)
    for eb in s.scalars(select(ExternalBalance)):
        ext[eb.source][(eb.location_code, eb.sku)] = eb
    diffs, compared = [], set()
    for source, rows in ext.items():
        for (loc_code, sku), eb in rows.items():
            loc = locs.get(loc_code)
            authority = loc.movement_source if loc else None
            if loc is None or source == authority:
                continue                       # the authority is the reference, not compared with itself
            if authority == "omai":
                ref_qty = balance(s, sku=sku, loc=loc_code)
            else:
                group = BILLZ_SHOP_SAP_WAREHOUSES.get(loc_code, (loc_code,)) if source == "billz" else (loc_code,)
                ref_rows = [ext.get(authority, {}).get((g, sku)) for g in group]
                if authority not in synced or (authority != "sap" and all(r is None for r in ref_rows)):
                    continue
                ref_qty = sum(r.qty for r in ref_rows if r is not None)
            ref = f"{source}:{loc_code}:{sku}"
            compared.add(ref)
            if abs(ref_qty - eb.qty) > 1e-6:
                d = {"source": source, "loc": loc_code, "sku": sku, "reference": authority, "reference_qty": ref_qty,
                     "system": eb.qty, "diff": eb.qty - ref_qty, "read_at": eb.read_at.isoformat()}
                diffs.append(d)
                raise_exception(s, "system_mismatch", ref,
                                f"{REF_LABEL.get(source, source)} {loc_code} да {eb.qty:g} × {sku}, "
                                f"{REF_LABEL.get(authority, authority)} {ref_qty:g}", severity="medium", **d)
            else:
                resolve_exception(s, "system_mismatch", ref, "balances agree")
    for exc in s.scalars(select(Exception_).where(Exception_.kind == "system_mismatch", Exception_.status == "open")):
        if exc.ref not in compared:
            exc.status, exc.resolved_at = "resolved", now_utc()
            exc.resolution = "not compared: the location's own authority, or an item one side doesn't know"
    return diffs


def check_holds(s: Session, older_than_days: int = 7) -> int:
    cutoff = now_utc() - timedelta(days=older_than_days)
    n = 0
    for a in s.scalars(select(Allocation).where(Allocation.status == "active", Allocation.created_at < cutoff)):
        raise_exception(s, "old_allocation", a.id, f"{a.qty:g} × {a.sku} {a.order_ref} учун банд, "
                                                   f"{a.created_at:%d.%m} дан бери", severity="low")
        n += 1
    return n


def check_transfers_and_receipts(s: Session, now: datetime | None = None) -> int:
    now = now or now_utc()
    n = 0
    for t in s.scalars(select(Transfer).where(Transfer.status.in_(["in_transit", "partial"]))):
        if t.due_at and _aware(t.due_at) < now:
            raise_exception(s, "transfer_overdue", t.id, f"кўчириш {t.id[:8]} {t.from_loc}→{t.to_loc} "
                                                         f"{t.due_at:%d.%m %H:%M} гача қабул қилинмади", severity="medium")
            n += 1
    for r in s.scalars(select(ExpectedReceipt).where(ExpectedReceipt.status.in_(["expected", "partial"]))):
        if r.due_at and _aware(r.due_at) < now:
            raise_exception(s, "late_receipt", f"{r.po_ref}:{r.sku}", f"{r.po_ref} {r.sku}: "
                            f"яна {r.qty_expected - r.qty_received:g} та кутилмоқда (муддат {r.due_at:%d.%m})")
            n += 1
        if r.status == "complete" and not r.erp_posted:
            raise_exception(s, "erp_posting_pending", f"{r.po_ref}:{r.sku}",
                            f"{r.po_ref} {r.sku} жисмонан қабул қилинди, SAP'да ҳали киритилмаган", severity="low")
    return n


# ----------------------------------------------------------------- measures
def measures(s: Session, viewer: User, since: datetime | None = None) -> dict:
    since = since or now_utc() - timedelta(days=30)
    lines = s.scalars(select(CountLine).join(Count).where(Count.closed_at.is_not(None), Count.closed_at >= since)).all()
    verified = [l for l in lines if l.results and l.sku != "*"]
    matched_first = 0
    serial_lines = serial_ok = 0
    for l in verified:
        first = min(l.results, key=lambda r: (r.round, r.at))
        if first.qty is not None and l.book_qty is not None and abs(first.qty - l.book_qty) < 1e-9:
            matched_first += 1
        item = s.get(Item, l.sku)
        if item and (item.serial_tracked or item.batch_tracked):
            serial_lines += 1
            serial_ok += l.status == "matched"
    counts_due = s.scalars(select(Count).where(Count.original_deadline >= since,
                                               Count.original_deadline <= now_utc())).all()
    on_time = [c for c in counts_due if c.closed_at and _aware(c.closed_at) <= _aware(c.original_deadline)]
    open_states = [st.value for st in (TaskState.assigned, TaskState.in_progress, TaskState.blocked,
                                       TaskState.recount_required)]
    overdue = s.scalar(select(func.count(Task.id)).where(Task.state.in_(open_states), Task.deadline < now_utc()))
    exc_open = s.scalars(select(Exception_).where(Exception_.status == "open")).all()
    shortages = sum(1 for e in exc_open if e.kind == "count_variance" and (e.data.get("diff") or 0) < 0)
    out = {
        "inventory_accuracy": round(matched_first / len(verified), 4) if verified else None,
        "count_lines_verified": len(verified),
        "serial_batch_accuracy": round(serial_ok / serial_lines, 4) if serial_lines else None,
        "count_discipline": round(len(on_time) / len(counts_due), 4) if counts_due else None,
        "counts_due": len(counts_due),
        "tasks_overdue": overdue,
        "transfers_overdue": sum(1 for e in exc_open if e.kind == "transfer_overdue"),
        "incomplete_receipts": sum(1 for e in exc_open if e.kind in ("short_receipt", "late_receipt")),
        "open_exceptions": len(exc_open),
        "safety_overdue": s.scalar(select(func.count(Task.id)).where(Task.kind.like("safety%"),
                                                                     Task.state.in_(open_states),
                                                                     Task.deadline < now_utc())),
        "variance_shortages": shortages,
    }
    if Role(viewer.role) in COST_ROLES:
        rows = ageing(s)
        out["aged_value"] = round(sum(r["value"] for r in rows if {"slow", "dead"} & set(r["flags"])), 2)
        out["dead_value"] = round(sum(r["value"] for r in rows if "dead" in r["flags"]), 2)
    return out


def stock_rows(s: Session, viewer: User) -> list[dict]:
    show_cost = Role(viewer.role) in COST_ROLES
    rows = []
    for p in positions(s):
        item = s.get(Item, p["sku"])
        if viewer.role in (Role.staff.value, Role.custodian.value) and viewer.location_code \
                and p["loc"] != viewer.location_code:
            continue
        r = {"location": p["loc"], "bin": p["bin"], "sku": p["sku"], "name": item.name if item else "",
             "lot": p["lot"], "condition": p["cond"], "ownership": p["ownership"], "qty": p["qty"]}
        if show_cost and item:
            r["unit_cost"], r["value"] = item.unit_cost, round(item.unit_cost * p["qty"], 2)
        rows.append(r)
    return rows


def excel(rows: list[dict], sheet: str = "Stock") -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    if rows:
        ws.append(list(rows[0].keys()))
        for r in rows:
            ws.append([", ".join(v) if isinstance(v, list) else v for v in r.values()])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
