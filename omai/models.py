"""OMAI data model.

Stock is never stored as an editable number. Every unit change is a line of a
*movement* (one physical or system event, with a unique idempotency key), and a
balance is the sum of those lines. That gives, by construction:

* an event processed twice changes stock once (``movements.idem_key`` unique);
* one event mirrored by SAP, 1C and Billz counts once (``shared_ref``);
* a transfer moves units source → transit → destination, never both at once;
* the ledger at any cutoff = lines whose ``event_time`` <= cutoff (so a late or
  backdated posting lands on the right side of a count);
* condition (saleable / damaged / quarantine / expired) and ownership
  (own / customer / consignment) are part of the stock position, and
  allocations are a *separate* table — reserved units are a subset of stock.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, time, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, Time, UniqueConstraint)
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column, relationship

from omai.db import Base


class UTCDateTime(TypeDecorator):
    """Always timezone-aware UTC in Python, whatever the database (SQLite drops the offset)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.astimezone(timezone.utc)
            if dialect.name == "sqlite":
                value = value.replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Role(str, enum.Enum):
    staff = "staff"            # counts, scans, moves
    custodian = "custodian"    # store stock custodian
    checker = "checker"        # independent checker of material variances
    manager = "manager"        # warehouse / store manager, approves within limit
    accountant = "accountant"  # approves adjustments within a higher limit
    owner = "owner"            # receives owner-decision exceptions
    it = "it"                  # integrations, access, recovery


COST_ROLES = {Role.manager, Role.accountant, Role.owner}
APPROVER_ROLES = {Role.manager, Role.accountant, Role.owner}


class Condition(str, enum.Enum):
    saleable = "saleable"
    damaged = "damaged"
    quarantine = "quarantine"
    expired = "expired"


class Ownership(str, enum.Enum):
    own = "own"
    customer = "customer"
    consignment = "consignment"


class LocationKind(str, enum.Enum):
    warehouse = "warehouse"
    store = "store"
    showroom = "showroom"
    staging = "staging"
    transit = "transit"
    technician = "technician"


class TaskState(str, enum.Enum):
    assigned = "assigned"
    in_progress = "in_progress"
    blocked = "blocked"
    submitted = "submitted"
    accepted = "accepted"
    recount_required = "recount_required"
    cancelled = "cancelled"


OPEN_TASK_STATES = {TaskState.assigned, TaskState.in_progress, TaskState.blocked, TaskState.recount_required}


