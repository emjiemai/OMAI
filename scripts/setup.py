"""Create tables and load locations, bins, items and people from an Excel workbook (the location & SKU mapping).

    python -m scripts.setup mapping.xlsx            # sheets: locations, bins, items, users
    python -m scripts.setup --pin otabek 4821       # set a login PIN
    python -m scripts.setup --add ulugbek "Улуғбек" owner    # add one person; asks for the PIN (not echoed)

Roles: staff, custodian, checker, manager, accountant, owner, it.

Sheet columns are the model fields (omai/models.py), e.g. locations: code, name, kind, sap_whs, billz_shop_id,
movement_source; users: id, name, role, login, telegram_chat_id, location_code, manager_id. A TRANSIT location is
always created.
"""

from __future__ import annotations

import sys

from omai import auth
from omai.db import init_db, session_scope
from omai.models import Bin, Item, Location, User

MODELS = {"locations": Location, "bins": Bin, "items": Item, "users": User}


def load(path: str) -> None:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True)
    with session_scope() as s:
        if s.get(Location, "TRANSIT") is None:
            s.add(Location(code="TRANSIT", name="In transit", kind="transit"))
        for sheet, model in MODELS.items():
            if sheet not in wb.sheetnames:
                continue
            rows = list(wb[sheet].iter_rows(values_only=True))
            head = [str(h).strip() for h in rows[0]]
            for r in rows[1:]:
                data = {k: v for k, v in zip(head, r) if v is not None}
                if data:
                    s.merge(model(**data))
            s.flush()
            print(f"{sheet}: {len(rows) - 1}")


def add(uid: str, name: str, role: str) -> None:
    from getpass import getpass

    from omai.models import Role

    Role(role)   # unknown role → error
    pin = getpass("PIN (4+ digits): ")
    if len(pin) < 4 or pin != getpass("PIN again: "):
        raise SystemExit("PINs differ or too short")
    with session_scope() as s:
        u = s.get(User, uid) or User(id=uid, name=name, role=role)
        u.name, u.role, u.login, u.pin_hash, u.active = name, role, uid, auth.hash_pin(pin), True
        s.merge(u)
    print(f"{uid} ({role}) ready: in Telegram send /login {uid} <PIN>")


def main() -> None:
    init_db()
    if len(sys.argv) == 5 and sys.argv[1] == "--add":
        add(sys.argv[2], sys.argv[3], sys.argv[4])
    elif len(sys.argv) == 4 and sys.argv[1] == "--pin":
        with session_scope() as s:
            u = s.get(User, sys.argv[2])
            u.login = u.login or u.id
            u.pin_hash = auth.hash_pin(sys.argv[3])
        print("PIN set")
    elif len(sys.argv) == 2:
        load(sys.argv[1])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
