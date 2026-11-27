"""scripts/rehearsal: refuses anything but a local rehearsal.db; builds count day end to end (1C / SAP stubbed)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from omai import db
from omai.config import settings
from omai.models import (
    Count,
    ExternalBalance,
    Item,
    Location,
    SourceSync,
    User,
    now_utc,
)
from scripts import rehearsal


def test_refuses_production_and_other_databases(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "sqlite:///omai.db")
    with pytest.raises(SystemExit):
        rehearsal.main()
    monkeypatch.setattr(settings, "database_url", "sqlite:///rehearsal.db")
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(SystemExit):
        rehearsal.main()


def test_builds_count_day(tmp_path, monkeypatch):
    from omai.connectors import mgmg_feed, onec
    from scripts import load_master

    path = tmp_path / "rehearsal.db"
    eng = db.make_engine(f"sqlite:///{path}")
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=eng, expire_on_commit=False))
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{path}")
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "onec_odata_url", "https://1c.test/")
    monkeypatch.setattr(settings, "onec_login", "u")
    monkeypatch.setattr(settings, "mgmg_sap_reader_url", type(settings.mgmg_sap_reader_url)("postgresql://x"))
    monkeypatch.setattr(rehearsal, "getpass", lambda _prompt: "2468")
    monkeypatch.setattr(load_master, "onec_snapshot", lambda: (
        {("cce7ceea-x", "r1"): 2}, [{"Ref_Key": "r1", "Code": "1", "Description": "Fenix", "Артикул": "010-1"}],
        {"cce7ceea-x": ("Основной склад", 1)}))

    def fake_sap(s):
        s.add(ExternalBalance(source="sap", location_code="01", sku="010-1", qty=3))
        s.add(SourceSync(source="sap", critical=True, last_success=now_utc()))
        return {"items": {}, "stock": {"rows": 1}, "pushed_minutes_ago": 5}

    monkeypatch.setattr(mgmg_feed, "sync", fake_sap)
    monkeypatch.setattr(onec, "OneC", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr(onec, "sync", lambda s, client: {"mapped": 1})
    rehearsal.main()
    with sessionmaker(bind=eng)() as s:
        assert s.get(Location, "01").movement_source == "omai" and s.get(Location, "01").onec_ref == "cce7ceea-x"
        assert {u.login for u in s.scalars(select(User))} == {"boss", "ombor", "tekshir"}
        c = s.scalar(select(Count))
        assert (c.kind, c.counter_id, c.checker_id, len(c.lines)) == ("baseline", "ombor", "tekshir", 1)
        assert s.get(Item, "010-1") is not None