# ----------------------------------------------------------------- people & places
class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20))
    login: Mapped[str | None] = mapped_column(String(120), unique=True)
    pin_hash: Mapped[str | None] = mapped_column(String(200))
    telegram_chat_id: Mapped[str | None] = mapped_column(String(40))
    location_code: Mapped[str | None] = mapped_column(String(40))
    manager_id: Mapped[str | None] = mapped_column(String(32))
    shift_start: Mapped[time] = mapped_column(Time, default=time(9, 0))
    shift_end: Mapped[time] = mapped_column(Time, default=time(18, 0))
    workdays: Mapped[str] = mapped_column(String(20), default="0123456")  # Monday=0; 7 = every day
    on_call: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Location(Base):
    __tablename__ = "locations"
    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    sap_whs: Mapped[str | None] = mapped_column(String(40))      # SAP WhsCode
    onec_ref: Mapped[str | None] = mapped_column(String(80))     # 1C склад Ref_Key
    billz_shop_id: Mapped[str | None] = mapped_column(String(80))
    custodian_id: Mapped[str | None] = mapped_column(String(32))
    # Which system's events are booked into the ledger for this location (the others are only compared):
    # "omai" = staff scan here (pilot warehouse), "sap" = SAP documents, "billz" = store tills.
    movement_source: Mapped[str] = mapped_column(String(10), default="sap")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Bin(Base):
    __tablename__ = "bins"
    __table_args__ = (UniqueConstraint("location_code", "code"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_code: Mapped[str] = mapped_column(ForeignKey("locations.code"))
    code: Mapped[str] = mapped_column(String(40))
    zone: Mapped[str] = mapped_column(String(40), default="")
    size_class: Mapped[str] = mapped_column(String(10), default="M")   # S / M / L / XL (pallet)
    max_load_kg: Mapped[float] = mapped_column(Float, default=200)
    floor_level: Mapped[bool] = mapped_column(Boolean, default=False)  # heavy goods at floor level only
    chemicals_ok: Mapped[bool] = mapped_column(Boolean, default=False) # detergents only in approved bins
    pick_rank: Mapped[int] = mapped_column(Integer, default=50)        # lower = closer to dispatch
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Item(Base):
    __tablename__ = "items"
    sku: Mapped[str] = mapped_column(String(60), primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    brand: Mapped[str] = mapped_column(String(60), default="")       # Garmin / Tanita / Primus / LONDRY
    category: Mapped[str] = mapped_column(String(60), default="")    # equipment / parts / detergent / display / supplies
    unit: Mapped[str] = mapped_column(String(20), default="pcs")
    barcode: Mapped[str | None] = mapped_column(String(60), index=True)
    serial_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    batch_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    expiry_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    weight_kg: Mapped[float] = mapped_column(Float, default=1)
    size_class: Mapped[str] = mapped_column(String(10), default="M")
    hazardous: Mapped[bool] = mapped_column(Boolean, default=False)
    heavy_handling: Mapped[bool] = mapped_column(Boolean, default=False)  # needs a qualified handler
    abc_group: Mapped[str] = mapped_column(String(1), default="C")
    unit_cost: Mapped[float] = mapped_column(Float, default=0)   # visible to COST_ROLES only
    slow_days: Mapped[int] = mapped_column(Integer, default=180)
    dead_days: Mapped[int] = mapped_column(Integer, default=365)
    excess_qty: Mapped[float | None] = mapped_column(Float)
    critical_spare: Mapped[bool] = mapped_column(Boolean, default=False)
    sap_code: Mapped[str | None] = mapped_column(String(60))
    onec_ref: Mapped[str | None] = mapped_column(String(80))
    billz_id: Mapped[str | None] = mapped_column(String(80))


# ----------------------------------------------------------------- the ledger
class Movement(Base):
    __tablename__ = "movements"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    idem_key: Mapped[str] = mapped_column(String(200), unique=True)     # source:doc_type:doc_id[:line]
    shared_ref: Mapped[str | None] = mapped_column(String(200), index=True)  # same event in another system
    source: Mapped[str] = mapped_column(String(20))                    # omai / sap / 1c / billz
    kind: Mapped[str] = mapped_column(String(30))
    doc_ref: Mapped[str | None] = mapped_column(String(120))
    transfer_id: Mapped[str | None] = mapped_column(String(32), index=True)
    event_time: Mapped[datetime] = mapped_column(UTCDateTime())   # when it physically happened
    posted_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now_utc)  # when we learned it
    actor_id: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text, default="")
    lines: Mapped[list["MovementLine"]] = relationship(back_populates="movement", cascade="all, delete-orphan")


class MovementLine(Base):
    """``qty`` units leave ``from_*`` (if set) and arrive at ``to_*`` (if set)."""

    __tablename__ = "movement_lines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    movement_id: Mapped[str] = mapped_column(ForeignKey("movements.id"))
    sku: Mapped[str] = mapped_column(ForeignKey("items.sku"))
    qty: Mapped[float] = mapped_column(Float)
    lot: Mapped[str] = mapped_column(String(80), default="")     # serial or batch; "" when untracked
    expiry: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ownership: Mapped[str] = mapped_column(String(20), default=Ownership.own.value)
    from_loc: Mapped[str | None] = mapped_column(String(40))
    from_bin: Mapped[str] = mapped_column(String(40), default="")
    from_cond: Mapped[str | None] = mapped_column(String(20))
    to_loc: Mapped[str | None] = mapped_column(String(40))
    to_bin: Mapped[str] = mapped_column(String(40), default="")
    to_cond: Mapped[str | None] = mapped_column(String(20))
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime())  # FIFO key of the receipt
    movement: Mapped[Movement] = relationship(back_populates="lines")


class Allocation(Base):
    __tablename__ = "allocations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    sku: Mapped[str] = mapped_column(ForeignKey("items.sku"))
    location_code: Mapped[str] = mapped_column(String(40))
    qty: Mapped[float] = mapped_column(Float)
    order_ref: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active / cancelled / fulfilled
    source: Mapped[str] = mapped_column(String(20), default="omai")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now_utc)


