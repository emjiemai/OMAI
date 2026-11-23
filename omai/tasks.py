"""Tasks, their states, reminders and escalation.

States: assigned → in_progress → submitted → accepted, with blocked and recount_required on the way.
Overdue is a flag (deadline passed, not accepted), not a state. One reminder before the deadline and one
after, both inside the assignee's shift; unresolved work is escalated to the manager. Urgent incidents
(safety, unauthorised release) skip shifts and go to the on-call person at once.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from omai.config import settings
from omai.ledger import audit
from omai.models import OPEN_TASK_STATES, Role, Task, TaskState, User, now_utc
from omai.notify import task_buttons, tell

REMIND_BEFORE = timedelta(minutes=30)
ESCALATE_AFTER = timedelta(hours=1)

ACTIONS = {
    # action: (allowed from-states, to-state)
    "start": ({TaskState.assigned, TaskState.blocked, TaskState.recount_required}, TaskState.in_progress),
    "help": (OPEN_TASK_STATES, TaskState.blocked),
    "problem": (OPEN_TASK_STATES, TaskState.blocked),
    "submit": ({TaskState.assigned, TaskState.in_progress, TaskState.recount_required}, TaskState.submitted),
    "accept": ({TaskState.submitted}, TaskState.accepted),
    "recount": ({TaskState.submitted}, TaskState.recount_required),
    "reopen": ({TaskState.submitted}, TaskState.in_progress),
    "cancel": (OPEN_TASK_STATES | {TaskState.submitted}, TaskState.cancelled),
}


class TaskError(ValueError):
    pass


def create(s: Session, *, kind: str, title: str, deadline: datetime, assignee_id: str | None,
           location: str | None = None, priority: int = 3, ref: str | None = None, result_required: str = "",
           accepting_role: str = Role.manager.value, urgent: bool = False, notify: bool = True) -> Task:
    t = Task(kind=kind, title=title, deadline=deadline, original_deadline=deadline, assignee_id=assignee_id,
             location_code=location, priority=1 if urgent else priority, ref=ref, result_required=result_required,
             accepting_role=accepting_role, urgent=urgent)
    s.add(t)
    s.flush()
    audit(s, None, "task.create", t.id, kind=kind, assignee=assignee_id, deadline=deadline)
    if notify:
        user = s.get(User, assignee_id) if assignee_id else None
        local = deadline.astimezone(settings.tz).strftime("%H:%M %d.%m")
        tell(s, user, f"<b>{title}</b>\nDue {local}. {result_required}".strip(), task_buttons(t.id),
             dedupe_key=f"task:{t.id}:new")
        if urgent:
            escalate_now(s, t, "urgent")
    return t


def open_for_ref(s: Session, ref: str, kind: str | None = None) -> list[Task]:
    q = select(Task).where(Task.ref == ref, Task.state.in_([st.value for st in OPEN_TASK_STATES | {TaskState.submitted}]))
    if kind:
        q = q.where(Task.kind == kind)
    return list(s.scalars(q))


def act(s: Session, task: Task, action: str, actor: User, note: str = "", evidence: dict | None = None) -> Task:
    if action not in ACTIONS:
        raise TaskError(f"unknown action {action}")
    allowed, target = ACTIONS[action]
    state = TaskState(task.state)
    if state not in allowed:
        raise TaskError(f"cannot {action} a task that is {state.value}")
    if action in ("accept", "recount", "reopen"):
        if actor.id == task.assignee_id:
            raise TaskError("the person who did the work cannot accept it")
        if actor.role != task.accepting_role and Role(actor.role) not in (Role.manager, Role.owner):
            raise TaskError(f"accepting this task needs the {task.accepting_role} role")
        task.accepted_by = actor.id if action == "accept" else None
    elif action != "cancel" and actor.id != task.assignee_id and Role(actor.role) not in (Role.manager, Role.owner):
        raise TaskError("only the assignee or a manager can do this")
    if action == "submit" and evidence is None and not task.evidence:
        raise TaskError("submit needs evidence (scans, photos, count form)")
    if evidence:
        task.evidence = [*task.evidence, {**evidence, "by": actor.id, "at": now_utc().isoformat()}]
    if action in ("help", "problem"):
        task.blocker = note or action
        mgr = _manager_of(s, task)
        tell(s, mgr, f"⚠ <b>{actor.name}</b>: {action} on «{task.title}»\n{note}", dedupe_key=None)
    task.state = target.value
    task.updated_at = now_utc()
    audit(s, actor.id, f"task.{action}", task.id, note=note)
    if action == "accept":
        assignee = s.get(User, task.assignee_id) if task.assignee_id else None
        tell(s, assignee, f"✅ Thank you — «{task.title}» is accepted.", dedupe_key=f"task:{task.id}:thanks")
    return task


def reschedule(s: Session, task: Task, new_deadline: datetime, actor: User, reason: str) -> Task:
    """A revised deadline keeps the original one (count discipline is measured against the original)."""
    if Role(actor.role) not in (Role.manager, Role.owner):
        raise TaskError("only a manager approves a revised deadline")
    task.deadline = new_deadline
    task.reminded_before = task.reminded_after = False
    audit(s, actor.id, "task.reschedule", task.id, new=new_deadline, original=task.original_deadline, reason=reason)
    return task


def is_overdue(task: Task, now: datetime | None = None) -> bool:
    now = now or now_utc()
    return TaskState(task.state) in OPEN_TASK_STATES and _aware(task.deadline) < now


def in_shift(user: User | None, at: datetime) -> bool:
    if user is None:
        return False
    local = at.astimezone(settings.tz)
    if "7" not in user.workdays and str(local.weekday()) not in user.workdays:
        return False
    return user.shift_start <= local.time() < user.shift_end


def _aware(d: datetime) -> datetime:
    from datetime import timezone
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _manager_of(s: Session, task: Task) -> User | None:
    assignee = s.get(User, task.assignee_id) if task.assignee_id else None
    if assignee and assignee.manager_id:
        return s.get(User, assignee.manager_id)
    return s.scalar(select(User).where(User.role == Role.manager.value, User.active.is_(True)))


def _on_call(s: Session) -> User | None:
    return s.scalar(select(User).where(User.on_call.is_(True), User.active.is_(True)))


def escalate_now(s: Session, task: Task, why: str) -> None:
    for person in {p.id: p for p in (_on_call(s), _manager_of(s, task)) if p}.values():
        tell(s, person, f"🚨 <b>Urgent</b> ({why}): {task.title}", task_buttons(task.id),
             dedupe_key=f"task:{task.id}:urgent:{person.id}")
    task.escalated_at = now_utc()


def reminder_tick(s: Session, now: datetime | None = None) -> dict:
    """Run every few minutes. Accepted or submitted work gets no reminder."""
    now = now or now_utc()
    stats = {"before": 0, "after": 0, "escalated": 0, "deferred": 0}
    open_states = [st.value for st in OPEN_TASK_STATES]
    for t in s.scalars(select(Task).where(Task.state.in_(open_states))):
        deadline = _aware(t.deadline)
        user = s.get(User, t.assignee_id) if t.assignee_id else None
        if not t.reminded_before and deadline - REMIND_BEFORE <= now < deadline:
            if in_shift(user, now):
                tell(s, user, f"⏰ Due at {deadline.astimezone(settings.tz):%H:%M}: {t.title}", task_buttons(t.id),
                     dedupe_key=f"task:{t.id}:before")
                t.reminded_before = True
                stats["before"] += 1
            else:
                stats["deferred"] += 1
        if now >= deadline and not t.reminded_after:
            if in_shift(user, now) or t.urgent:
                tell(s, user, f"❗ Overdue: {t.title}. Submit, or tell us what blocks it.", task_buttons(t.id),
                     dedupe_key=f"task:{t.id}:after")
                t.reminded_after = True
                stats["after"] += 1
            else:
                stats["deferred"] += 1
        if now >= deadline + ESCALATE_AFTER and t.escalated_at is None:
            mgr = _manager_of(s, t)
            if t.urgent or in_shift(mgr, now):
                name = user.name if user else "unassigned"
                tell(s, mgr, f"⬆ Escalation: «{t.title}» ({name}) is overdue since "
                             f"{deadline.astimezone(settings.tz):%H:%M %d.%m}, state {t.state}.",
                     task_buttons(t.id), dedupe_key=f"task:{t.id}:escalate")
                t.escalated_at = now
                stats["escalated"] += 1
                audit(s, None, "task.escalate", t.id, to=mgr.id if mgr else None)
            else:
                stats["deferred"] += 1
    return stats
