"""OMAI HTTP API: staff workspace, owner dashboard, webhooks (CASH release, SAP gateway push, Telegram)."""

from __future__ import annotations

import hmac
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from omai.ledger import KIND_UZ
from omai import approvals, auth, counting, insights, tasks, warehouse
from omai.config import settings
from omai.db import SessionLocal, init_db
from omai.ledger import StockError, available, balance
from omai.models import (APPROVER_ROLES, COST_ROLES, OPEN_TASK_STATES, Approval, Count, Exception_,
                         Role, Task, User, now_utc)
from omai.sources import freshness

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="OMAI Virtual Warehouse Manager", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()
    if settings.run_scheduler:
        from omai.routine import start_scheduler
        app.state.scheduler = start_scheduler()


# ----------------------------------------------------------------- plumbing
def db() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def current_user(authorization: str = Header(default=""), s: Session = Depends(db)) -> User:
    token = authorization.removeprefix("Bearer ").strip()
    uid = auth.verify(token) if token else None
    user = s.get(User, uid) if uid else None
    if user is None or not user.active:
        raise HTTPException(401, "тизимга киринг")
    return user


def need(*roles: Role):
    def dep(user: User = Depends(current_user)) -> User:
        if Role(user.role) not in roles:
            raise HTTPException(403, f"лавозим керак: {', '.join(r.value for r in roles)}")
        return user
    return dep


def guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except (StockError, tasks.TaskError) as exc:
        raise HTTPException(409, str(exc)) from exc


def task_json(t: Task) -> dict:
    return {"id": t.id, "kind": t.kind, "title": t.title, "location": t.location_code, "state": t.state,
            "deadline": t.deadline.isoformat(), "original_deadline": t.original_deadline.isoformat(),
            "priority": t.priority, "urgent": t.urgent, "overdue": tasks.is_overdue(t), "ref": t.ref,
            "result_required": t.result_required, "accepting_role": t.accepting_role, "blocker": t.blocker,
            "assignee_id": t.assignee_id}


# ----------------------------------------------------------------- pages
@app.get("/")
def workspace() -> FileResponse:
    return FileResponse(STATIC / "workspace.html")


@app.get("/owner")
def owner_page() -> FileResponse:
    return FileResponse(STATIC / "owner.html")


@app.get("/health")
def health(s: Session = Depends(db)) -> dict:
    return {"ok": True, "sources": freshness(s), "at": now_utc().isoformat()}


# ----------------------------------------------------------------- auth
class LoginIn(BaseModel):
    login: str
    pin: str


@app.post("/auth/login")
def login(body: LoginIn, s: Session = Depends(db)) -> dict:
    user = s.scalar(select(User).where(User.login == body.login, User.active.is_(True)))
    if user is None or not auth.check_pin(body.pin, user.pin_hash):
        raise HTTPException(401, "логин ёки PIN нотўғри")
    return {"token": auth.issue(user.id), "user": {"id": user.id, "name": user.name, "role": user.role}}


# ----------------------------------------------------------------- tasks
@app.get("/me/tasks")
def my_tasks(user: User = Depends(current_user), s: Session = Depends(db)) -> list[dict]:
    states = [st.value for st in OPEN_TASK_STATES] + ["submitted"]
    q = select(Task).where(Task.state.in_(states))
    if Role(user.role) in (Role.staff, Role.custodian, Role.checker, Role.it):
        q = q.where(Task.assignee_id == user.id)
    return [task_json(t) for t in s.scalars(q.order_by(Task.priority, Task.deadline))]


class ActIn(BaseModel):
    note: str = ""
    evidence: dict | None = None


@app.post("/tasks/{task_id}/{action}")
def task_action(task_id: str, action: str, body: ActIn, user: User = Depends(current_user),
                s: Session = Depends(db)) -> dict:
    t = s.get(Task, task_id)
    if t is None:
        raise HTTPException(404)
    return task_json(guard(tasks.act, s, t, action, user, body.note, body.evidence))


class RescheduleIn(BaseModel):
    deadline: datetime
    reason: str


@app.post("/tasks/{task_id}/reschedule")
def task_reschedule(task_id: str, body: RescheduleIn, user: User = Depends(current_user),
                    s: Session = Depends(db)) -> dict:
    t = s.get(Task, task_id)
    return task_json(guard(tasks.reschedule, s, t, body.deadline, user, body.reason))


# ----------------------------------------------------------------- stock
@app.get("/stock")
def stock(user: User = Depends(current_user), s: Session = Depends(db)) -> list[dict]:
    return insights.stock_rows(s, user)


