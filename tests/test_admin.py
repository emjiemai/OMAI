"""The admin page's API: owner and IT manage people; only the owner gives approving roles; blocking closes the bot."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from omai import auth, db
from omai.models import AuditLog, User


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from omai.api import app as appmod

    eng = db.make_engine(f"sqlite:///{tmp_path}/admin.db")
    db.Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(appmod, "SessionLocal", factory)
    monkeypatch.setattr(appmod, "init_db", lambda: None)
    with factory() as s:
        from tests.conftest import seed
        seed(s)
        for uid in ("own", "otabek", "mgr"):
            s.get(User, uid).login, s.get(User, uid).pin_hash = uid, auth.hash_pin("1111")
        s.add(User(id="ulug", name="IT", role="it", login="ulug", pin_hash=auth.hash_pin("1111")))
        s.get(User, "otabek").telegram_chat_id = "555"
        s.commit()
    c = TestClient(appmod.app)
    c.factory = factory
    return c


def _h(c, login):
    return {"Authorization": "Bearer " + c.post("/auth/login", json={"login": login, "pin": "1111"}).json()["token"]}


def test_owner_adds_changes_and_blocks(client):
    c, h = client, _h(client, "own")
    assert c.get("/admin").status_code == 200
    r = c.post("/admin/users", headers=h, json={"login": "Dilshod", "name": "Дилшод", "role": "checker", "pin": "4821",
                                                "location": "WH1"})
    assert r.status_code == 200, r.text and r.json()["login"] == "dilshod"
    assert c.post("/auth/login", json={"login": "dilshod", "pin": "4821"}).status_code == 200
    assert c.post("/admin/users", headers=h, json={"login": "dilshod", "name": "x", "role": "staff",
                                                   "pin": "1234"}).status_code == 409
    assert c.patch("/admin/users/otabek", headers=h, json={"active": False}).json()["telegram"] is False
    assert c.post("/auth/login", json={"login": "otabek", "pin": "1111"}).status_code == 401
    with c.factory() as s:
        assert s.get(User, "otabek").telegram_chat_id is None
        assert {a.action for a in s.query(AuditLog)} >= {"user_added", "user_changed"}
        assert all("4821" not in str(a.detail) for a in s.query(AuditLog))


def test_it_limits(client):
    c, h = client, _h(client, "ulug")
    assert c.get("/admin/users", headers=_h(client, "otabek")).status_code == 403     # staff: no admin
    assert c.get("/admin/users", headers=h).status_code == 200
    assert c.post("/admin/users", headers=h, json={"login": "boss2", "name": "x", "role": "owner",
                                                   "pin": "1234"}).status_code == 403
    assert c.patch("/admin/users/mgr", headers=h, json={"active": False}).status_code == 403
    assert c.patch("/admin/users/otabek", headers=h, json={"role": "manager"}).status_code == 403
    assert c.patch("/admin/users/otabek", headers=h, json={"pin": "12"}).status_code == 400
    assert c.patch("/admin/users/otabek", headers=h, json={"pin": "7777", "location": ""}).status_code == 200


def test_no_self_lockout_and_one_owner_stays(client):
    c, h = client, _h(client, "own")
    assert c.patch("/admin/users/own", headers=h, json={"active": False}).status_code == 400
    assert c.patch("/admin/users/own", headers=h, json={"role": "staff"}).status_code == 400
