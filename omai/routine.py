"""The daily routine (Asia/Tashkent), each run keyed by its slot so a restart never runs a slot twice and a
failed slot is retried.

| When              | What |
| every 5 min       | reminders / escalations, send the outbox |
| every 15 min      | sync Billz / 1C (SAP arrives by the gateway push), duplicates & holds, overdue transfers and receipts, system mismatches |
| 09:00             | opening, access and hazard checks → the warehouse lead confirms or reports |
| 09:15             | assign receiving, put-away, dispatch and due counts |
| 11:00, 15:00      | progress review: open tasks to each person, pending transfer receipts |
| 17:30             | reconcile the day's movements |
| 17:50             | closing check (locks, keys, handovers) and one short digest |
| Mon 08:30         | ageing facts and risks to AIPRO |
"""

from __future__ import annotations

import traceback
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from omai.ledger import KIND_UZ
from omai import counting, insights, tasks
from omai.config import settings
from omai.db import session_scope
from omai.models import (OPEN_TASK_STATES, DispatchOrder, Exception_, ExpectedReceipt, Job, Location, Role, Task,
                         User, now_utc)
from omai.notify import deliver_outbox, tell
from omai.sources import freshness

COUNT_CAPACITY = 20   # count lines per location per day (set in approved policy)


def run_slot(name: str, slot: datetime, fn: Callable[[Session], dict | None], factory=None) -> str:
    key = f"{name}:{slot.astimezone(settings.tz):%Y-%m-%dT%H:%M}"
    with session_scope(factory) as s:
        job = s.get(Job, key)
        if job and job.status == "done":
            return "skipped"
        if job and job.status == "running" and now_utc() - _aware(job.started_at) < timedelta(minutes=30):
            return "running"
        if job:
            job.status, job.attempts, job.started_at = "running", job.attempts + 1, now_utc()
        else:
            s.add(Job(key=key))
    try:
        with session_scope(factory) as s:
            fn(s)
        status, err = "done", None
    except Exception:  # noqa: BLE001 — recorded, retried on the next tick
        status, err = "failed", traceback.format_exc()[-2000:]
    with session_scope(factory) as s:
        job = s.get(Job, key)
        job.status, job.finished_at, job.error = status, now_utc(), err
    return status


def _aware(d: datetime) -> datetime:
    from datetime import timezone
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _lead(s: Session, loc: Location) -> User | None:
    if loc.custodian_id:
        return s.get(User, loc.custodian_id)
    return s.scalar(select(User).where(User.location_code == loc.code, User.active.is_(True)))


def _today_at(h: int, m: int) -> datetime:
    return datetime.now(settings.tz).replace(hour=h, minute=m, second=0, microsecond=0)


# ----------------------------------------------------------------- jobs
def every_5(s: Session) -> dict:
    r = tasks.reminder_tick(s)
    r["sent"] = deliver_outbox(s)
    return r


def every_15(s: Session) -> dict:
    from omai.connectors import billz, onec

    out: dict = {}
    for name, ok, fn in (("billz", settings.billz_configured,
                                                                          billz.sync),
                         ("1c", settings.onec_configured, onec.sync)):
        if not ok:
            continue
        try:
            with s.begin_nested():
                out[name] = fn(s)
        except Exception as exc:  # noqa: BLE001 — a failing source must not stop the others
            out[name] = f"error: {exc}"
            from omai.sources import mark_failure
            mark_failure(s, name, str(exc))
    out["mismatches"] = len(insights.reconcile_systems(s))
    out["holds"] = insights.check_holds(s)
    out["late"] = insights.check_transfers_and_receipts(s)
    for src in freshness(s):
        if src["stale"] and src["critical"] and src["last_success"]:
            it = s.scalar(select(User).where(User.role == Role.it.value, User.active.is_(True)))
            tasks_open = s.scalar(select(Task).where(Task.ref == f"recover:{src['source']}",
                                                     Task.state.in_([st.value for st in OPEN_TASK_STATES])))
            if not tasks_open:
                tasks.create(s, kind="recovery", title=f"{src['source']} маълумоти эскирган "
                                                       f"({src['age_minutes']:.0f} дақ.): синхронлашни тиклаш",
                             deadline=now_utc() + timedelta(hours=1), assignee_id=it.id if it else None,
                             ref=f"recover:{src['source']}", accepting_role=Role.it.value, priority=1)
    return out


