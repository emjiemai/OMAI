from __future__ import annotations

from datetime import datetime

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from omai.config import settings
from omai.connectors import billz, sap_b1
from omai.ledger import balance


def test_sap_gateway_push(s):
    out = sap_b1.ingest_payload(s, {"kind": "stock_value", "rows": [{"ItemCode": "TAN1", "WhsCode": "01", "OnHand": 4}]})
    assert out["rows"] == 1 and out["mapped"] == 1


def test_sap_stock_complete_push_zeroes_what_sap_no_longer_lists(s):
    from omai.models import ExternalBalance

    rows = [{"ItemCode": "TAN1", "WhsCode": "01", "OnHand": 4}, {"ItemCode": "GAR-F8", "WhsCode": "01", "OnHand": 2}]
    sap_b1.ingest_kind(s, "stock_value", {"rows": rows, "complete": True})
    out = sap_b1.ingest_kind(s, "stock_value", {"rows": rows[:1], "complete": True})   # F8 sold out in SAP
    qty = {b.sku: b.qty for b in s.scalars(select(ExternalBalance).where(ExternalBalance.source == "sap"))}
    assert qty == {"TANITA1": 4, "FENIX8": 0} and out["zeroed"] == 1
    sap_b1.ingest_kind(s, "stock_value", {"rows": [], "complete": False})                # a cut push changes nothing
    assert s.scalar(select(ExternalBalance).where(ExternalBalance.sku == "TANITA1")).qty == 4


def test_sap_gateway_rows_book_and_cancel(s):
    """HANA rows (DocEntry / LineNum / WhsCode …), booked only where SAP is the location's authority; a
    cancellation document ('C') undoes the original ('Y') whatever order they come in."""
    from omai.models import Location

    s.get(Location, "WH1").movement_source = "sap"
    receipt = {"DocEntry": 55, "DocNum": 1055, "DocDate": "2026-11-20T00:00:00.000Z", "CANCELED": "N",
               "LineNum": 0, "ItemCode": "TAN1", "Quantity": 5, "WhsCode": "01", "CardName": "Supplier"}
    out = sap_b1.ingest_kind(s, "goods_receipts", {"rows": [receipt], "complete": True})
    again = sap_b1.ingest_kind(s, "goods_receipts", {"rows": [receipt], "complete": True})
    assert out["booked"] == 1 and again["booked"] == 0
    assert balance(s, sku="TANITA1", loc="WH1", cond="saleable") == 5

    issue = {"ObjType": 60, "DocEntry": 7, "DocNum": 7, "DocDate": "2026-11-21", "CANCELED": "Y",
             "LineNum": 0, "ItemCode": "TAN1", "Quantity": 2, "WhsCode": "01"}
    cancel = {**issue, "DocEntry": 8, "DocNum": 8, "CANCELED": "C"}
    sap_b1.ingest_kind(s, "inventory_moves", {"rows": [cancel, issue], "complete": True})
    assert balance(s, sku="TANITA1", loc="WH1", cond="saleable") == 5          # issued 2, cancelled: back to 5

    transfer = {"DocEntry": 9, "DocNum": 9, "DocDate": "2026-11-21", "CANCELED": "N", "LineNum": 0,
                "ItemCode": "TAN1", "Quantity": 1, "FromWhsCod": "01", "WhsCode": "G05"}
    out = sap_b1.ingest_kind(s, "stock_transfers", {"rows": [transfer], "complete": True})
    assert out["booked"] == 1 and balance(s, sku="TANITA1", loc="WH1", cond="saleable") == 4


def test_sap_data_endpoint(s, monkeypatch):
    from omai.api.app import app, db

    monkeypatch.setattr(settings, "sap_push_webhook_secret", type(settings.sap_push_webhook_secret)("sec"))
    app.dependency_overrides[db] = lambda: s
    try:
        c = TestClient(app)
        body = {"rows": [{"ItemCode": "TAN1", "WhsCode": "01", "OnHand": 3}], "complete": False}
        assert c.post("/sap/data/stock_value", json=body).status_code == 403
        r = c.post("/sap/data/stock_value", json=body, headers={"X-Omai-Secret": "sec"})
        assert r.status_code == 200 and r.json()["mapped"] == 1
        assert c.post("/sap/data/sales", json=body, headers={"X-Omai-Secret": "sec"}).status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_onec_stock_from_the_accounting_register(s, monkeypatch):
    """No trade registers in this 1C: stock = КоличествоBalance on 29xx / 10xx / 28xx, item and склад found by the
    subconto's type; the item maps by Item.onec_ref, else «Артикул» = the SAP code; sums never read."""
    from omai.connectors import onec
    from omai.models import ExternalBalance, Location

    monkeypatch.setattr(settings, "onec_odata_url", "https://1c.test/odata/")
    monkeypatch.setattr(settings, "onec_login", "u")
    onec._kept.clear()
    s.get(Location, "WH1").onec_ref = "whs-main"
    stock_acc, money_acc = "acc-2910", "acc-5110"

    def row(acc, item, whs, qty):
        return {"Account_Key": acc, "ExtDimension1": item, "ExtDimension1_Type": "StandardODATA.Catalog_Номенклатура",
                "ExtDimension2": "doc", "ExtDimension2_Type": "StandardODATA.Document_ПоступлениеТоваровУслуг",
                "ExtDimension3": whs, "ExtDimension3_Type": "StandardODATA.Catalog_Склады",
                "КоличествоBalance": qty, "СуммаBalance": 999}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "ChartOfAccounts" in path:
            return httpx.Response(200, json={"value": [{"Ref_Key": stock_acc, "Code": "2910"},
                                                       {"Ref_Key": money_acc, "Code": "5110"}]})
        if "Balance" in path:
            assert "Period=datetime'" in path
            return httpx.Response(200, json={"value": [
                row(stock_acc, "item-tan", "whs-main", 3), row(stock_acc, "item-tan", "whs-main", 2),   # two batches
                row(stock_acc, "item-x", "whs-main", 1),            # no Артикул match
                row(stock_acc, "item-tan", "whs-other", 4),          # склад not mapped
                row(money_acc, "item-tan", "whs-main", 50)]})        # not a stock account
        if "Catalog_" in path:
            return httpx.Response(200, json={"value": [{"Ref_Key": "item-tan", "Артикул": "TAN1"},
                                                       {"Ref_Key": "item-x", "Артикул": ""}]})
        return httpx.Response(404)

    client = onec.OneC(transport=httpx.MockTransport(handler))
    out = onec.sync(s, client)
    b = s.scalar(select(ExternalBalance).where(ExternalBalance.source == "1c"))
    assert (b.location_code, b.sku, b.qty) == ("WH1", "TANITA1", 5)
    assert out["mapped"] == 1 and out["unmapped_items"] == 1 and out["unmapped_warehouses"] == 1


