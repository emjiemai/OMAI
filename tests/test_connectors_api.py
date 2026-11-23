from __future__ import annotations

from datetime import datetime

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from omai.config import settings
from omai.connectors import billz, sap_b1
from omai.ledger import balance
from omai.models import DispatchOrder, ExpectedReceipt, ExternalBalance, Location


def test_sap_service_layer_sync(s, monkeypatch):
    monkeypatch.setattr(settings, "sap_sl_url", "https://sap.test/b1s/v1")
    monkeypatch.setattr(settings, "sap_company_db", "EMJIEM")
    monkeypatch.setattr(settings, "sap_read_user", "omai_read")
    s.get(Location, "WH1").movement_source = "sap"
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.split("/b1s/v1/")[-1]
        calls.append(path)
        if path == "Login":
            return httpx.Response(200, json={"SessionId": "x"})
        data = {
            "Items": [{"ItemCode": "TAN1", "ItemName": "Tanita", "ManageSerialNumbers": "tNO",
                       "ManageBatchNumbers": "tNO", "ItemWarehouseInfoCollection": [
                           {"WarehouseCode": "01", "InStock": 7}]}],
            "PurchaseDeliveryNotes": [{"DocEntry": 5, "DocNum": 105, "DocDate": "2026-11-20",
                                       "DocumentLines": [{"LineNum": 0, "ItemCode": "TAN1", "Quantity": 7,
                                                          "WarehouseCode": "01"}]}],
            "Orders": [{"DocNum": 9, "CardName": "Hotel", "DocumentLines": [
                {"ItemCode": "TAN1", "Quantity": 2, "RemainingOpenQuantity": 2, "WarehouseCode": "01"}]}],
            "PurchaseOrders": [{"DocNum": 3, "DocDueDate": "2026-11-25", "DocumentLines": [
                {"ItemCode": "TAN1", "Quantity": 12, "RemainingOpenQuantity": 12, "WarehouseCode": "01"}]}],
        }.get(path, [])
        return httpx.Response(200, json={"value": data})

    sl = sap_b1.ServiceLayer(transport=httpx.MockTransport(handler))
    sap_b1.sync(s, sl)
    sap_b1.sync(s, sl)                    # second run: nothing doubles
    assert balance(s, sku="TANITA1", loc="WH1") == 7
    assert s.scalar(select(ExternalBalance)).qty == 7
    assert s.scalar(select(DispatchOrder)).order_ref == "SO9"
    assert s.scalar(select(ExpectedReceipt)).qty_expected == 12
    assert calls[0] == "Login"


def test_sap_gateway_push(s):
    out = sap_b1.ingest_payload(s, {"kind": "stock", "rows": [{"ItemCode": "TAN1", "WhsCode": "01", "OnHand": 4}]})
    assert out["rows"] == 1


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
