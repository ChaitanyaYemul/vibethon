from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
import pytest
from app.constants import ACTIVE
from app.errors import ApiError
from app.models import Assignment, Batch, Rider, ShelterDemand, Event
from app.engine.batches import create_batch
from app.engine.planner import plan_batch, claim_ttl
from app.engine.lifecycle import transition
from app.engine.rerouter import handle_shelter_change, reroute_assignment
from app.engine.sweeper import run_sweep
from app.engine.mutex import engine_lock
from app.schemas import BatchCreate


def setup_assignment(db, factory, now, category="COOKED_FOOD", in_transit=False):
    r = factory.make_rider()
    s = factory.make_shelter()
    d = factory.make_demand(s.id, category)
    b = factory.make_batch(category=category)
    plan_batch(db, b.id, now, commit=True)
    a = db.scalar(select(Assignment).where(Assignment.batch_id == b.id))
    if in_transit:
        transition(db, a, "PICKED_UP", now, "test")
        transition(db, a, "IN_TRANSIT", now, "test")
    return b, a, r, s, d


def conservation(db):
    db.flush()
    for b in db.scalars(select(Batch)):
        carried = db.scalar(select(func.coalesce(func.sum(Assignment.quantity), 0)).where(Assignment.batch_id == b.id, Assignment.status.in_((*ACTIVE, "DELIVERED"))))
        assert b.quantity_total == b.quantity_unassigned + b.quantity_wasted + b.quantity_lost + carried
    for d in db.scalars(select(ShelterDemand)):
        reserved = db.scalar(select(func.coalesce(func.sum(Assignment.quantity), 0)).join(Batch).where(Assignment.shelter_id == d.shelter_id, Batch.category == d.category, Assignment.status.in_(ACTIVE)))
        assert d.in_transit == reserved


def test_split_sums_to_total(db, factory, now):
    for _ in range(4):
        factory.make_rider()
    s = factory.make_shelter()
    factory.make_demand(s.id)
    b = factory.make_batch(quantity_total=180)
    result = plan_batch(db, b.id, now, commit=True)
    assert [x.quantity for x in result.slices] == [40] * 4
    assert b.quantity_unassigned == 20
    assert result.reasons == ["NO_RIDER"]
    assert all(x.quantity <= db.get(Rider, x.rider_id).capacity_units for x in result.slices)
    conservation(db)


def test_tight_expiry_infeasible(db, factory, now, client, headers):
    donor = factory.make_donor(lat=18.64, lng=73.76)
    factory.make_rider()
    factory.make_shelter()
    response = client.post("/api/batches", headers=headers, json=dict(donor_id=donor.id, category="MEDICAL", item_name="Insulin", cold_chain=True, quantity_total=25, expires_in_min=12))
    assert response.status_code == 422
    data = response.json()
    assert data["status"] == "INFEASIBLE"
    assert data["reasons"] == ["TIME_INFEASIBLE"]
    assert data["best_case_eta_min"] > 2
    assert not db.scalar(select(Assignment))
    conservation(db)


def test_urgent_batch_planned_first(db, factory, now):
    factory.make_rider()
    s = factory.make_shelter()
    factory.make_demand(s.id)
    later = factory.make_batch(expires_at=now+timedelta(minutes=90))
    urgent = factory.make_batch(expires_at=now+timedelta(minutes=30))
    run_sweep(db, now)
    assert db.scalar(select(Assignment)).batch_id == urgent.id
    assert later.quantity_unassigned == 40


def test_claim_expiry_releases_and_replans(db, factory, now):
    b, a, r, s, d = setup_assignment(db, factory, now)
    other = factory.make_rider()
    later = a.claim_expires_at + timedelta(seconds=1)
    r.last_heartbeat_at = other.last_heartbeat_at = later
    db.flush()
    run_sweep(db, later)
    assert a.status == "CLAIM_EXPIRED"
    new = db.scalar(select(Assignment).where(Assignment.status == "CLAIMED"))
    assert new.rider_id == other.id
    assert d.in_transit == 40
    assert b.quantity_unassigned == 0
    conservation(db)