def test_onec_410_says_what_to_do(monkeypatch):
    from omai.connectors import onec

    monkeypatch.setattr(settings, "onec_odata_url", "https://1c.test/odata/")
    monkeypatch.setattr(settings, "onec_login", "u")
    client = onec.OneC(transport=httpx.MockTransport(lambda r: httpx.Response(410)))
    try:
        client.get("ChartOfAccounts_Хозрасчетный")
        raise AssertionError("410 must raise")
    except onec.OneCError as exc:
        assert "switched off in Clobus" in str(exc)


def test_company_bot_token_is_refused(monkeypatch):
    secret = type(settings.omai_telegram_bot_token)
    monkeypatch.setattr(settings, "ops_manager_bot_telegram_bot_token", secret("111:company"))
    monkeypatch.setattr(settings, "omai_telegram_bot_token", secret("111:company"))
    assert settings.telegram_token_is_company_bot and not settings.telegram_configured
    monkeypatch.setattr(settings, "omai_telegram_bot_token", secret("222:omai"))
    assert settings.telegram_configured and not settings.telegram_token_is_company_bot


def test_billz_sales_sync(s, monkeypatch):
    monkeypatch.setattr(settings, "billz_secret_token", type(settings.billz_secret_token)("k"))
    day = datetime.now(settings.tz).date().isoformat()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/auth/login":
            return httpx.Response(200, json={"data": {"access_token": "t"}})
        if req.url.path == "/v1/product-general-table" and req.url.params["start_date"] == day:
            return httpx.Response(200, json={"count": 2, "products_stats_by_date": [
                {"order_id": "o1", "order_number": "101", "shop_id": "shop-2", "product_sku": "tan1",
                 "net_sold_measurement_value": 1},
                {"order_id": "o2", "order_number": "102", "shop_id": "shop-2", "product_sku": "tan1",
                 "net_sold_measurement_value": -1}]})
        return httpx.Response(200, json={"count": 0, "products_stats_by_date": [], "products": []})

    client = billz.Billz(transport=httpx.MockTransport(handler), pause=0)
    r1 = billz.sync(s, client)
    r2 = billz.sync(s, client)
    assert r1["booked"] == 2 and r2["booked"] == 0
    assert balance(s, sku="TANITA1", loc="ST2", cond="quarantine") == 1     # returns are quarantined
    assert balance(s, sku="TANITA1", loc="ST2", cond="saleable") == -1      # sale beyond known stock…
    from omai.models import Exception_
    assert s.scalar(select(Exception_).where(Exception_.kind == "negative_stock"))   # …is flagged


def test_api_login_tasks_and_permissions(tmp_path, monkeypatch):
    from omai import auth, db
    from omai.api import app as appmod
    from omai.models import User
    from sqlalchemy.orm import sessionmaker

    eng = db.make_engine(f"sqlite:///{tmp_path}/api.db")
    db.Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(appmod, "SessionLocal", factory)
    monkeypatch.setattr(appmod, "init_db", lambda: None)
    with factory() as s:
        from tests.conftest import seed
        seed(s)
        s.get(User, "otabek").pin_hash = auth.hash_pin("1234")
        s.get(User, "mgr").login, s.get(User, "mgr").pin_hash = "mgr", auth.hash_pin("9999")
        s.commit()
    c = TestClient(appmod.app)
    assert c.post("/auth/login", json={"login": "otabek", "pin": "0000"}).status_code == 401
    tok = c.post("/auth/login", json={"login": "otabek", "pin": "1234"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert c.get("/me/tasks", headers=h).status_code == 200
    assert c.get("/dashboard", headers=h).status_code == 403            # staff: no owner dashboard
    r = c.post("/receive", headers=h, json={"receipt_id": "A", "po_ref": "P", "sku": "TANITA1", "location": "WH1",
                                            "good_qty": 3})
    assert r.status_code == 200, r.text
    assert all("value" not in row for row in c.get("/stock", headers=h).json())
    mh = {"Authorization": "Bearer " + c.post("/auth/login", json={"login": "mgr", "pin": "9999"}).json()["token"]}
    d = c.get("/dashboard", headers=mh).json()
    assert "measures" in d and any(t["kind"] == "putaway" for t in d["tasks"])
    assert c.get("/health").json()["ok"]
    assert c.get("/reports/stock.xlsx", headers=mh).status_code == 200
