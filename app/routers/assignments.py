from fastapi import APIRouter, Depends
from sqlalchemy import select
from app import clock, rules
from app.deps import write_db, require_role, own
from app.errors import ApiError
from app.models import Assignment, Batch, Rider, Shelter, ShelterDemand
from app.engine.lifecycle import transition
from app.engine.planner import plan_batch
from app.engine.feasibility import eta_for
from app.serializers import assignment_detail

router = APIRouter(prefix="/assignments")
allowed = require_role("rider", "admin")


def owned_assignment(db, ident, user):
    a = db.get(Assignment, ident)
    if not a:
        raise ApiError(404, "NOT_FOUND", "Assignment not found")
    own(db, Rider, a.rider_id, user)
    return a


def destination_valid(db, a, now, pickup=False):
    b = db.get(Batch, a.batch_id)
    s = db.get(Shelter, a.shelter_id) if a.shelter_id else None
    d = db.scalar(select(ShelterDemand).where(ShelterDemand.shelter_id == a.shelter_id, ShelterDemand.category == b.category))
    if not s or s.status != "OPEN" or rules.missing(b.category, b.cold_chain, s):
        return False
    if not d or d.received + d.in_transit > d.required:
        return False
    return eta_for(now, db.get(Rider, a.rider_id), b, s, picked_up=not pickup).feasible


def pickup_cargo(db, a, now, actor):
    if a.status == "CLAIM_EXPIRED":
        raise ApiError(409, "CLAIM_EXPIRED", "Claim expired")
    if a.status == "CANCELLED" and a.reason == "NO_VALID_DESTINATION":
        raise ApiError(409, "NO_VALID_DESTINATION", "No safe destination remains")
    if a.status != "CLAIMED":
        raise ApiError(409, "INVALID_TRANSITION", "Pickup requires a claimed assignment")
    if now > a.claim_expires_at:
        transition(db, a, "CLAIM_EXPIRED", now, actor, "TTL")
        raise ApiError(409, "CLAIM_EXPIRED", "Claim expired")
    b = db.get(Batch, a.batch_id)
    if now < b.ready_at:
        raise ApiError(409, "NOT_READY", "Cargo is not ready yet")
    if not destination_valid(db, a, now, pickup=True):
        from app.engine.rerouter import reroute_assignment
        if reroute_assignment(db, a, now, "PICKUP_RECHECK") == "CANCELLED":
            raise ApiError(409, "NO_VALID_DESTINATION", "No safe destination remains")
    transition(db, a, "PICKED_UP", now, actor)
    rider = db.get(Rider, a.rider_id)
    rider.lat, rider.lng = b.lat, b.lng


@router.post("/{ident}/pickup")
def pickup(ident: int, user=Depends(allowed), db=Depends(write_db)):
    a = owned_assignment(db, ident, user)
    now = clock.now(db)
    try:
        pickup_cargo(db, a, now, str(user.id))
    except ApiError:
        db.commit()
        raise
    db.commit()
    return assignment_detail(db, a, now)


@router.post("/{ident}/depart")
def depart(ident: int, user=Depends(allowed), db=Depends(write_db)):
    a = owned_assignment(db, ident, user)
    now = clock.now(db)
    if a.status != "PICKED_UP":
        raise ApiError(409, "INVALID_TRANSITION", "Departure requires picked-up cargo")
    transition(db, a, "IN_TRANSIT", now, str(user.id))
    db.commit()
    return assignment_detail(db, a, now)


@router.post("/{ident}/deliver")
def deliver(ident: int, user=Depends(allowed), db=Depends(write_db)):
    a = owned_assignment(db, ident, user)
    now = clock.now(db)
    if a.status == "LOST" and a.reason == "SPOILED":
        raise ApiError(409, "SPOILED", "Delivery deadline passed")
    if a.status != "IN_TRANSIT":
        raise ApiError(409, "INVALID_TRANSITION", "Delivery requires transit")
    if now > a.deliver_by:
        transition(db, a, "LOST", now, str(user.id), "SPOILED")
        db.commit()
        raise ApiError(409, "SPOILED", "Delivery deadline passed")
    if not destination_valid(db, a, now):
        from app.engine.rerouter import reroute_assignment
        outcome = reroute_assignment(db, a, now, "DELIVERY_RECHECK")
        result = assignment_detail(db, a, now)
        db.commit()
        raise ApiError(409, "NO_VALID_DESTINATION", "Destination changed; follow updated instructions", outcome=outcome, assignment=result)
    transition(db, a, "DELIVERED", now, str(user.id))
    rider, shelter = db.get(Rider, a.rider_id), db.get(Shelter, a.shelter_id)
    rider.lat, rider.lng = shelter.lat, shelter.lng
    db.commit()
    return assignment_detail(db, a, now)


@router.post("/{ident}/cancel")
def cancel(ident: int, user=Depends(allowed), db=Depends(write_db)):
    a = owned_assignment(db, ident, user)
    now = clock.now(db)
    transition(db, a, "CANCELLED", now, str(user.id), "RIDER_CANCEL")
    plan_batch(db, a.batch_id, now, commit=True, extra_exclude={a.rider_id})
    db.commit()
    return assignment_detail(db, a, now)