# ----------------------------------------------------------------- documents
class ExpectedReceipt(Base):
    """A planned external arrival (PO line). Never counted as stock until received."""

    __tablename__ = "expected_receipts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    po_ref: Mapped[str] = mapped_column(String(120), index=True)
    sku: Mapped[str] = mapped_column(ForeignKey("items.sku"))
    location_code: Mapped[str] = mapped_column(String(40))
    qty_expected: Mapped[float] = mapped_column(Float)
    qty_received: Mapped[float] = mapped_column(Float, default=0)
    qty_damaged: Mapped[float] = mapped_column(Float, default=0)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    erp_posted: Mapped[bool] = mapped_column(Boolean, default=False)   # physical arrival ≠ ERP posting
    status: Mapped[str] = mapped_column(String(20), default="expected")  # expected / partial / complete / closed


class DispatchOrder(Base):
    __tablename__ = "dispatch_orders"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    order_ref: Mapped[str] = mapped_column(String(120), unique=True)
    location_code: Mapped[str] = mapped_column(String(40))
    customer: Mapped[str] = mapped_column(String(200), default="")
    lines: Mapped[list] = mapped_column(JSON, default=list)   # [{sku, qty, serials?}]
    release_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / approved / refused
    release_by: Mapped[str | None] = mapped_column(String(120))
    release_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(20), default="open")  # open / picked / packed / dispatched / blocked
    handover_evidence: Mapped[dict | None] = mapped_column(JSON)


class Transfer(Base):
    __tablename__ = "transfers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    from_loc: Mapped[str] = mapped_column(String(40))
    to_loc: Mapped[str] = mapped_column(String(40))
    lines: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="planned")  # planned / in_transit / received / partial
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now_utc)


class ReturnCase(Base):
    __tablename__ = "returns"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    original_doc: Mapped[str] = mapped_column(String(120))
    sku: Mapped[str] = mapped_column(ForeignKey("items.sku"))
    lot: Mapped[str] = mapped_column(String(80), default="")
    qty: Mapped[float] = mapped_column(Float)
    location_code: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(20), default="customer")  # customer / technician / supplier
    status: Mapped[str] = mapped_column(String(20), default="quarantine")  # quarantine / accepted / rejected
    expected_back_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decided_by: Mapped[str | None] = mapped_column(String(32))


# ----------------------------------------------------------------- counting
class Count(Base):
    __tablename__ = "counts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    location_code: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(20), default="cycle")   # cycle / full / baseline / handover / incident
    cutoff_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    freeze: Mapped[bool] = mapped_column(Boolean, default=False)
    counter_id: Mapped[str] = mapped_column(String(32))
    checker_id: Mapped[str] = mapped_column(String(32))
    deadline: Mapped[datetime] = mapped_column(UTCDateTime())
    original_deadline: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(20), default="open")
    # open → counting → (recount) → review → approval → closed
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now_utc)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lines: Mapped[list["CountLine"]] = relationship(back_populates="count", cascade="all, delete-orphan")


class CountLine(Base):
    __tablename__ = "count_lines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    count_id: Mapped[str] = mapped_column(ForeignKey("counts.id"))
    bin: Mapped[str] = mapped_column(String(40), default="")
    sku: Mapped[str] = mapped_column(String(60))
    lot: Mapped[str] = mapped_column(String(80), default="")
    unlisted: Mapped[bool] = mapped_column(Boolean, default=False)   # found goods not on the list
    book_qty: Mapped[float | None] = mapped_column(Float)            # ledger at cutoff — never shown to counters
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending / matched / variance / recount / investigating / resolved
    cause: Mapped[str | None] = mapped_column(Text)
    count: Mapped[Count] = relationship(back_populates="lines")
    results: Mapped[list["CountResult"]] = relationship(back_populates="line", cascade="all, delete-orphan")


