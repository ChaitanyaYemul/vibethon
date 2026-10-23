from fastapi import APIRouter, Depends
from sqlalchemy import select
from app import clock
from app.constants import ACTIVE
from app.deps import read_db, write_db, require_role, own
from app.models import Rider, Assignment
from app.schemas import Position, Online
from app.errors import ApiError
from app.engine.events import emit
from app.serializers import assignment_detail, record

router = APIRouter(prefix="/riders")
allowed = require_role("rider", "admin")


def active(db, ident):
    return db.scalar(select(Assignment).where(Assignment.rider_id == ident, Assignment.status.in_(ACTIVE)))


@router.get("/{ident}/assignment")
def assignment(ident: int, user=Depends(allowed), db=Depends(read_db)):
    r = own(db, Rider, ident, user)
    a = active(db, ident)
    return {"rider": record(r), "assignment": assignment_detail(db, a, clock.now(db)) if a else None}


@router.post("/{ident}/heartbeat")
def heartbeat(ident: int, body: Position, user=Depends(allowed), db=Depends(write_db)):
    r = own(db, Rider, ident, user)
    now = clock.now(db)
    if not r.demo_silenced:
        r.lat, r.lng, r.last_heartbeat_at = body.lat, body.lng, now
        if r.status == "UNRESPONSIVE" and not active(db, ident):
            r.status = "AVAILABLE"
    db.commit()
    return {"status": r.status}


@router.post("/{ident}/status")
def status(ident: int, body: Online, user=Depends(allowed), db=Depends(write_db)):
    r = own(db, Rider, ident, user)
    now = clock.now(db)
    a = active(db, ident)
    if not body.online and a:
        raise ApiError(409, "ACTIVE_ASSIGNMENT", "Complete or cancel the active assignment first")
    if body.online:
        r.status, r.last_heartbeat_at = ("BUSY" if a else "AVAILABLE"), now
        emit(db, now, str(user.id), "rider", r.id, "RIDER_ONLINE", {})
    else:
        r.status = "OFFLINE"
    db.flush()
    from app.engine.sweeper import run_sweep
    run_sweep(db, now)
    db.commit()
    return record(r)