def test_claim_ttl_shrinks_for_tight_batches(db, factory, now):
    assert claim_ttl(now, now+timedelta(minutes=4)).total_seconds() == 120
    assert claim_ttl(now, now+timedelta(minutes=60)).total_seconds() == 300
    factory.make_rider()
    s = factory.make_shelter(lat=18.52, lng=73.85)
    factory.make_demand(s.id)
    b = factory.make_batch(expires_at=now+timedelta(minutes=5), safety_buffer_min=0)
    plan_batch(db, b.id, now, commit=True)
    a = db.scalar(select(Assignment))
    assert a.claim_expires_at <= a.pickup_deadline


def test_silent_rider_claimed_reassigned(db, factory, now):
    b, a, r, s, d = setup_assignment(db, factory, now)
    other = factory.make_rider()
    later = now+timedelta(seconds=91)
    other.last_heartbeat_at = later
    db.flush()
    run_sweep(db, later)
    assert r.status == "UNRESPONSIVE"
    assert a.status == "CLAIM_EXPIRED"
    assert db.scalar(select(Assignment).where(Assignment.status == "CLAIMED")).rider_id == other.id
    conservation(db)


def test_silent_rider_in_transit_creates_recovery(db, factory, now):
    b, a, r, s, d = setup_assignment(db, factory, now, in_transit=True)
    r.lat, r.lng = 18.53, 73.86
    db.flush()
    run_sweep(db, now+timedelta(seconds=91))
    rb = db.scalar(select(Batch).where(Batch.is_recovery.is_(True)))
    assert a.status == "LOST" and b.quantity_lost == a.quantity
    assert rb.parent_batch_id == b.id and rb.quantity_total == a.quantity
    assert (rb.lat, rb.lng) == (r.lat, r.lng)
    conservation(db)


def test_shelter_close_reroutes_in_transit(db, factory, now):
    b, a, r, s, d = setup_assignment(db, factory, now, in_transit=True)
    fallback = factory.make_shelter(lat=18.53)
    fd = factory.make_demand(fallback.id)
    s.status = "CLOSED"
    db.flush()
    assert handle_shelter_change(db, s.id, now) == 1
    assert a.shelter_id == fallback.id and a.reroute_count == 1
    assert d.in_transit == 0 and fd.in_transit == a.quantity
    conservation(db)


def test_walk_in_overflow_reroutes(db, factory, now):
    b, a, r, s, d = setup_assignment(db, factory, now, in_transit=True)
    fallback = factory.make_shelter()
    factory.make_demand(fallback.id)
    d.received = 180
    db.flush()
    handle_shelter_change(db, s.id, now)
    assert a.shelter_id == fallback.id
    assert d.received+d.in_transit <= d.required
    conservation(db)


def test_no_fallback_marks_stranded(db, factory, now):
    b, a, r, s, d = setup_assignment(db, factory, now, in_transit=True)
    s.status = "CLOSED"
    db.flush()
    handle_shelter_change(db, s.id, now)
    assert a.status == "STRANDED" and a.shelter_id is None and d.in_transit == 0
    assert db.scalar(select(Event).where(Event.event_type == "STRANDED"))
    conservation(db)


def test_stranded_retried_when_shelter_opens(db, factory, now):
    b, a, r, s, d = setup_assignment(db, factory, now, in_transit=True)
    s.status = "CLOSED"
    db.flush()
    handle_shelter_change(db, s.id, now)
    s.status = "OPEN"
    db.flush()
    run_sweep(db, now)
    assert a.status == "IN_TRANSIT" and a.shelter_id == s.id
    conservation(db)


