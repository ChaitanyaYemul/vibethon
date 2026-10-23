from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from app import clock
from app.config import settings
from app.db import get_db
from app.engine.mutex import engine_lock
from app.models import User, Donor, Rider, Shelter, ShelterDemand, Batch
from app.security import hash_password, limiter, token_for


@pytest.fixture(scope="session")
def test_engine():
    url = settings.test_database_url
    if not make_url(url).database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must name a dedicated database ending in _test")
    engine = create_engine(url, poolclass=NullPool, connect_args={"prepare_threshold": None})
    with engine.begin() as conn:
        conn.exec_driver_sql("drop schema public cascade; create schema public;")
        conn.exec_driver_sql(Path("schema.sql").read_text(encoding="utf-8"))
    yield engine
    engine.dispose()


@pytest.fixture
def now():
    return clock.real_now()


@pytest.fixture
def db(test_engine, now):
    with sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)() as session:
        engine_lock(session)
        session.execute(text("truncate assignments, batches, shelter_demands, shelters, riders, donors, users, events, idempotency_keys restart identity cascade"))
        session.execute(text("update app_state set value='0'"))
        session.commit()
        engine_lock(session)
        yield session
        session.rollback()


@pytest.fixture(scope="session")
def password_hash():
    return hash_password("test1234")


@pytest.fixture
def factory(db, now, password_hash):
    def add(obj):
        db.add(obj)
        db.flush()
        return obj

    def user(role):
        return add(User(name=role, email=str(uuid4())+"@test.local", password_hash=password_hash, role=role, created_at=now))

    def donor(**kw):
        return add(Donor(**(dict(user_id=user("donor").id, name="Donor", lat=18.52, lng=73.85) | kw)))

    def rider(**kw):
        return add(Rider(**(dict(user_id=user("rider").id, name="Rider", lat=18.52, lng=73.85, capacity_units=40, speed_kmph=25, status="AVAILABLE", last_heartbeat_at=now) | kw)))

    def shelter(**kw):
        return add(Shelter(**(dict(user_id=user("shelter").id, name="Shelter", lat=18.525, lng=73.855, status="OPEN", has_clean_water=True, has_cooking_fuel=True, has_refrigeration=True) | kw)))

    def demand(shelter_id, category="COOKED_FOOD", **kw):
        return add(ShelterDemand(**(dict(shelter_id=shelter_id, category=category, required=200, received=0, in_transit=0) | kw)))

    def batch(donor_id=None, **kw):
        qty = kw.get("quantity_total", 40)
        return add(Batch(**(dict(donor_id=donor_id or donor().id, category="COOKED_FOOD", item_name="Meals", quantity_total=qty, quantity_unassigned=qty, ready_at=now, expires_at=now+timedelta(minutes=75), safety_buffer_min=10, lat=18.52, lng=73.85, created_at=now) | kw)))

    return SimpleNamespace(add=add, user=user, make_donor=donor, make_rider=rider, make_shelter=shelter, make_demand=demand, make_batch=batch)


@pytest.fixture
def client(db, now, monkeypatch):
    from app.main import app
    monkeypatch.setattr(clock, "real_now", lambda: now)
    monkeypatch.setattr(limiter, "enabled", False)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def admin(factory):
    return factory.user("admin")


@pytest.fixture
def headers(admin):
    return {"Authorization": "Bearer " + token_for(admin)}


@pytest.fixture
def make_donor(factory):
    return factory.make_donor


@pytest.fixture
def make_rider(factory):
    return factory.make_rider


@pytest.fixture
def make_shelter(factory):
    return factory.make_shelter


@pytest.fixture
def make_demand(factory):
    return factory.make_demand


@pytest.fixture
def make_batch(factory):
    return factory.make_batch
