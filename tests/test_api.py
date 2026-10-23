from datetime import timedelta
from fastapi.testclient import TestClient
from sqlalchemy import select, text, func
from app import clock
from app.config import settings
from app.db import get_db
from app.models import Assignment, Batch, Donor, Rider, ShelterDemand, User
from app.security import token_for, limiter
from tests.test_engine import setup_assignment, conservation


def test_deliver_after_deadline_rejected(db, factory, now, client, headers, monkeypatch):
    b, a, r, s, d = setup_assignment(db, factory, now, in_transit=True)
    later = a.deliver_by + timedelta(seconds=1)
    r.last_heartbeat_at = later
    db.flush()
    monkeypatch.setattr(clock, "real_now", lambda: later)
    response = client.post(f"/api/assignments/{a.id}/deliver", headers=headers)
    assert response.status_code == 409 and response.json()["code"] == "SPOILED"
    assert a.status == "LOST" and b.quantity_wasted == a.quantity
    assert d.received == 0
    conservation(db)


def test_pickup_after_ttl_rejected(db, factory, now, client, headers, monkeypatch):
    b, a, r, s, d = setup_assignment(db, factory, now)
    later = a.claim_expires_at+timedelta(seconds=1)
    r.last_heartbeat_at = later
    db.flush()
    monkeypatch.setattr(clock, "real_now", lambda: later)
    response = client.post(f"/api/assignments/{a.id}/pickup", headers=headers)
    assert response.status_code == 409 and response.json()["code"] == "CLAIM_EXPIRED"
    assert a.status == "CLAIM_EXPIRED"
    conservation(db)


def test_rbac_and_ownership(db, factory, now, client, monkeypatch):
    b, a, r, s, d = setup_assignment(db, factory, now)
    other = factory.make_rider()
    donor_user = db.get(User, db.get(Donor, b.donor_id).user_id)
    rider_user = db.get(User, other.user_id)
    rider_headers = {"Authorization": "Bearer "+token_for(rider_user)}
    donor_headers = {"Authorization": "Bearer "+token_for(donor_user)}
    assert client.post(f"/api/assignments/{a.id}/pickup", headers=rider_headers).status_code == 404
    assert client.get(f"/api/riders/{r.id}/assignment", headers=rider_headers).status_code == 404
    assert client.post(f"/api/shelters/{s.id}/close", headers=donor_headers).status_code == 403
    assert client.get("/api/batches").status_code == 401
    response = client.get("/api/config")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    # Exercise slowapi as well as RBAC; errors must keep the public JSON shape.
    monkeypatch.setattr(limiter, "enabled", True)
    limiter.reset()
    for _ in range(10):
        assert client.post("/api/auth/login", json={"email": "missing@test.local", "password": "wrong"}).status_code == 401
    limited = client.post("/api/auth/login", json={"email": "missing@test.local", "password": "wrong"})
    assert limited.status_code == 429 and limited.json()["code"] == "RATE_LIMITED"


def test_validation_bounds(db, factory, client, headers):
    donor = factory.make_donor()
    body = dict(donor_id=donor.id, category="COOKED_FOOD", item_name="Meals", quantity_total=10, expires_in_min=60)
    for change in [{"quantity_total": -1}, {"category": "OTHER"}, {"extra": True}, {"expires_in_min": 0}, {"ready_in_min": 720, "expires_in_min": 10}]:
        assert client.post("/api/batches", headers=headers, json=body|change).status_code == 422
    r = factory.make_rider()
    assert client.post(f"/api/riders/{r.id}/heartbeat", headers=headers, json={"lat": 200, "lng": 0}).status_code == 422
    assert client.post("/api/auth/register", json=dict(name="Bad", email="bad@test.local", password="test1234", role="admin", lat=0, lng=0)).status_code == 422
    # A compatible destination can become unsafe between planning and pickup.
    b, a, r, s, d = setup_assignment(db, factory, clock.now(db), "RAW_RATION")
    s.has_cooking_fuel = False
    db.flush()
    response = client.post(f"/api/assignments/{a.id}/pickup", headers=headers)
    assert response.status_code == 409
    assert a.status == "CANCELLED" and d.in_transit == 0
    conservation(db)


