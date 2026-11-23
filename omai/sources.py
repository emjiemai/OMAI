"""Source freshness. Stale critical data stops unsupported digital releases (with a controlled manual fallback)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from omai.config import SYNC_STALE_MINUTES
from omai.ledger import raise_exception, resolve_exception
from omai.models import SourceSync, now_utc

SOURCES = {"sap": True, "1c": False, "billz": True}   # critical?


def _row(s: Session, source: str) -> SourceSync:
    row = s.get(SourceSync, source)
    if row is None:
        row = SourceSync(source=source, critical=SOURCES.get(source, False))
        s.add(row)
        s.flush()
    return row


def mark_success(s: Session, source: str, cursor: str | None = None, at: datetime | None = None) -> None:
    row = _row(s, source)
    row.last_attempt = row.last_success = at or now_utc()
    row.last_error, row.failures = None, 0
    if cursor is not None:
        row.cursor = cursor
    resolve_exception(s, "source_down", source, "sync recovered")


def mark_failure(s: Session, source: str, error: str, at: datetime | None = None) -> None:
    row = _row(s, source)
    row.last_attempt = at or now_utc()
    row.last_error = error[:1000]
    row.failures += 1
    if row.failures >= 2:
        raise_exception(s, "source_down", source, f"{source} синхронлаш ишламаяпти ({row.failures}×): {error[:200]}",
                        severity="high" if row.critical else "medium")


def age_minutes(s: Session, source: str, now: datetime | None = None) -> float | None:
    row = s.get(SourceSync, source)
    if row is None or row.last_success is None:
        return None
    last = row.last_success if row.last_success.tzinfo else row.last_success.replace(tzinfo=timezone.utc)
    return ((now or now_utc()) - last).total_seconds() / 60


def is_stale(s: Session, source: str, now: datetime | None = None) -> bool:
    age = age_minutes(s, source, now)
    return age is None or age > SYNC_STALE_MINUTES


def stale_critical(s: Session, now: datetime | None = None, configured_only: bool = True) -> list[str]:
    """Critical sources that are stale. A source never set up (no row) only counts when ``configured_only`` is off."""
    out = []
    for src, critical in SOURCES.items():
        if not critical:
            continue
        if configured_only and s.get(SourceSync, src) is None:
            continue
        if is_stale(s, src, now):
            out.append(src)
    return out


def freshness(s: Session, now: datetime | None = None) -> list[dict]:
    rows = []
    for src in SOURCES:
        r = s.get(SourceSync, src)
        age = age_minutes(s, src, now)
        rows.append({"source": src, "critical": SOURCES[src],
                     "last_success": r.last_success.isoformat() if r and r.last_success else None,
                     "age_minutes": round(age, 1) if age is not None else None,
                     "stale": is_stale(s, src, now), "last_error": r.last_error if r else "not connected",
                     "failures": r.failures if r else 0})
    return rows


def recent(minutes: int) -> datetime:
    return now_utc() - timedelta(minutes=minutes)
