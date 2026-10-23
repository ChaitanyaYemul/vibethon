from fastapi import APIRouter, Depends
from sqlalchemy import select
from app import clock
from app.constants import ACTIVE
from app.deps import read_db, write_db, require_role, own
from app.models import Shelter, ShelterDemand, Assignment
from app.schemas import Demand, WalkIn, Flags
from app.engine.events import emit
from app.engine.rerouter import handle_shelter_change
from app.engine.sweeper import run_sweep
from app.serializers import record, assignment_detail

router = APIRouter(prefix="/shelters")
allowed = require_role("shelter", "admin")


def shelter_detail(db, s):
    return {**record(s), "demands": [{**record(d), "open_capacity": d.open_capacity} for d in db.scalars(select(ShelterDemand).where(ShelterDemand.shelter_id == s.id).order_by(ShelterDemand.id))]}


def demand_row(db, ident, category):
    d = db.scalar(select(ShelterDemand).where(ShelterDemand.shelter_id == ident, ShelterDemand.category == category).with_for_update())
    if not d:
        d = ShelterDemand(shelter_id=ident, category=category, required=0, received=0, in_transit=0)
        db.add(d)
        db.flush()
    return d


def finish_change(db, s, now, user, event, payload):
    emit(db, now, str(user.id), "shelter", s.id, event, payload)
    db.flush()
    handle_shelter_change(db, s.id, now)
    run_sweep(db, now)
    db.commit()
    return shelter_detail(db, s)


@router.get("/{ident}")
def detail(ident: int, user=Depends(allowed), db=Depends(read_db)):
    return shelter_detail(db, own(db, Shelter, ident, user))


@router.put("/{ident}/demand")
def demand(ident: int, body: Demand, user=Depends(allowed), db=Depends(write_db)):
    s = own(db, Shelter, ident, user)
    now = clock.now(db)
    d = demand_row(db, ident, body.category)
    d.required = body.required
    return finish_change(db, s, now, user, "DEMAND_CHANGED", {"category": body.category, "required": body.required})


@router.post("/{ident}/walk-in")
def walk_in(ident: int, body: WalkIn, user=Depends(allowed), db=Depends(write_db)):
    s = own(db, Shelter, ident, user)
    now = clock.now(db)
    d = demand_row(db, ident, body.category)
    d.received += body.quantity
    return finish_change(db, s, now, user, "WALK_IN", {"category": body.category, "quantity": body.quantity})


@router.post("/{ident}/close")
def close(ident: int, user=Depends(allowed), db=Depends(write_db)):
    s = own(db, Shelter, ident, user)
    now = clock.now(db)
    s.status = "CLOSED"
    return finish_change(db, s, now, user, "SHELTER_CLOSED", {})


@router.post("/{ident}/open")
def open_shelter(ident: int, user=Depends(allowed), db=Depends(write_db)):
    s = own(db, Shelter, ident, user)
    now = clock.now(db)
    s.status = "OPEN"
    return finish_change(db, s, now, user, "SHELTER_OPENED", {})


@router.put("/{ident}/flags")
def flags(ident: int, body: Flags, user=Depends(allowed), db=Depends(write_db)):
    s = own(db, Shelter, ident, user)
    now = clock.now(db)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(s, field, value)
    return finish_change(db, s, now, user, "SHELTER_FLAGS", {})


@router.get("/{ident}/incoming")
def incoming(ident: int, user=Depends(allowed), db=Depends(read_db)):
    own(db, Shelter, ident, user)
    now = clock.now(db)
    return [assignment_detail(db, a, now) for a in db.scalars(select(Assignment).where(Assignment.shelter_id == ident, Assignment.status.in_(ACTIVE)).order_by(Assignment.id))]