def test_idempotency_key_replay(db, factory, now, client, headers):
    donor = factory.make_donor()
    body = dict(donor_id=donor.id, category="KIT", item_name="Aid kits", quantity_total=10, expires_in_min=60)
    request_headers = headers|{"Idempotency-Key": "create-once"}
    first = client.post("/api/batches", headers=request_headers, json=body)
    second = client.post("/api/batches", headers=request_headers, json=body)
    assert first.status_code == 201
    assert second.json() == first.json()
    assert db.scalar(select(func.count()).select_from(Batch)) == 1
    # Preview reserves nothing, and dispatch replay cannot allocate twice.
    factory.make_rider()
    s = factory.make_shelter()
    factory.make_demand(s.id, "KIT")
    db.commit()
    ident = first.json()["id"]
    preview = client.post(f"/api/batches/{ident}/plan", headers=headers)
    assert preview.status_code == 200
    assert not db.scalar(select(Assignment))
    dispatch_headers = headers|{"Idempotency-Key": "dispatch-once"}
    first_dispatch = client.post(f"/api/batches/{ident}/dispatch", headers=dispatch_headers)
    second_dispatch = client.post(f"/api/batches/{ident}/dispatch", headers=dispatch_headers)
    assert first_dispatch.json() == second_dispatch.json()
    assert db.scalar(select(func.count()).select_from(Assignment)) == 1


def test_demo_routes_absent_when_disabled(monkeypatch):
    from app.main import create_app
    monkeypatch.setattr(settings, "demo_mode", False)
    with TestClient(create_app()) as client:
        assert client.post("/api/demo/time-travel", json={"minutes": 5}).status_code == 404
        assert client.post("/api/demo/seed").status_code == 404


def test_time_travel_keeps_live_riders_fresh(db, factory, now, client, headers):
    live = factory.make_rider()
    silent = factory.make_rider(demo_silenced=True)
    response = client.post("/api/demo/time-travel", headers=headers, json={"minutes": 15})
    assert response.status_code == 200
    assert live.last_heartbeat_at == now+timedelta(minutes=15)
    assert live.status == "AVAILABLE"
    assert silent.status == "UNRESPONSIVE"


def test_judge_demo_end_to_end(db, now, client, headers):
    seeded = client.post("/api/demo/seed", headers=headers)
    assert seeded.status_code == 200
    headers = {"Authorization": "Bearer "+seeded.json()["token"]}
    main = client.post("/api/demo/batches/main", headers=headers)
    assert main.status_code == 201
    batch = main.json()["batches"][0]
    assert len(batch["assignments"]) == 4 and batch["quantity_unassigned"] == 20
    assert client.post("/api/demo/riders/2/silence", headers=headers, json={"on": True}).status_code == 200
    assert client.post("/api/demo/riders/accept-all", headers=headers).json()["accepted"] == 3
    assert client.post("/api/demo/time-travel", headers=headers, json={"minutes": 6}).status_code == 200
    assert db.get(Rider, 2).status == "UNRESPONSIVE"
    assert db.get(Batch, batch["id"]).quantity_unassigned == 60
    assert client.post("/api/riders/5/status", headers=headers, json={"online": True}).status_code == 200
    assert db.get(Batch, batch["id"]).quantity_unassigned == 20
    active = db.scalar(select(Assignment).where(Assignment.status == "IN_TRANSIT"))
    old_shelter = active.shelter_id
    assert client.post(f"/api/shelters/{old_shelter}/close", headers=headers).status_code == 200
    assert active.shelter_id != old_shelter or active.status == "STRANDED"
    conservation(db)
    assert client.post("/api/demo/seed", headers=headers).status_code == 200
    tight = client.post("/api/demo/batches/tight", headers=headers)
    assert tight.status_code == 422 and tight.json()["batches"][0]["reasons"] == ["TIME_INFEASIBLE"]
    tight_id = tight.json()["batches"][0]["id"]
    assert client.post(f"/api/batches/{tight_id}/dispatch", headers=headers).status_code == 422
    assert db.get(Batch, tight_id).status == "INFEASIBLE"
    assert client.post("/api/demo/seed", headers=headers).status_code == 200
    compat = client.post("/api/demo/batches/compat", headers=headers)
    assert compat.status_code == 201
    for b in compat.json()["batches"]:
        for a in b["assignments"]:
            if b["category"] == "RAW_RATION":
                assert a["shelter_id"] in (1, 2)
            else:
                assert a["shelter_id"] in (1, 3)
    conservation(db)
