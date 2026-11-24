"""Load OMAI's warehouses (SAP's own codes) and items (from 1C), and link each warehouse to its 1C склад / Billz shop.

    python -m scripts.load_master                      # look only: counts, 1C склады, Billz shops; writes nothing
    python -m scripts.load_master --apply              # create the SAP warehouses and the 1C items
    python -m scripts.load_master --link 01 onec=<key>     # warehouse 01 = this 1C склад (first 8+ chars of its key)
    python -m scripts.load_master --link G.A._01 billz=<shop id>

Run it where the 1C / Billz variables are set (the Render Shell of omai-api). Read-only towards 1C and Billz (GET,
plus Billz's login). Prints counts and the склад / shop names needed for linking — never quantities or amounts.

* Warehouses: the live SAP warehouse list (docs/backend.md §3). OMAI code = SAP ``WhsCode``. ``movement_source``
  stays ``sap`` (the model default): nothing is booked until SAP pushes, and the pilot warehouse is switched to
  ``omai`` when its baseline count starts.
* Items: every 1C item that has stock now on the stock accounts (29xx / 10xx / 28xx). The SKU is its «Артикул»,
  taken as the SAP ``ItemCode`` (the rule ``omai/connectors/onec.py`` already uses). Items without an Артикул, or
  Several 1C cards with one Артикул become one item (see ``plan_items``). Items without an Артикул are **not**
  loaded, only counted: the 1C ↔ SAP item mapping for them is the owner's to settle with finance. Re-running is safe: existing items keep their data, empty fields are filled.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime

from sqlalchemy import select

from omai.config import settings
from omai.db import init_db, session_scope
from omai.models import Item, Location

# SAP WhsCode → (name, kind). Names are user-facing (Uzbek Cyrillic). Stock held outside a warehouse by a person or
# a customer (15, 16) is "technician"; goods at customs (02) are "staging".
WAREHOUSES: dict[str, tuple[str, str]] = {
    "01": ("Асосий омборхона", "warehouse"),
    "02": ("Божхона", "staging"),
    "03": ("Гармин-Бодомзор", "store"),
    "04": ("Гармин-Абай", "store"),
    "05": ("Гармин-Минор", "store"),
    "06": ("Носоз маҳсулот (Гармин)", "warehouse"),
    "07": ("Эҳтиёт қисмлар", "warehouse"),
    "08": ("Шоурум", "showroom"),
    "09": ("Гармин заҳиралари (Танита)", "warehouse"),
    "10": ("Гармин Амбасадор", "store"),
    "11": ("Лондри Юнусобод", "store"),
    "12": ("Лондри Олмазор", "store"),
    "13": ("Спотх", "warehouse"),
    "15": ("Сотувчи заҳираси", "technician"),
    "16": ("Примус: ижарадаги товарлар", "technician"),
    "20": ("Инвентарлар", "warehouse"),
    "G.A._01": ("Гармин Абай (G.A._01)", "store"),
    "G.A._02": ("Гармин Танита (G.A._02)", "store"),
}


def load_warehouses(s) -> dict:
    out = {"created": 0, "kept": 0}
    if s.get(Location, "TRANSIT") is None:
        s.add(Location(code="TRANSIT", name="Йўлда", kind="transit"))
    for code, (name, kind) in WAREHOUSES.items():
        loc = s.get(Location, code)
        if loc is None:
            s.add(Location(code=code, name=name, kind=kind, sap_whs=code))
            out["created"] += 1
        else:
            loc.sap_whs = loc.sap_whs or code
            out["kept"] += 1
    s.flush()
    return out


def onec_snapshot() -> tuple[dict, list[dict], dict[str, int]]:
    """(1C stock positions, item catalog rows, склад key → number of stocked items there). GET only."""
    from omai.connectors.onec import ITEMS, OneC, stock_rows
    from omai.connectors.onec import WAREHOUSES as ONEC_WHS

    client = OneC()
    try:
        positions = {k: q for k, q in stock_rows(client, datetime.now(settings.tz)).items() if q}
        catalog = client.listing(ITEMS, "Ref_Key,Code,Description,Артикул", 90)
        whs = client.listing(ONEC_WHS, "Ref_Key,Description", 90)
    finally:
        client.close()
    per_whs = Counter(w for (w, _item) in positions)
    return positions, catalog, {str(w["Ref_Key"]): (str(w.get("Description") or "").strip(), per_whs[str(w["Ref_Key"])])
                                for w in whs}


def plan_items(positions: dict, catalog: list[dict]) -> tuple[list[dict], dict]:
    """One OMAI item per Артикул. When several 1C cards carry the same Артикул (1C keeps 2–5 cards for some Garmin
    part numbers), they are the one SAP item: loaded once, without ``onec_ref``, so the 1C sync maps them all by
    Артикул and adds their quantities."""
    stocked = {ref for (_w, ref) in positions}
    by_art: dict[str, list[dict]] = {}
    out = {"stocked_in_1c": len(stocked), "no_article": 0, "too_long": 0}
    for r in sorted((r for r in catalog if str(r["Ref_Key"]) in stocked), key=lambda r: str(r.get("Code") or "")):
        art = str(r.get("Артикул") or "").strip()
        if not art:
            out["no_article"] += 1
        elif len(art) > 60:
            out["too_long"] += 1
        else:
            by_art.setdefault(art, []).append(r)
    good = [{"sku": art, "name": (str(rs[0].get("Description") or "").strip() or art)[:300],
             "onec_ref": str(rs[0]["Ref_Key"]) if len(rs) == 1 else None} for art, rs in by_art.items()]
    out["items_one_1c_card"] = sum(1 for g in good if g["onec_ref"])
    out["items_several_1c_cards"] = sum(1 for g in good if not g["onec_ref"])
    out["loadable"] = len(good)
    return good, out


def load_items(s, good: list[dict]) -> dict:
    out = {"created": 0, "updated": 0, "kept": 0, "other_1c_ref": 0}
    for g in good:
        item = s.get(Item, g["sku"])
        if item is None:
            s.add(Item(sku=g["sku"], name=g["name"], sap_code=g["sku"], onec_ref=g["onec_ref"]))
            out["created"] += 1
        elif g["onec_ref"] and item.onec_ref and item.onec_ref != g["onec_ref"]:
            out["other_1c_ref"] += 1        # set by hand to another 1C item: keep it
        elif (g["onec_ref"] and not item.onec_ref) or not item.sap_code:
            item.onec_ref, item.sap_code = item.onec_ref or g["onec_ref"], item.sap_code or g["sku"]
            out["updated"] += 1
        else:
            out["kept"] += 1
    return out


def billz_shops() -> list[tuple[str, str]]:
    from omai.connectors.billz import Billz

    client = Billz()
    try:
        data = client.get("/v1/shop", {"limit": 100})
    finally:
        client.close()
    shops = data.get("shops") or (data.get("data") or {}).get("shops") or data.get("data") or []
    return [(str(x.get("id")), str(x.get("name") or "")) for x in shops if isinstance(x, dict)]


def link(code: str, target: str) -> None:
    system, _, value = target.partition("=")
    if system not in ("onec", "billz") or not value:
        raise SystemExit("use onec=<1C склад key> or billz=<Billz shop id>")
    with session_scope() as s:
        loc = s.get(Location, code)
        if loc is None:
            raise SystemExit(f"no warehouse {code}: run --apply first")
        if system == "onec":
            _p, _c, whs = onec_snapshot()
            hits = [k for k in whs if k.startswith(value)]
            if len(hits) != 1 or len(value) < 8:
                raise SystemExit(f"{len(hits)} 1C склады start with {value!r}: give 8+ characters of one key")
            loc.onec_ref = hits[0]
            print(f"{code} = 1C «{whs[hits[0]][0]}»")
        else:
            loc.billz_shop_id = value
            print(f"{code} = Billz shop {value}")


def main() -> None:
    args = sys.argv[1:]
    init_db()
    if len(args) == 3 and args[0] == "--link":
        link(args[1], args[2])
        return
    if args not in ([], ["--apply"]):
        print(__doc__)
        return
    apply = args == ["--apply"]
    print(f"SAP warehouses: {len(WAREHOUSES)}")
    good, items_plan = [], {}
    if settings.onec_configured:
        positions, catalog, whs = onec_snapshot()
        good, items_plan = plan_items(positions, catalog)
        print("1C items:", ", ".join(f"{k}={v}" for k, v in items_plan.items()))
        with session_scope() as s:
            linked = {l.onec_ref: l.code for l in s.scalars(select(Location).where(Location.onec_ref.is_not(None)))}
        print("1C склады (key → name, stocked items, linked to):")
        for key, (name, n) in sorted(whs.items(), key=lambda kv: -kv[1][1]):
            print(f"  {key[:8]}  {name}  items={n}  -> {linked.get(key, '-')}")
    else:
        print("1C: not configured here, items skipped")
    if settings.billz_configured:
        with session_scope() as s:
            linked = {l.billz_shop_id: l.code for l in s.scalars(select(Location).where(Location.billz_shop_id.is_not(None)))}
        try:
            shops = billz_shops()
        except Exception as exc:  # noqa: BLE001  (a Billz outage must not stop the load)
            print(f"Billz shops: not read ({type(exc).__name__}: {str(exc)[:80]})")
            shops = []
        if shops:
            print("Billz shops (id → name, linked to):")
        for sid, name in shops:
            print(f"  {sid}  {name}  -> {linked.get(sid, '-')}")
    if not apply:
        print("Nothing written. Add --apply to load.")
        return
    with session_scope() as s:
        print("warehouses:", load_warehouses(s))
        print("items:", load_items(s, good))


if __name__ == "__main__":
    main()
