"""A full count-day rehearsal on YOUR computer, with real data and a separate test bot. Production is not touched.

    DATABASE_URL=sqlite:///rehearsal.db .venv/bin/python -m scripts.rehearsal

It builds ``rehearsal.db`` (a local file) from the live systems — read-only, the same reads the server makes:
the 18 SAP warehouses, items from 1C and SAP, SAP's and 1C's stock, the four 1C склады linked like on the server.
Then three test people for your three Telegram accounts — ``boss`` (раҳбар), ``ombor`` (омборчи, Otabek's part) and
``tekshir`` (текширувчи) — one PIN for all three, asked here and never shown. Then count day: 01 is switched to
OMAI (opening = SAP's stock at 01), shelves G01–G10 and FLOOR are added, and the baseline count goes to ``ombor``.

Needs in the local ``.env``: a TEST bot's token as ``OMAI_TELEGRAM_BOT_TOKEN`` (never the production bot), the 1C
variables, and ``MGMG_SAP_READER_URL`` (omai_reader with mgmg-db's External host). Run it again to start over.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from getpass import getpass

from omai.config import settings

LINKS = {"01": "cce7ceea", "07": "61cc1d9b", "09": "0f127f40", "13": "5b2d6b42"}   # as on the server, 24.11
PEOPLE = (("boss", "Синов раҳбар", "owner"), ("ombor", "Синов омборчи", "staff"), ("tekshir", "Синов текширувчи", "checker"))


def main() -> None:
    url = settings.database_url
    if settings.environment == "production" or not url.startswith("sqlite") or not url.endswith("rehearsal.db"):
        raise SystemExit("Run it only on your computer, like this:\n"
                         "  DATABASE_URL=sqlite:///rehearsal.db .venv/bin/python -m scripts.rehearsal")
    if not (settings.onec_configured and settings.mgmg_feed_configured):
        raise SystemExit("The local .env needs the 1C variables and MGMG_SAP_READER_URL (see the top of this file)")
    pin = getpass("One PIN for the three test people (4+ digits): ")
    if not (pin.isdigit() and len(pin) >= 4) or pin != getpass("PIN again: "):
        raise SystemExit("PINs differ or too short")

    path = url.split("sqlite:///", 1)[1]
    if os.path.exists(path):
        os.remove(path)                                   # a rehearsal starts from nothing

    from omai import auth
    from omai.connectors import mgmg_feed, onec
    from omai.connectors.onec import OneC
    from omai.db import init_db, session_scope
    from omai.models import Location, User
    from scripts import count_day, load_master

    init_db()
    print("1/5 warehouses and items (1C, then SAP) …", flush=True)
    positions, catalog, whs = load_master.onec_snapshot()
    good, _plan = load_master.plan_items(positions, catalog)
    with session_scope() as s:
        print("   warehouses:", load_master.load_warehouses(s))
        print("   items from 1C:", load_master.load_items(s, good))
        for code, prefix in LINKS.items():
            key = next((k for k in whs if k.startswith(prefix)), None)
            if key:
                s.get(Location, code).onec_ref = key
        out = mgmg_feed.sync(s)
        print(f"   SAP: items {out['items']}, stock rows {out['stock']['rows']}, last push "
              f"{out['pushed_minutes_ago']} min ago")
    print("2/5 1C stock …", flush=True)
    with session_scope() as s:
        client = OneC()
        try:
            r = onec.sync(s, client)
        finally:
            client.close()
        print(f"   1C: {r['mapped']} item × склад balances")
    print("3/5 test people …", flush=True)
    with session_scope() as s:
        for login, name, role in PEOPLE:
            s.add(User(id=login, login=login, name=name, role=role, pin_hash=auth.hash_pin(pin),
                       location_code="01", manager_id="boss", workdays="7"))
    print("4/5 count day: 01 to OMAI, shelves …", flush=True)
    load_master.pilot("01", apply=True)
    with session_scope() as s:
        print("   shelves:", count_day.add_bins(s, "01", ["G01-G10", "FLOOR"]))
    print("5/5 the baseline count …", flush=True)
    with session_scope() as s:
        deadline = (datetime.now(settings.tz) + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        c = count_day.baseline(s, "01", "ombor", "tekshir", deadline)
        print(f"   count {c.id[:8]}: {len(c.lines)} items for ombor, checker tekshir")
    print("\nReady. In two terminals:\n"
          "  DATABASE_URL=sqlite:///rehearsal.db .venv/bin/uvicorn omai.api.app:app --port 8000\n"
          "  DATABASE_URL=sqlite:///rehearsal.db .venv/bin/python -m omai.telegram_bot\n"
          "Then in Telegram, open the TEST bot from each account: /login boss <PIN>, /login ombor <PIN>, "
          "/login tekshir <PIN>.")


if __name__ == "__main__":
    sys.exit(main())
