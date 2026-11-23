"""The movement ledger: the only way stock changes.

A *position* is (location, bin, sku, lot, condition, ownership). A balance is
the sum of movement lines arriving at the position minus those leaving it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from omai.models import (Allocation, AuditLog, Condition, Exception_, Movement, MovementLine, Ownership, now_utc)

TRANSIT = "TRANSIT"   # the transit location; a transfer's units sit in bin = transfer id


# Exception kinds as people read them (Uzbek Cyrillic); the codes stay English.
KIND_UZ = {"allocation_short": "банд қилишга етмади", "count_variance": "санашда фарқ",
           "damaged_receipt": "шикастланган қабул", "erp_posting_pending": "SAP'га киритилмаган",
           "expired_saleable": "муддати ўтган товар сотувда", "late_receipt": "кечиккан қабул",
           "negative_stock": "манфий қолдиқ", "old_allocation": "эски банд", "over_receipt": "ортиқча қабул",
           "release_blocked": "чиқариш тўхтатилди", "short_receipt": "кам қабул", "source_down": "манба ишламаяпти",
           "system_mismatch": "тизимлар мос эмас", "transfer_incomplete": "кўчириш тугалланмаган",
           "transfer_overdue": "кўчириш кечикди", "unmapped": "боғланмаган товар / жой"}


class StockError(ValueError):
    """A movement that would break the ledger rules (not enough stock, wrong serial, ...)."""


# Checks other modules add (the count freeze): guard(session, lines, source, kind) raises StockError to refuse.
GUARDS: list = []


@dataclass
class Line:
    sku: str
    qty: float
    from_loc: str | None = None
    from_bin: str = ""
    from_cond: str | None = None
    to_loc: str | None = None
    to_bin: str = ""
    to_cond: str | None = None
    lot: str = ""
    ownership: str = Ownership.own.value
    expiry: datetime | None = None
    received_at: datetime | None = None


@dataclass
class Result:
    movement: Movement
    created: bool
    mirrored_of: str | None = None
    warnings: list[str] = field(default_factory=list)


def audit(s: Session, actor: str | None, action: str, ref: str | None = None, **detail: object) -> None:
    s.add(AuditLog(actor_id=actor, action=action, ref=ref, detail=_jsonable(detail)))


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    return out


def raise_exception(s: Session, kind: str, ref: str | None, detail: str, severity: str = "medium",
                    owner_decision: bool = False, **data: object) -> Exception_:
    """Open an exception, or return the one already open for the same kind and ref (no duplicate noise)."""
    existing = s.scalar(select(Exception_).where(Exception_.kind == kind, Exception_.ref == ref,
                                                 Exception_.status == "open"))
    if existing:
        return existing
    exc = Exception_(kind=kind, ref=ref, detail=detail, severity=severity, owner_decision=owner_decision,
                     data=_jsonable(data))
    s.add(exc)
    s.flush()
    return exc


def resolve_exception(s: Session, kind: str, ref: str, resolution: str) -> int:
    rows = s.scalars(select(Exception_).where(Exception_.kind == kind, Exception_.ref == ref,
                                              Exception_.status == "open")).all()
    for r in rows:
        r.status, r.resolved_at, r.resolution = "resolved", now_utc(), resolution
    return len(rows)


# ----------------------------------------------------------------- balances
def _filters(col_prefix: str, *, sku, loc, bin_, cond, lot, ownership):
    c = lambda name: getattr(MovementLine, f"{col_prefix}_{name}")  # noqa: E731
    conds = [c("loc").is_not(None)]
    if sku is not None:
        conds.append(MovementLine.sku == sku)
    if loc is not None:
        conds.append(c("loc") == loc)
    if bin_ is not None:
        conds.append(c("bin") == bin_)
    if cond is not None:
        conds.append(c("cond") == cond)
    if lot is not None:
        conds.append(MovementLine.lot == lot)
    if ownership is not None:
        conds.append(MovementLine.ownership == ownership)
    return conds


def balance(s: Session, *, sku: str | None = None, loc: str | None = None, bin_: str | None = None,
            cond: str | None = None, lot: str | None = None, ownership: str | None = None,
            at: datetime | None = None, exclude_transit: bool = False) -> float:
    """Units at the matching positions; ``at`` = as of that event time (count cutoffs)."""
    total = 0.0
    for prefix, sign in (("to", 1), ("from", -1)):
        q = select(func.coalesce(func.sum(MovementLine.qty), 0)).join(Movement)
        q = q.where(and_(*_filters(prefix, sku=sku, loc=loc, bin_=bin_, cond=cond, lot=lot, ownership=ownership)))
        if at is not None:
            q = q.where(Movement.event_time <= at)
        if exclude_transit:
            q = q.where(getattr(MovementLine, f"{prefix}_loc") != TRANSIT)
        total += sign * float(s.scalar(q) or 0)
    return round(total, 6)


def positions(s: Session, *, sku: str | None = None, loc: str | None = None, cond: str | None = None,
              at: datetime | None = None) -> list[dict]:
    """Every non-zero position, oldest receipt first (FIFO order)."""
    acc: dict[tuple, dict] = {}
    for prefix, sign in (("to", 1), ("from", -1)):
        L = MovementLine
        cols = (getattr(L, f"{prefix}_loc"), getattr(L, f"{prefix}_bin"), L.sku, L.lot,
                getattr(L, f"{prefix}_cond"), L.ownership)
        q = select(*cols, func.sum(L.qty), func.min(L.received_at), func.min(L.expiry)).join(Movement)
        q = q.where(and_(*_filters(prefix, sku=sku, loc=loc, bin_=None, cond=cond, lot=None, ownership=None)))
        if at is not None:
            q = q.where(Movement.event_time <= at)
        q = q.group_by(*cols)
        for row in s.execute(q):
            key = tuple(row[:6])
            p = acc.setdefault(key, {"loc": row[0], "bin": row[1], "sku": row[2], "lot": row[3], "cond": row[4],
                                     "ownership": row[5], "qty": 0.0, "received_at": None, "expiry": None})
            p["qty"] += sign * float(row[6] or 0)
            if sign == 1:
                p["received_at"], p["expiry"] = row[7], row[8]
    out = [p for p in acc.values() if abs(p["qty"]) > 1e-9]
    far = datetime.max.replace(tzinfo=None)
    out.sort(key=lambda p: (_naive(p["expiry"]) or far, _naive(p["received_at"]) or far, p["bin"]))
    return out


def _naive(d: datetime | None) -> datetime | None:
    return d.replace(tzinfo=None) if d else None


def allocated(s: Session, sku: str, loc: str) -> float:
    return float(s.scalar(select(func.coalesce(func.sum(Allocation.qty), 0)).where(
        Allocation.sku == sku, Allocation.location_code == loc, Allocation.status == "active")) or 0)


def available(s: Session, sku: str, loc: str) -> float:
    """Saleable own on-hand less active allocations, counted once. Never below zero."""
    on_hand = balance(s, sku=sku, loc=loc, cond=Condition.saleable.value, ownership=Ownership.own.value)
    return max(0.0, on_hand - allocated(s, sku, loc))


def company_total(s: Session, sku: str) -> float:
    """All units the company holds, transit included, each unit once."""
    return balance(s, sku=sku)


# ----------------------------------------------------------------- recording
def record(s: Session, *, idem_key: str, source: str, kind: str, lines: Iterable[Line],
           event_time: datetime | None = None, shared_ref: str | None = None, doc_ref: str | None = None,
           transfer_id: str | None = None, actor: str | None = None, note: str = "",
           strict: bool = True) -> Result:
    """Record one event once.

    * Same ``idem_key`` again (a retry, a repeated sync) → the first movement, nothing changes.
    * Same ``shared_ref`` from another system (Billz sale also posted in SAP) → linked, nothing changes.
    * ``strict`` (OMAI scans): refuse to take units a position does not hold. External postings
      (``strict=False``) are recorded as the system says, and a shortfall opens an exception instead.
    """
    existing = s.scalar(select(Movement).where(Movement.idem_key == idem_key))
    if existing:
        return Result(existing, created=False)
    if shared_ref:
        twin = s.scalar(select(Movement).where(Movement.shared_ref == shared_ref))
        if twin:
            audit(s, actor, "movement.mirrored", shared_ref, idem_key=idem_key, of=twin.id, source=source)
            return Result(twin, created=False, mirrored_of=twin.id)

    lines = list(lines)
    if not lines:
        raise StockError("ҳаракатда камида битта қатор бўлиши керак")
    for guard in GUARDS:
        guard(s, lines, source, kind)
    warnings: list[str] = []
    need: dict[tuple, float] = {}
    for ln in lines:
        if ln.qty <= 0:
            raise StockError(f"{ln.sku}: миқдор мусбат бўлиши керак")
        if ln.from_loc is None and ln.to_loc is None:
            raise StockError(f"{ln.sku}: қатор бирор жойдан чиқиши ёки бирор жойга кириши керак")
        if ln.from_loc is not None:
            key = (ln.sku, ln.from_loc, ln.from_bin, ln.from_cond, ln.lot, ln.ownership)
            need[key] = need.get(key, 0) + ln.qty
    for (sku, loc, bin_, cond, lot, own), qty in need.items():
        have = balance(s, sku=sku, loc=loc, bin_=bin_, cond=cond, lot=lot, ownership=own)
        if have + 1e-9 < qty:
            msg = f"{sku} {loc}/{bin_ or '-'} {cond} {lot or ''}: {qty:g} керак, бор-йўғи {have:g}"
            if strict:
                raise StockError(msg)
            warnings.append(msg)

    mv = Movement(idem_key=idem_key, source=source, kind=kind, doc_ref=doc_ref, transfer_id=transfer_id,
                  event_time=event_time or now_utc(), shared_ref=shared_ref, actor_id=actor, note=note)
    for ln in lines:
        mv.lines.append(MovementLine(
            sku=ln.sku, qty=ln.qty, lot=ln.lot, ownership=ln.ownership, expiry=ln.expiry,
            from_loc=ln.from_loc, from_bin=ln.from_bin, from_cond=ln.from_cond,
            to_loc=ln.to_loc, to_bin=ln.to_bin, to_cond=ln.to_cond,
            received_at=ln.received_at or (event_time or now_utc() if ln.from_loc is None else ln.received_at)))
    s.add(mv)
    s.flush()
    audit(s, actor, f"movement.{kind}", mv.id, idem_key=idem_key, source=source, lines=len(lines))
    for w in warnings:
        raise_exception(s, "negative_stock", f"{mv.id}", f"{source} OMAI'даги қолдиқдан кўп чиқарди: {w}",
                        severity="high", movement=mv.id)
    return Result(mv, created=True, warnings=warnings)