class CountResult(Base):
    """One counter's answer for one line. ``qty`` None = blank (not counted); 0 = counted empty."""

    __tablename__ = "count_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("count_lines.id"))
    counter_id: Mapped[str] = mapped_column(String(32))
    round: Mapped[int] = mapped_column(Integer, default=1)
    qty: Mapped[float | None] = mapped_column(Float)
    serials: Mapped[list] = mapped_column(JSON, default=list)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now_utc)
    line: Mapped[CountLine] = relationship(back_populates="results")


class CountSchedule(Base):
    __tablename__ = "count_schedule"
    __table_args__ = (UniqueConstraint("location_code", "sku"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_code: Mapped[str] = mapped_column(String(40))
    sku: Mapped[str] = mapped_column(String(60))
    last_accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_due: Mapped[datetime] = mapped_column(UTCDateTime())
    original_due: Mapped[datetime] = mapped_column(UTCDateTime())
    trigger: Mapped[str | None] = mapped_column(String(60))  # why an extra count was planned


# ----------------------------------------------------------------- work
class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(30))     # receive / putaway / pick / pack / dispatch / transfer / count / ...
    location_code: Mapped[str | None] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    result_required: Mapped[str] = mapped_column(Text, default="")
    assignee_id: Mapped[str | None] = mapped_column(String(32))
    accepting_role: Mapped[str] = mapped_column(String(20), default=Role.manager.value)
    deadline: Mapped[datetime] = mapped_column(UTCDateTime())
    original_deadline: Mapped[datetime] = mapped_column(UTCDateTime())
    priority: Mapped[int] = mapped_column(Integer, default=3)   # 1 = urgent … 5 = low
    urgent: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(20), default=TaskState.assigned.value)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    ref: Mapped[str | None] = mapped_column(String(120), index=True)
    blocker: Mapped[str | None] = mapped_column(Text)
    reminded_before: Mapped[bool] = mapped_column(Boolean, default=False)
    reminded_after: Mapped[bool] = mapped_column(Boolean, default=False)
    escalated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    accepted_by: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now_utc)


class Exception_(Base):
    __tablename__ = "exceptions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10), default="medium")  # low / medium / high / critical
    ref: Mapped[str | None] = mapped_column(String(200), index=True)
    detail: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="open")
    owner_decision: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now_utc)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resolution: Mapped[str | None] = mapped_column(Text)


class Approval(Base):
    """A request bound to one exact action (``payload_hash``); approving it allows that action only."""

    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    action: Mapped[str] = mapped_column(String(30))   # adjustment / write_off / disposal
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[str] = mapped_column(String(32))
    decided_by: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / approved / refused / executed
    posting_ok: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now_utc)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


# ----------------------------------------------------------------- systems
class SourceSync(Base):
    __tablename__ = "source_syncs"
    source: Mapped[str] = mapped_column(String(30), primary_key=True)
    critical: Mapped[bool] = mapped_column(Boolean, default=True)
    last_attempt: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_success: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    cursor: Mapped[str | None] = mapped_column(String(200))
    failures: Mapped[int] = mapped_column(Integer, default=0)


class ExternalBalance(Base):
    """What each system says is on hand, as last read. Compared, never averaged."""

    __tablename__ = "external_balances"
    __table_args__ = (UniqueConstraint("source", "location_code", "sku"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(20))
    location_code: Mapped[str] = mapped_column(String(40))
    sku: Mapped[str] = mapped_column(String(60))
    qty: Mapped[float] = mapped_column(Float)
    read_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now_utc)


class Job(Base):
    """A scheduled run, keyed so a restart never runs the same slot twice."""

    __tablename__ = "jobs"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)   # name:YYYY-MM-DDTHH:MM
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now_utc)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1)


class Outbox(Base):
    """Messages to send (Telegram, AIPRO, MIYA). Sent by a worker with retries; a crash never loses one."""

    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    channel: Mapped[str] = mapped_column(String(20))   # telegram / aipro / miya
    to: Mapped[str | None] = mapped_column(String(120))
    body: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now_utc)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now_utc)
    actor_id: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(60))
    ref: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Skill(Base):
    """A reviewed, versioned warehouse procedure (receiving, slotting, counting...) or an SOP source."""

    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("name", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(300), default="")
    source_date: Mapped[str] = mapped_column(String(20), default="")
    reviewed_by: Mapped[str | None] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=False)