@app.get("/stock/{sku}/{loc}")
def stock_one(sku: str, loc: str, user: User = Depends(current_user), s: Session = Depends(db)) -> dict:
    return {"sku": sku, "loc": loc, "on_hand": balance(s, sku=sku, loc=loc), "available": available(s, sku, loc),
            "pick_plan": warehouse.pick_plan(s, sku, loc, 10 ** 9)}


@app.get("/reports/stock.xlsx")
def stock_xlsx(user: User = Depends(current_user), s: Session = Depends(db)) -> Response:
    return Response(insights.excel(insights.stock_rows(s, user)),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=omai-stock.xlsx"})


# ----------------------------------------------------------------- receiving / moving
class ReceiveIn(BaseModel):
    receipt_id: str
    po_ref: str
    sku: str
    location: str
    good_qty: float
    damaged_qty: float = 0
    serials: list[str] = []
    batch: str = ""
    expiry: datetime | None = None


@app.post("/receive")
def receive(body: ReceiveIn, user: User = Depends(current_user), s: Session = Depends(db)) -> dict:
    return guard(warehouse.receive, s, actor=user, **body.model_dump())


class PutawayIn(BaseModel):
    task_ref: str
    sku: str
    qty: float
    location: str
    scanned_bin: str
    lot: str = ""


@app.post("/putaway")
def putaway(body: PutawayIn, user: User = Depends(current_user), s: Session = Depends(db)) -> dict:
    return guard(warehouse.putaway, s, actor=user, **body.model_dump())


@app.get("/slotting/{sku}/{loc}")
def slotting(sku: str, loc: str, user: User = Depends(current_user), s: Session = Depends(db)) -> dict:
    return {"bin": guard(warehouse.suggest_bin, s, sku, loc)}


class ScansIn(BaseModel):
    scans: list[dict]


@app.post("/orders/{order_ref}/pick")
def pick(order_ref: str, body: ScansIn, user: User = Depends(current_user), s: Session = Depends(db)) -> dict:
    return guard(warehouse.pick, s, order_ref=order_ref, scans=body.scans, actor=user)


class DispatchIn(BaseModel):
    handover: dict
    manual_override_by: str | None = None


@app.post("/orders/{order_ref}/dispatch")
def dispatch(order_ref: str, body: DispatchIn, user: User = Depends(current_user), s: Session = Depends(db)) -> dict:
    override = s.get(User, body.manual_override_by) if body.manual_override_by else None
    try:
        return warehouse.dispatch(s, order_ref=order_ref, actor=user, handover=body.handover,
                                  manual_override_by=override)
    except StockError as exc:
        s.commit()   # keep the block record and the escalation
        raise HTTPException(409, str(exc)) from exc


class ReleaseIn(BaseModel):
    approved: bool


@app.post("/orders/{order_ref}/release")
def release(order_ref: str, body: ReleaseIn, user: User = Depends(need(Role.accountant, Role.owner)),
            s: Session = Depends(db)) -> dict:
    """Release authorisation by the designated approver (CASH's role), logged in — no extra webhook secret."""
    o = guard(warehouse.set_release, s, order_ref, body.approved, f"{user.role}:{user.id}")
    return {"order_ref": o.order_ref, "release_status": o.release_status}


class TransferIn(BaseModel):
    from_loc: str
    to_loc: str
    lines: list[dict]
    assignee_id: str | None = None


@app.post("/transfers")
def transfer_create(body: TransferIn, user: User = Depends(need(Role.manager, Role.owner, Role.custodian)),
                    s: Session = Depends(db)) -> dict:
    t = guard(warehouse.create_transfer, s, **body.model_dump())
    return {"id": t.id, "status": t.status}


@app.post("/transfers/{tid}/dispatch")
def transfer_out(tid: str, body: ScansIn, user: User = Depends(current_user), s: Session = Depends(db)) -> dict:
    return guard(warehouse.dispatch_transfer, s, transfer_id=tid, scans=body.scans, actor=user)


@app.post("/transfers/{tid}/receive")
def transfer_in(tid: str, body: ScansIn, user: User = Depends(current_user), s: Session = Depends(db)) -> dict:
    return guard(warehouse.receive_transfer, s, transfer_id=tid, scans=body.scans, actor=user)


class ReturnIn(BaseModel):
    original_doc: str
    sku: str
    qty: float
    location: str
    serial: str = ""
    kind: str = "customer"


@app.post("/returns")
def return_open(body: ReturnIn, user: User = Depends(current_user), s: Session = Depends(db)) -> dict:
    rc = guard(warehouse.open_return, s, actor=user, **body.model_dump())
    return {"id": rc.id, "status": rc.status}


@app.post("/returns/{rid}/{decision}")
def return_decide(rid: str, decision: str, user: User = Depends(need(Role.manager, Role.owner)),
                  s: Session = Depends(db)) -> dict:
    rc = guard(warehouse.decide_return, s, return_id=rid, accept=decision == "accept", approver=user)
    return {"id": rc.id, "status": rc.status}


# ----------------------------------------------------------------- counts
class CountIn(BaseModel):
    location: str
    counter_id: str
    checker_id: str
    deadline: datetime
    skus: list[str] | None = None
    bins: list[str] | None = None
    kind: str = "cycle"
    freeze: bool = False


@app.post("/counts")
def count_create(body: CountIn, user: User = Depends(need(Role.manager, Role.owner)), s: Session = Depends(db)) -> dict:
    c = guard(counting.create, s, location=body.location, counter=s.get(User, body.counter_id),
              checker=s.get(User, body.checker_id), deadline=body.deadline, skus=body.skus, bins=body.bins,
              kind=body.kind, freeze=body.freeze)
    return {"id": c.id, "lines": len(c.lines)}


def _count(s: Session, cid: str) -> Count:
    c = s.get(Count, cid)
    if c is None:
        raise HTTPException(404)
    return c


@app.post("/counts/{cid}/start")
def count_start(cid: str, user: User = Depends(current_user), s: Session = Depends(db)) -> dict:
    c = guard(counting.start, s, _count(s, cid))
    return {"cutoff_at": c.cutoff_at.isoformat()}


@app.get("/counts/{cid}/form")
def count_form(cid: str, user: User = Depends(current_user), s: Session = Depends(db)) -> list[dict]:
    """The blind form: no expected quantities, for anyone. Opening it starts the count (records the cutoff)."""
    c = _count(s, cid)
    if c.status == "open" and user.id == c.counter_id:
        counting.start(s, c)
    return counting.blind_form(c)


class CountEntries(BaseModel):
    entries: list[dict]


@app.post("/counts/{cid}/submit")
def count_submit(cid: str, body: CountEntries, user: User = Depends(current_user), s: Session = Depends(db)) -> dict:
    return guard(counting.submit, s, _count(s, cid), user, body.entries)


@app.post("/counts/{cid}/import")
async def count_import(cid: str, file: UploadFile = File(...), user: User = Depends(current_user),
                       s: Session = Depends(db)) -> dict:
    """Excel count import: columns bin, sku, lot, qty (empty qty = not counted, 0 = empty)."""
    from io import BytesIO

    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(await file.read()), read_only=True)
    rows = list(wb.active.iter_rows(values_only=True))
    head = [str(h or "").strip().lower() for h in rows[0]]
    entries = [{k: v for k, v in zip(head, r)} for r in rows[1:] if any(v is not None for v in r)]
    for e in entries:
        e["lot"] = str(e.get("lot") or "")
    return guard(counting.submit, s, _count(s, cid), user, entries)