def opening(s: Session) -> dict:
    n = 0
    for loc in s.scalars(select(Location).where(Location.active.is_(True), Location.kind.in_(["warehouse", "store",
                                                                                              "showroom"]))):
        lead = _lead(s, loc)
        tasks.create(s, kind="safety_open", title=f"{loc.name} очилиш текшируви: кириш, йўлаклар, сақлаш, кимёвий моддалар, "
                                                  f"ускуналар", deadline=_today_at(9, 30),
                     assignee_id=lead.id if lead else None, location=loc.code, ref=f"open:{loc.code}:{now_utc():%F}",
                     result_required="Очилганини тасдиқланг ёки муаммони ёзинг (расм).", priority=2)
        n += 1
    return {"opening_tasks": n}


def assign(s: Session) -> dict:
    out = {"counts": 0, "receiving": 0, "dispatch": 0}
    today_end = _today_at(17, 0)
    for loc in s.scalars(select(Location).where(Location.active.is_(True), Location.movement_source == "omai")):
        lead = _lead(s, loc)
        due = counting.due_items(s, loc.code, now_utc(), COUNT_CAPACITY)
        checker = s.scalar(select(User).where(User.role == Role.checker.value, User.active.is_(True)))
        if due and lead and checker and lead.id != checker.id:
            counting.create(s, location=loc.code, counter=lead, checker=checker, deadline=_today_at(11, 0), skus=due)
            out["counts"] += 1
        for r in s.scalars(select(ExpectedReceipt).where(ExpectedReceipt.location_code == loc.code,
                                                         ExpectedReceipt.status.in_(["expected", "partial"]))):
            if r.due_at and _aware(r.due_at).date() <= datetime.now(settings.tz).date() and \
                    not tasks.open_for_ref(s, f"receive:{r.po_ref}"):
                tasks.create(s, kind="receive", title=f"{r.po_ref} ни қабул қилиш: буюртма, юк хати ва товарни текширинг",
                             deadline=today_end, assignee_id=lead.id if lead else None, location=loc.code,
                             ref=f"receive:{r.po_ref}", result_required="Артикул, миқдор, серия/партияни сканерланг; шикастни ёзинг.")
                out["receiving"] += 1
        for o in s.scalars(select(DispatchOrder).where(DispatchOrder.location_code == loc.code,
                                                       DispatchOrder.status == "open",
                                                       DispatchOrder.release_status == "approved")):
            if not tasks.open_for_ref(s, f"order:{o.order_ref}"):
                tasks.create(s, kind="pick", title=f"{o.order_ref} ни териш, қадоқлаш ва топшириш ({o.customer})",
                             deadline=today_end, assignee_id=lead.id if lead else None, location=loc.code,
                             ref=f"order:{o.order_ref}", result_required="Ячейка ва серия рақамларини сканерланг; топширилгани ҳақида далил.")
                out["dispatch"] += 1
    return out


def progress(s: Session) -> dict:
    n = 0
    open_states = [st.value for st in OPEN_TASK_STATES]
    for u in s.scalars(select(User).where(User.active.is_(True))):
        mine = s.scalars(select(Task).where(Task.assignee_id == u.id, Task.state.in_(open_states))
                         .order_by(Task.priority, Task.deadline)).all()
        if not mine:
            continue
        lines = [f"• {t.title} — {_aware(t.deadline).astimezone(settings.tz):%H:%M}"
                 f"{' ⚠ муддати ўтган' if tasks.is_overdue(t) else ''}" for t in mine[:10]]
        tell(s, u, "Иш ҳолати. Илтимос, далилни топширинг, ёрдам сўранг ёки янги вақт таклиф қилинг:\n" + "\n".join(lines),
             dedupe_key=f"progress:{u.id}:{now_utc():%F%H}")
        n += 1
    return {"people": n}