def test_incompatible_never_planned(db, factory, now):
    factory.make_rider()
    no_fuel = factory.make_shelter(has_cooking_fuel=False)
    no_water = factory.make_shelter(has_clean_water=False)
    for s in (no_fuel, no_water):
        factory.make_demand(s.id, "RAW_RATION")
    b = factory.make_batch(category="RAW_RATION")
    result = plan_batch(db, b.id, now, commit=True)
    assert not result.slices and result.reasons == ["INCOMPATIBLE"]
    safe = factory.make_shelter()
    factory.make_demand(safe.id, "RAW_RATION")
    plan_batch(db, b.id, now, commit=True)
    a = db.scalar(select(Assignment))
    transition(db, a, "PICKED_UP", now, "test")
    transition(db, a, "IN_TRANSIT", now, "test")
    safe.status = "CLOSED"
    db.flush()
    assert reroute_assignment(db, a, now, "TEST") == "STRANDED"
    conservation(db)


def test_flag_change_triggers_reroute(db, factory, now):
    b, a, r, s, d = setup_assignment(db, factory, now, "INFANT_FORMULA", True)
    fallback = factory.make_shelter()
    factory.make_demand(fallback.id, "INFANT_FORMULA")
    s.has_clean_water = False
    db.flush()
    handle_shelter_change(db, s.id, now)
    assert a.shelter_id == fallback.id


def test_one_active_assignment_per_rider(db, factory, now):
    b, a, r, s, d = setup_assignment(db, factory, now)
    with pytest.raises(IntegrityError), db.begin_nested():
        factory.add(Assignment(batch_id=b.id, rider_id=r.id, shelter_id=s.id, quantity=1, status="CLAIMED", claim_expires_at=a.claim_expires_at, pickup_deadline=a.pickup_deadline, deliver_by=a.deliver_by, created_at=now, updated_at=now))


def test_no_oversubscription_concurrent(db, factory, now, test_engine):
    for _ in range(2):
        factory.make_rider()
    s = factory.make_shelter()
    factory.make_demand(s.id, required=50)
    ids = [factory.make_batch().id for _ in range(2)]
    db.commit()
    def dispatch(ident):
        with sessionmaker(bind=test_engine, autoflush=False)() as worker:
            engine_lock(worker)
            plan_batch(worker, ident, now, commit=True)
            worker.commit()
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(dispatch, ids))
    db.expire_all()
    assert db.scalar(select(ShelterDemand)).in_transit == 50
    assert db.scalar(select(func.sum(Assignment.quantity))) == 50
    conservation(db)


def test_quantity_conservation(db, factory, now):
    b, a, r, s, d = setup_assignment(db, factory, now)
    conservation(db)
    transition(db, a, "CANCELLED", now, "test")
    conservation(db)
    plan_batch(db, b.id, now, commit=True)
    a = db.scalar(select(Assignment).where(Assignment.status == "CLAIMED"))
    transition(db, a, "PICKED_UP", now, "test")
    conservation(db)
    transition(db, a, "IN_TRANSIT", now, "test")
    conservation(db)
    transition(db, a, "DELIVERED", now, "test")
    conservation(db)
    assert b.status == "COMPLETED"


def test_sweep_idempotent(db, factory, now):
    b, a, r, s, d = setup_assignment(db, factory, now)
    later = now+timedelta(minutes=80)
    run_sweep(db, later)
    before = (b.quantity_unassigned, b.quantity_wasted, b.quantity_lost, a.status, d.in_transit, db.scalar(select(func.count()).select_from(Event)))
    run_sweep(db, later)
    after = (b.quantity_unassigned, b.quantity_wasted, b.quantity_lost, a.status, d.in_transit, db.scalar(select(func.count()).select_from(Event)))
    assert before == after
    conservation(db)


def test_invalid_transition_409(db, factory, now):
    b, a, r, s, d = setup_assignment(db, factory, now, in_transit=True)
    transition(db, a, "DELIVERED", now, "test")
    with pytest.raises(ApiError) as error:
        transition(db, a, "PICKED_UP", now, "test")
    assert error.value.status == 409