@app.post("/counts/{cid}/evaluate")
def count_evaluate(cid: str, user: User = Depends(need(Role.checker, Role.manager, Role.owner)),
                   s: Session = Depends(db)) -> dict:
    return guard(counting.evaluate, s, _count(s, cid))


@app.get("/counts/{cid}/review")
def count_review(cid: str, user: User = Depends(need(Role.checker, Role.manager, Role.accountant, Role.owner)),
                 s: Session = Depends(db)) -> dict:
    c = _count(s, cid)
    return {"id": c.id, "status": c.status, "cutoff_at": c.cutoff_at.isoformat() if c.cutoff_at else None,
            "lines": [{"id": l.id, "bin": l.bin, "sku": l.sku, "lot": l.lot, "book": l.book_qty, "status": l.status,
                       "cause": l.cause, "results": [{"round": r.round, "by": r.counter_id, "qty": r.qty,
                                                      "serials": r.serials} for r in l.results]} for l in c.lines]}


class CauseIn(BaseModel):
    cause: str
    corrective_assignee: str | None = None


@app.post("/counts/{cid}/lines/{lid}/cause")
def count_cause(cid: str, lid: int, body: CauseIn, user: User = Depends(need(Role.checker, Role.manager)),
                s: Session = Depends(db)) -> dict:
    who = s.get(User, body.corrective_assignee) if body.corrective_assignee else None
    line = guard(counting.record_cause, s, _count(s, cid), lid, body.cause, who, user)
    return {"line": line.id, "status": line.status}


