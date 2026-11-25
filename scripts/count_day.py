"""Count day for the pilot warehouse: shelves, the first (baseline) count, and where counts stand.

    python -m scripts.count_day bins 01 G01-G20 FLOOR          # add shelves (a range, or single names)
    python -m scripts.count_day count 01 otabek dilshod 2026-11-28T18:00
                                                                # the baseline count: counter, checker, deadline
    python -m scripts.count_day show 01                         # the warehouse's counts (counts only)

Run in the omai-api Shell. Order on count day: ``load_master --pilot 01 --apply`` first (OMAI's book starts from
SAP's stock), then ``count``.

The baseline count is **by item across the whole warehouse**: after the switch every unit sits in OMAI without a
shelf (bin ""), so the count lists each item once, "anywhere in 01", and never shows a quantity (blind form). The
counter gets the task in Telegram; the checker (a different person) evaluates; differences go the normal way —
independent recount, cause, approval. Putting the counted stock onto shelves comes after the baseline.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime

from sqlalchemy import select

from omai.config import settings
from omai.db import init_db, session_scope
from omai.models import Bin, Count, Location, User

OPEN = ("open", "counting")


def expand(specs: list[str]) -> list[str]:
    """``G01-G20`` → G01 … G20 (padding kept); anything else as given, upper case."""
    out: list[str] = []
    for spec in specs:
        m = re.fullmatch(r"([A-Za-z]*)(\d+)-\1?(\d+)", spec.strip())
        if m:
            prefix, a, b = m.group(1).upper(), m.group(2), m.group(3)
            width = len(a) if a.startswith("0") or len(a) == len(b) else 0
            for n in range(int(a), int(b) + 1):
                out.append(f"{prefix}{n:0{width}d}")
        elif spec.strip():
            out.append(spec.strip().upper())
    return list(dict.fromkeys(out))


def add_bins(s, code: str, specs: list[str]) -> dict:
    if s.get(Location, code) is None:
        raise SystemExit(f"no warehouse {code}")
    have = {b.code for b in s.scalars(select(Bin).where(Bin.location_code == code))}
    names = expand(specs)
    for name in names:
        if name not in have:
            s.add(Bin(location_code=code, code=name[:40]))
    s.flush()
    return {"asked": len(names), "added": len([n for n in names if n not in have]), "total": len(have | set(names))}


def baseline(s, code: str, counter_login: str, checker_login: str, deadline: datetime) -> Count:
    from omai import counting

    loc = s.get(Location, code)
    if loc is None:
        raise SystemExit(f"no warehouse {code}")
    if loc.movement_source != "omai":
        raise SystemExit(f"{code} is not run by OMAI yet: run  python -m scripts.load_master --pilot {code} --apply")
    if s.scalar(select(Count).where(Count.location_code == code, Count.status.in_(OPEN))):
        raise SystemExit(f"{code} already has an open count: see  python -m scripts.count_day show {code}")
    people = {}
    for role, login in (("counter", counter_login), ("checker", checker_login)):
        u = s.scalar(select(User).where(User.login == login.lower(), User.active.is_(True)))
        if u is None:
            raise SystemExit(f"{role} {login!r}: no active person with this login (add them on /admin)")
        people[role] = u
    if people["counter"].id == people["checker"].id:
        raise SystemExit("the checker must be a different person from the counter")
    c = counting.create(s, location=code, counter=people["counter"], checker=people["checker"], deadline=deadline,
                        kind="baseline", bins=[""])
    if not c.lines:
        raise SystemExit(f"{code} holds nothing in OMAI: nothing to count")
    return c


def show(s, code: str) -> list[str]:
    rows = []
    for c in s.scalars(select(Count).where(Count.location_code == code).order_by(Count.deadline.desc())):
        pending = sum(1 for line in c.lines if line.status in ("pending", "recount"))
        rows.append(f"{c.id[:8]}  {c.kind}  {c.status}  lines={len(c.lines)} to_count={pending}  "
                    f"deadline={c.deadline.astimezone(settings.tz):%d.%m %H:%M}")
    return rows or [f"{code}: no counts"]


def main() -> None:
    args = sys.argv[1:]
    init_db()
    with session_scope() as s:
        if len(args) >= 3 and args[0] == "bins":
            print(f"{args[1]} shelves:", add_bins(s, args[1], args[2:]))
        elif len(args) == 5 and args[0] == "count":
            deadline = datetime.fromisoformat(args[4])
            deadline = deadline if deadline.tzinfo else deadline.replace(tzinfo=settings.tz)
            c = baseline(s, args[1], args[2], args[3], deadline)
            print(f"baseline count {c.id[:8]} for {args[1]}: {len(c.lines)} items to count; "
                  f"the counter got the task in Telegram")
        elif len(args) == 2 and args[0] == "show":
            print("\n".join(show(s, args[1])))
        else:
            print(__doc__)


if __name__ == "__main__":
    main()
