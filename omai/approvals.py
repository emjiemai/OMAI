"""Approvals bound to one exact action.

OMAI may *request* an adjustment, write-off or disposal; it never approves one. The approver must be
someone else than the requester, hold an approver role, and the value must be within their written limit.
The approval is tied to a hash of the exact payload: changing the quantity after approval voids it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy.orm import Session

from omai.config import settings
from omai.ledger import Line, StockError, audit, record
from omai.models import APPROVER_ROLES, Approval, Role, User, now_utc

ACTIONS = {"adjustment", "write_off", "disposal"}


def _hash(action: str, payload: dict) -> str:
    return hashlib.sha256(json.dumps({"a": action, "p": payload}, sort_keys=True, default=str).encode()).hexdigest()


def limit_for(role: Role) -> float:
    return {Role.manager: settings.adjustment_limit_manager,
            Role.accountant: settings.adjustment_limit_accountant,
            Role.owner: float("inf")}.get(role, 0.0)


def request(s: Session, *, action: str, payload: dict, value: float, reason: str, requester: User) -> Approval:
    if action not in ACTIONS:
        raise StockError(f"unknown approval action {action}")
    if not reason.strip():
        raise StockError("a correction needs a recorded cause")
    a = Approval(action=action, payload=payload, payload_hash=_hash(action, payload), value=abs(value),
                 reason=reason, requested_by=requester.id)
    s.add(a)
    s.flush()
    audit(s, requester.id, "approval.request", a.id, kind=action, value=value)
    return a


def decide(s: Session, approval: Approval, approver: User, approve: bool, note: str = "") -> Approval:
    if approval.status != "pending":
        raise StockError(f"approval is already {approval.status}")
    role = Role(approver.role)
    if role not in APPROVER_ROLES:
        raise StockError("you are not an approver")
    if approver.id == approval.requested_by:
        raise StockError("nobody approves their own adjustment")
    payload_actor = approval.payload.get("counted_by") or []
    if approver.id in payload_actor:
        raise StockError("a counter cannot approve the correction of their own count")
    if approve and approval.value > limit_for(role):
        raise StockError(f"{approval.value:,.0f} is above the {role.value} limit — needs a higher approver")
    approval.status = "approved" if approve else "refused"
    approval.decided_by, approval.decided_at = approver.id, now_utc()
    audit(s, approver.id, f"approval.{approval.status}", approval.id, note=note)
    return approval


def execute(s: Session, approval: Approval, actor: User, payload: dict, poster=None,
            event_time: datetime | None = None) -> dict:
    """Do exactly the approved action. ``poster(approval) -> bool`` posts it to the ERP (SAP) when connected."""
    if approval.status != "approved":
        raise StockError("not approved")
    if _hash(approval.action, payload) != approval.payload_hash:
        raise StockError("this is not the action that was approved")
    p = payload
    diff = float(p["diff"])
    if diff > 0:
        line = Line(sku=p["sku"], qty=diff, to_loc=p["loc"], to_bin=p.get("bin", ""), to_cond=p.get("cond", "saleable"),
                    lot=p.get("lot", ""))
    else:
        line = Line(sku=p["sku"], qty=-diff, from_loc=p["loc"], from_bin=p.get("bin", ""),
                    from_cond=p.get("cond", "saleable"), lot=p.get("lot", ""))
    res = record(s, idem_key=f"omai:{approval.action}:{approval.id}", source="omai", kind=approval.action,
                 lines=[line], actor=actor.id, doc_ref=p.get("count_id"), event_time=event_time,
                 note=f"approved {approval.id} by {approval.decided_by}: {approval.reason}")
    ok = poster(approval) if poster else None
    approval.posting_ok = ok
    approval.status = "executed" if ok is not False else "approved"
    audit(s, actor.id, "approval.execute", approval.id, posted=ok, movement=res.movement.id)
    return {"movement": res.movement.id, "posting_ok": ok}
