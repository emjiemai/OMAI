"""Physical counts: blind counts, cutoffs, independent recounts, investigation, approval, closure, scheduling.

Rules from the procedure:
* counters never see the expected quantity;
* explicit 0 = counted empty, blank = not counted (a blank line keeps the count open);
* the book quantity is the ledger *at the cutoff* (event time), so a sale after the cutoff or a backdated
  posting before it lands on the correct side;
* a material difference or serial mismatch needs an independent recount; both results are kept and never
  averaged;
* quantities are only corrected by a separately approved adjustment — never to make systems agree.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from omai import approvals, tasks
from omai.config import COUNT_TOLERANCE_UNITS, COUNT_TOLERANCE_VALUE
from omai.ledger import GUARDS, TRANSIT, StockError, audit, balance, positions, raise_exception
from omai.models import (Bin, Count, CountLine, CountResult, CountSchedule, ExternalBalance, Item, Movement,
                         MovementLine, Role, User, now_utc)

FREQ_DAYS = {"A": 7, "B": 30, "C": 91}
EMPTY = "*"   # an empty-bin line


# ----------------------------------------------------------------- freeze guard
def _freeze_guard(s: Session, lines, source: str, kind: str) -> None:
    if source != "omai" or kind in ("adjustment", "write_off", "disposal"):
        return
    frozen = s.scalars(select(Count).where(Count.freeze.is_(True), Count.status.in_(["counting", "recount"]))).all()
    for c in frozen:
        skus = {l.sku for l in c.lines if l.sku != EMPTY}
        for ln in lines:
            if ln.sku in skus and c.location_code in (ln.from_loc, ln.to_loc):
                raise StockError(f"{ln.sku} {c.location_code} да {c.id[:8]} санаш учун музлатилган (frozen) — кутинг ёки "
                                 f"тасдиқланган ҳаракатлар журналидан фойдаланинг")


GUARDS.append(_freeze_guard)


# ----------------------------------------------------------------- create / start
def create(s: Session, *, location: str, counter: User, checker: User, deadline: datetime,
           skus: list[str] | None = None, bins: list[str] | None = None, kind: str = "cycle",
           freeze: bool = False) -> Count:
    if counter.id == checker.id:
        raise StockError("текширувчи санаган кишидан бошқа одам бўлиши керак")
    c = Count(location_code=location, kind=kind, counter_id=counter.id, checker_id=checker.id, deadline=deadline,
              original_deadline=deadline, freeze=freeze)
    s.add(c)
    s.flush()
    all_bins = [b.code for b in s.scalars(select(Bin).where(Bin.location_code == location, Bin.active.is_(True)))]
    bins = bins or all_bins
    held = [p for p in positions(s, loc=location) if p["qty"] > 0 and (skus is None or p["sku"] in skus)]
    seen = set()
    for p in held:
        if p["bin"] in bins or not bins or p["bin"] in ("DOCK", "PACK", ""):
            key = (p["bin"], p["sku"], p["lot"])
            if key not in seen:
                seen.add(key)
                c.lines.append(CountLine(bin=p["bin"], sku=p["sku"], lot=p["lot"]))
    # listed SKUs the ledger shows nowhere in these bins: counted in every listed bin
    for sku in skus or []:
        if not any(k[1] == sku for k in seen):
            for b in bins:
                c.lines.append(CountLine(bin=b, sku=sku, lot=""))
                seen.add((b, sku, ""))
    # zero-stock bins are counted too: sku "*" = "should be empty"; anything found is entered as unlisted
    for b in bins:
        if not any(k[0] == b for k in seen):
            c.lines.append(CountLine(bin=b, sku=EMPTY, lot=""))
    s.flush()
    tasks.create(s, kind="count", title=f"Кўр-кўрона санаш {c.id[:8]}, {location} ({len(c.lines)} қатор)",
                 deadline=deadline, assignee_id=counter.id, location=location, ref=f"count:{c.id}",
                 accepting_role=Role.checker.value,
                 result_required="Товар, ячейка ва серия/партияни сканерланг. Бўш ячейкага 0 ёзинг; фақат "
                                 "санамаган бўлсангиз бўш қолдиринг.")
    audit(s, None, "count.create", c.id, lines=len(c.lines), counter=counter.id, checker=checker.id)
    return c


def start(s: Session, count: Count, cutoff: datetime | None = None) -> Count:
    if count.status != "open":
        raise StockError(f"санаш ҳолати: {count.status}")
    count.cutoff_at = cutoff or now_utc()
    count.status = "counting"
    audit(s, None, "count.cutoff", count.id, cutoff=count.cutoff_at, freeze=count.freeze)
    return count


def blind_form(count: Count) -> list[dict]:
    """What the counter sees. No book quantity — ever."""
    return [{"line_id": l.id, "bin": l.bin, "sku": l.sku, "lot": l.lot, "round": _round(l) + 1}
            for l in count.lines if l.status in ("pending", "recount")]


def _round(line: CountLine) -> int:
    return max((r.round for r in line.results), default=0)


# ----------------------------------------------------------------- submit
def submit(s: Session, count: Count, counter: User, entries: list[dict]) -> dict:
    """``entries``: [{line_id | (bin, sku, lot), qty: number|None, serials: []}].

    ``qty`` None (or missing) means blank: the line stays uncounted. Unlisted goods may be added by bin/sku.
    """
    if count.status not in ("counting", "recount"):
        raise StockError(f"санаш ҳолати: {count.status}")
    recounting = count.status == "recount"
    if recounting and counter.id == count.counter_id:
        raise StockError("қайта санашни биринчи санаган кишидан бошқа одам қилиши керак")
    if not recounting and counter.id != count.counter_id:
        raise StockError("бу санаш бошқа кишига берилган")
    by_id = {l.id: l for l in count.lines}
    stored = 0
    for e in entries:
        line = by_id.get(e.get("line_id")) if e.get("line_id") else None
        if line is None:
            line = next((l for l in count.lines if l.bin == e.get("bin") and l.sku == e.get("sku")
                         and l.lot == (e.get("lot") or "")), None)
        if line is None:
            if s.get(Item, e.get("sku")) is None:
                raise StockError(f"номаълум артикул {e.get('sku')} — ёрлиқни расмга олиб хабар беринг")
            line = CountLine(bin=e.get("bin", ""), sku=e["sku"], lot=e.get("lot") or "", unlisted=True)
            count.lines.append(line)
            s.flush()
        if recounting and line.status != "recount":
            continue
        qty = e.get("qty")
        qty = None if qty in (None, "") else float(qty)
        rnd = _round(line) + 1 if recounting else 1
        existing = next((r for r in line.results if r.round == rnd and r.counter_id == counter.id), None)
        if existing:
            existing.qty, existing.serials, existing.at = qty, e.get("serials") or [], now_utc()
        else:
            line.results.append(CountResult(counter_id=counter.id, round=rnd, qty=qty, serials=e.get("serials") or []))
        stored += 1
    s.flush()
    return {"stored": stored, "blank": sum(1 for l in count.lines if _latest(l) is None)}


def _latest(line: CountLine) -> CountResult | None:
    rs = sorted(line.results, key=lambda r: (r.round, r.at))
    return rs[-1] if rs and rs[-1].qty is not None else None


# ----------------------------------------------------------------- evaluate
def _book(s: Session, count: Count, line: CountLine) -> float:
    if line.sku == EMPTY:
        return 0.0
    return balance(s, sku=line.sku, loc=count.location_code, bin_=line.bin, lot=line.lot or None,
                   at=count.cutoff_at)


def _book_serials(s: Session, count: Count, line: CountLine) -> set[str]:
    return {p["lot"] for p in positions(s, sku=line.sku, loc=count.location_code, at=count.cutoff_at)
            if p["bin"] == line.bin and p["qty"] > 0 and p["lot"] and (not line.lot or p["lot"] == line.lot)}


def material(diff: float, unit_cost: float) -> bool:
    return abs(diff) > COUNT_TOLERANCE_UNITS or abs(diff) * unit_cost > COUNT_TOLERANCE_VALUE > 0


def evaluate(s: Session, count: Count) -> dict:
    """Compare each counted line with the ledger at the cutoff. Never changes stock."""
    if count.cutoff_at is None:
        raise StockError("санаш бошланган вақт ёзилмаган")
    out = {"matched": 0, "recount": 0, "investigate": 0, "blank": 0}
    for line in count.lines:
        if line.status in ("matched", "resolved"):
            out["matched"] += line.status == "matched"
            continue
        line.book_qty = _book(s, count, line)
        latest = _latest(line)
        if latest is None:
            out["blank"] += 1
            continue
        item = s.get(Item, line.sku)
        serial_mismatch = False
        if item and item.serial_tracked and latest.serials:
            serial_mismatch = set(latest.serials) != _book_serials(s, count, line)
        diff = latest.qty - line.book_qty
        if not material(diff, item.unit_cost if item else 0) and not serial_mismatch:
            line.status = "matched"
            out["matched"] += 1
            continue
        rounds = sorted({r.round for r in line.results if r.qty is not None})
        if len(rounds) == 1:
            line.status = "recount"
            out["recount"] += 1
            continue
        last, prev = [next(r for r in reversed(line.results) if r.round == k) for k in rounds[-2:][::-1]]
        if last.qty == prev.qty:   # two independent counts agree: the difference is real → investigate
            line.status = "investigating"
            out["investigate"] += 1
        else:                     # they disagree: keep both, count again — never average
            line.status = "recount"
            out["recount"] += 1
    if out["recount"]:
        count.status = "recount"
        checker = s.get(User, count.checker_id)
        if not tasks.open_for_ref(s, f"count:{count.id}", "recount"):
            tasks.create(s, kind="recount", title=f"{count.id[:8]} санаш учун мустақил қайта санаш "
                                                  f"({out['recount']} қатор)",
                         deadline=now_utc() + timedelta(hours=4), assignee_id=checker.id if checker else None,
                         location=count.location_code, ref=f"count:{count.id}", accepting_role=Role.manager.value)
        for l in count.lines:
            if l.status == "recount" and s.get(Item, l.sku) and s.get(Item, l.sku).serial_tracked:
                reschedule_trigger(s, count.location_code, l.sku, "serial mismatch")
    elif out["investigate"]:
        count.status = "review"
        for l in count.lines:
            if l.status == "investigating":
                raise_exception(s, "count_variance", f"{count.id}:{l.id}",
                                f"count {count.id[:8]} {l.bin} {l.sku}: book {l.book_qty:g}, counted "
                                f"{_latest(l).qty:g} twice", severity="high", hints=investigate(s, count, l))
    elif out["blank"] == 0:
        count.status = "review"
    for t in tasks.open_for_ref(s, f"count:{count.id}"):
        if t.state == "submitted" and t.kind == "recount" and out["recount"] == 0:
            t.state = "accepted"
    return out


def investigate(s: Session, count: Count, line: CountLine) -> list[str]:
    """Usual causes to check: late postings, wrong units, duplicates, stock in other places."""
    hints = []
    window = timedelta(hours=24)
    near = s.scalars(select(Movement).join(MovementLine).where(
        MovementLine.sku == line.sku, Movement.event_time.between(count.cutoff_at - window, count.cutoff_at + window))
    ).all()
    for m in near:
        posted = m.posted_at if m.posted_at.tzinfo else m.posted_at.replace(tzinfo=count.cutoff_at.tzinfo)
        if posted > count.cutoff_at and m.event_time <= count.cutoff_at:
            hints.append(f"late posting {m.source} {m.doc_ref or m.id[:8]} (event before cutoff, posted after)")
        elif m.event_time > count.cutoff_at:
            hints.append(f"movement after cutoff: {m.kind} {m.doc_ref or m.id[:8]}")
    elsewhere = [p for p in positions(s, sku=line.sku) if p["loc"] != count.location_code or p["bin"] != line.bin]
    for p in elsewhere[:5]:
        hints.append(f"also at {p['loc']}/{p['bin'] or '-'} ({p['cond']}): {p['qty']:g}")
    for eb in s.scalars(select(ExternalBalance).where(ExternalBalance.sku == line.sku,
                                                      ExternalBalance.location_code == count.location_code)):
        hints.append(f"{eb.source} says {eb.qty:g} at {count.location_code}")
    item = s.get(Item, line.sku)
    if item and item.unit not in ("pcs", "шт"):
        hints.append(f"unit is {item.unit} — check packs vs pieces")
    return hints


def record_cause(s: Session, count: Count, line_id: int, cause: str, corrective_task_for: User | None,
                 actor: User) -> CountLine:
    line = next(l for l in count.lines if l.id == line_id)
    if line.status != "investigating":
        raise StockError("сабаб фақат тасдиқланган фарқ учун керак")
    line.cause = cause
    if corrective_task_for:
        tasks.create(s, kind="corrective", title=f"{line.sku} {line.bin} фарқининг сабабини бартараф этиш: {cause}",
                     deadline=now_utc() + timedelta(days=3), assignee_id=corrective_task_for.id,
                     location=count.location_code, ref=f"count:{count.id}:{line.id}", notify=True)
    audit(s, actor.id, "count.cause", f"{count.id}:{line.id}", cause=cause)
    return line


def adjustment_payload(count: Count, line: CountLine) -> dict:
    latest = _latest(line)
    return {"count_id": count.id, "line_id": line.id, "sku": line.sku, "loc": count.location_code, "bin": line.bin,
            "lot": line.lot, "book": line.book_qty, "counted": latest.qty, "diff": latest.qty - line.book_qty,
            "counted_by": sorted({r.counter_id for r in line.results})}


def request_adjustment(s: Session, count: Count, line_id: int, requester: User):
    line = next(l for l in count.lines if l.id == line_id)
    if line.status != "investigating" or not line.cause:
        raise StockError("тузатиш сўрашдан олдин сабабини ёзинг")
    payload = adjustment_payload(count, line)
    item = s.get(Item, line.sku)
    value = abs(payload["diff"]) * (item.unit_cost if item else 0)
    action = "adjustment" if payload["diff"] > 0 else "write_off"
    return approvals.request(s, action=action, payload=payload, value=value, reason=line.cause, requester=requester)


def apply_adjustment(s: Session, count: Count, approval, actor: User, poster=None) -> dict:
    line = next(l for l in count.lines if l.id == approval.payload["line_id"])
    res = approvals.execute(s, approval, actor, adjustment_payload(count, line), poster=poster)
    if approval.status == "executed" or res["posting_ok"] is None:
        line.status = "resolved"
    return res


# ----------------------------------------------------------------- close
def close(s: Session, count: Count, actor: User) -> Count:
    """Close only when every line is matched or resolved through an executed correction."""
    if actor.id == count.counter_id:
        raise StockError("санаган киши ўз санашини ёпа олмайди")
    pending = [l for l in count.lines if l.status not in ("matched", "resolved")]
    if pending:
        raise StockError(f"{len(pending)} қатор ҳал бўлмаган (бўш, қайта санаш ёки тасдиқ кутмоқда)")
    count.status, count.closed_at = "closed", now_utc()
    count.freeze = False
    for t in tasks.open_for_ref(s, f"count:{count.id}"):
        t.state, t.accepted_by = "accepted", actor.id
    for sku in {l.sku for l in count.lines if l.sku != EMPTY}:
        accept_schedule(s, count.location_code, sku, count.closed_at)
    audit(s, actor.id, "count.close", count.id)
    return count


# ----------------------------------------------------------------- schedule
def accept_schedule(s: Session, location: str, sku: str, at: datetime) -> CountSchedule:
    item = s.get(Item, sku)
    days = FREQ_DAYS.get(item.abc_group if item else "C", 91)
    row = s.scalar(select(CountSchedule).where(CountSchedule.location_code == location, CountSchedule.sku == sku))
    nxt = at + timedelta(days=days)
    if row is None:
        row = CountSchedule(location_code=location, sku=sku, next_due=nxt, original_due=nxt)
        s.add(row)
    row.last_accepted_at, row.next_due, row.original_due, row.trigger = at, nxt, nxt, None
    return row


def reschedule_trigger(s: Session, location: str, sku: str, why: str) -> None:
    row = s.scalar(select(CountSchedule).where(CountSchedule.location_code == location, CountSchedule.sku == sku))
    soon = now_utc() + timedelta(days=1)
    if row is None:
        s.add(CountSchedule(location_code=location, sku=sku, next_due=soon, original_due=soon, trigger=why))
    elif row.next_due.replace(tzinfo=soon.tzinfo) > soon:
        row.next_due, row.trigger = soon, why   # original_due is kept


def due_items(s: Session, location: str, at: datetime, capacity: int) -> list[str]:
    """SKUs at a location due for counting, A before B before C, oldest due first; never-counted first."""
    held = {p["sku"] for p in positions(s, loc=location) if p["qty"] > 0 and p["loc"] != TRANSIT}
    sched = {r.sku: r for r in s.scalars(select(CountSchedule).where(CountSchedule.location_code == location))}
    due = []
    for sku in held | set(sched):
        r = sched.get(sku)
        if r is None or r.next_due.replace(tzinfo=at.tzinfo) <= at:
            item = s.get(Item, sku)
            due.append(((r is not None), "ABC".index(item.abc_group if item else "C"),
                        r.next_due.replace(tzinfo=None) if r else datetime.min, sku))
    due.sort()
    return [d[3] for d in due[:capacity]]