def reconcile_day(s: Session) -> dict:
    return {"mismatches": len(insights.reconcile_systems(s)), "late": insights.check_transfers_and_receipts(s)}


def closing(s: Session) -> dict:
    for loc in s.scalars(select(Location).where(Location.active.is_(True), Location.kind.in_(["warehouse", "store",
                                                                                              "showroom"]))):
        lead = _lead(s, loc)
        tasks.create(s, kind="safety_close", title=f"{loc.name} ёпилиши: қулфлар, калитлар топширилди, тугалланмаган ишлар "
                                                   f"топширилди", deadline=_today_at(18, 0),
                     assignee_id=lead.id if lead else None, location=loc.code, ref=f"close:{loc.code}:{now_utc():%F}",
                     priority=2)
    text = digest(s)
    for u in s.scalars(select(User).where(User.role.in_([Role.manager.value, Role.owner.value]),
                                          User.active.is_(True))):
        tell(s, u, text, dedupe_key=f"digest:{u.id}:{now_utc():%F}")
    return {"digest": text}


def digest(s: Session) -> str:
    """One short digest: verified, pending, unknown."""
    ex = s.scalars(select(Exception_).where(Exception_.status == "open")).all()
    open_states = [st.value for st in OPEN_TASK_STATES]
    open_tasks = s.scalars(select(Task).where(Task.state.in_(open_states))).all()
    overdue = [t for t in open_tasks if tasks.is_overdue(t)]
    submitted = s.scalars(select(Task).where(Task.state == "submitted")).all()
    stale = [f["source"] for f in freshness(s) if f["stale"] and f["last_success"]]
    by_kind: dict[str, int] = {}
    for e in ex:
        by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
    parts = [f"<b>OMAI — {datetime.now(settings.tz):%d.%m}</b>",
             f"Топшириқлар: {len(open_tasks)} очиқ, {len(overdue)} муддати ўтган, {len(submitted)} қабул кутмоқда",
             "Муаммолар: " + (", ".join(f"{KIND_UZ.get(k, k)} {v}" for k, v in sorted(by_kind.items())) or "йўқ")]
    if stale:
        parts.append("⚠ Номаълум / эскирган: " + ", ".join(stale))
    owner = [e for e in ex if e.owner_decision]
    if owner:
        parts.append(f"Раҳбар қарори керак: {len(owner)}")
    return "\n".join(parts)


def ageing_to_aipro(s: Session) -> dict:
    return {"rows": insights.send_ageing_to_aipro(s)}


SCHEDULE = [
    # (name, cron kwargs, fn)
    ("tick5", {"minute": "*/5"}, every_5),
    ("sync15", {"minute": "*/15"}, every_15),
    ("opening", {"hour": 9, "minute": 0}, opening),
    ("assign", {"hour": 9, "minute": 15}, assign),
    ("progress11", {"hour": 11, "minute": 0}, progress),
    ("progress15", {"hour": 15, "minute": 0}, progress),
    ("reconcile", {"hour": 17, "minute": 30}, reconcile_day),
    ("closing", {"hour": 17, "minute": 50}, closing),
    ("aipro", {"day_of_week": "mon", "hour": 8, "minute": 30}, ageing_to_aipro),
]


def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone=settings.tz)
    for name, cron, fn in SCHEDULE:
        def job(name=name, fn=fn):
            slot = datetime.now(settings.tz).replace(second=0, microsecond=0)
            run_slot(name, slot, fn)
        sched.add_job(job, "cron", id=name, misfire_grace_time=600, coalesce=True, max_instances=1, **cron)
    sched.start()
    return sched


def run_now(name: str) -> str:
    fn = {n: f for n, _, f in SCHEDULE}[name]
    return run_slot(name, datetime.now(settings.tz).replace(second=0, microsecond=0), fn)