@app.post("/counts/{cid}/lines/{lid}/adjustment")
def count_adjust(cid: str, lid: int, user: User = Depends(need(Role.checker, Role.manager)),
                 s: Session = Depends(db)) -> dict:
    a = guard(counting.request_adjustment, s, _count(s, cid), lid, user)
    return {"approval": a.id, "action": a.action, "value": a.value if Role(user.role) in COST_ROLES else None}


class DecideIn(BaseModel):
    approve: bool
    note: str = ""


@app.post("/approvals/{aid}/decide")
def approval_decide(aid: str, body: DecideIn, user: User = Depends(need(*APPROVER_ROLES)),
                    s: Session = Depends(db)) -> dict:
    a = s.get(Approval, aid)
    if a is None:
        raise HTTPException(404)
    return {"status": guard(approvals.decide, s, a, user, body.approve, body.note).status}


@app.post("/approvals/{aid}/execute")
def approval_execute(aid: str, user: User = Depends(need(Role.manager, Role.accountant, Role.owner)),
                     s: Session = Depends(db)) -> dict:
    a = s.get(Approval, aid)
    c = _count(s, a.payload["count_id"])
    return guard(counting.apply_adjustment, s, c, a, user, poster=None)  # IT posts the approved document in SAP and confirms


@app.post("/counts/{cid}/close")
def count_close(cid: str, user: User = Depends(need(Role.checker, Role.manager, Role.owner)),
                s: Session = Depends(db)) -> dict:
    return {"status": guard(counting.close, s, _count(s, cid), user).status}


# ----------------------------------------------------------------- dashboard
@app.get("/dashboard")
def dashboard(user: User = Depends(need(Role.manager, Role.accountant, Role.owner, Role.it)),
              s: Session = Depends(db)) -> dict:
    ex = s.scalars(select(Exception_).where(Exception_.status == "open").order_by(Exception_.created_at.desc())).all()
    states = [st.value for st in OPEN_TASK_STATES] + ["submitted"]
    due = s.scalars(select(Task).where(Task.state.in_(states)).order_by(Task.deadline)).all()
    show_cost = Role(user.role) in COST_ROLES
    return {
        "measures": insights.measures(s, user),
        "sources": freshness(s),
        "exceptions": [{"id": e.id, "kind": e.kind, "kind_label": KIND_UZ.get(e.kind, e.kind), "severity": e.severity, "detail": e.detail, "ref": e.ref,
                        "owner_decision": e.owner_decision, "created_at": e.created_at.isoformat()} for e in ex],
        "tasks": [task_json(t) for t in due],
        "ageing": [r if show_cost else {k: v for k, v in r.items() if k != "value"} for r in insights.ageing(s)],
        "approvals": [{"id": a.id, "action": a.action, "reason": a.reason, "requested_by": a.requested_by,
                       "value": a.value if show_cost else None, "status": a.status}
                      for a in s.scalars(select(Approval).where(Approval.status == "pending"))],
    }


# ----------------------------------------------------------------- integrations
def _sap_secret_ok(given: str) -> bool:
    secret = settings.sap_push_webhook_secret.get_secret_value()
    return bool(secret) and hmac.compare_digest(given, secret)


@app.post("/sap/data/{kind}")
async def sap_data(kind: str, request: Request, x_omai_secret: str = Header(default=""),
                   s: Session = Depends(db)) -> dict:
    """The SAP gateway push (``push-ar-aging.ps1`` → ``$OmaiHost``): one tool's ``{"rows", "complete"}``."""
    from omai.connectors.sap_b1 import SapError, ingest_kind

    if not _sap_secret_ok(x_omai_secret):
        raise HTTPException(403)
    try:
        return {"ok": True, **ingest_kind(s, kind, await request.json())}
    except SapError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/sap/push")
async def sap_push(request: Request, x_omai_secret: str = Header(default=""), s: Session = Depends(db)) -> dict:
    from omai.connectors.sap_b1 import SapError, ingest_payload

    if not _sap_secret_ok(x_omai_secret):
        raise HTTPException(403)
    try:
        return ingest_payload(s, await request.json())
    except SapError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request, s: Session = Depends(db)) -> dict[str, Any]:
    """Task buttons: ``t:<task id>:<start|help|problem|submit>``. Submit from Telegram only opens the workspace
    link — evidence (scans) is entered there."""
    expected = settings.derived_secret("telegram")
    if not settings.telegram_configured or not expected or not hmac.compare_digest(secret, expected):
        raise HTTPException(403)
    from omai.telegram_bot import handle_update

    handle_update(s, await request.json())
    return {"ok": True}
