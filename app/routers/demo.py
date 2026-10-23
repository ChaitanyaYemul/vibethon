from datetime import timedelta
from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from app import clock
from app.deps import write_db, require_role, own
from app.models import Rider, Assignment, Donor
from app.schemas import BatchCreate, TimeTravel, Silence
from app.errors import ApiError
from app.engine.events import emit
from app.engine.batches import create_batch, best_case_eta
from app.engine.sweeper import run_sweep
from app.engine.lifecycle import transition
from app.routers.assignments import pickup_cargo
from app.routers.auth import credentials
from app.serializers import batch_detail
from scripts.seed import seed, rows

router = APIRouter(prefix="/demo")
allowed = require_role("admin")


@router.post("/seed")
def reseed(user=Depends(allowed), db=Depends(write_db)):
    admin = seed(db, clock.real_now())
    result = credentials(db, admin)
    db.commit()
    return result


@router.post("/batches/{scenario}")
def scenario_batches(scenario: str, user=Depends(allowed), db=Depends(write_db)):
    if scenario not in ("main", "tight", "compat"):
        raise ApiError(404, "NOT_FOUND", "Unknown scenario")
    now = clock.now(db)
    result, status = [], 201
    for row in rows("batches"):
        if row.pop("scenario") != scenario:
            continue
        donor = db.scalar(select(Donor).where(Donor.name == row.pop("donor_name")))
        body = BatchCreate(**row)
        b, plan = create_batch(db, now, donor, body, str(user.id))
        item = batch_detail(db, b, now)
        if b.status == "INFEASIBLE":
            status = 422
            item.update(code="INFEASIBLE", batch_id=b.id, best_case_eta_min=best_case_eta(db, b, now))
        result.append(item)
    db.commit()
    return JSONResponse(status_code=status, content=jsonable_encoder({"batches": result}))


@router.post("/time-travel")
def time_travel(body: TimeTravel, user=Depends(allowed), db=Depends(write_db)):
    now = clock.now(db)
    delta = timedelta(minutes=body.minutes)
    offset = clock.get_offset(db).total_seconds() + delta.total_seconds()
    db.execute(text("update app_state set value=:v where key='clock_offset_s'"), {"v": str(offset)})
    for rider in db.scalars(select(Rider).where(Rider.demo_silenced.is_(False))):
        rider.last_heartbeat_at += delta
    db.flush()
    emit(db, now + delta, str(user.id), "system", None, "CLOCK_JUMP", {"minutes": body.minutes})
    stats = run_sweep(db, now + delta)
    db.commit()
    return {"now": now + delta, "stats": stats}


@router.post("/riders/{ident}/silence")
def silence(ident: int, body: Silence, user=Depends(allowed), db=Depends(write_db)):
    r = own(db, Rider, ident, user)
    r.demo_silenced = body.on
    db.commit()
    return {"id": r.id, "demo_silenced": r.demo_silenced}


@router.post("/riders/accept-all")
def accept_all(user=Depends(allowed), db=Depends(write_db)):
    now = clock.now(db)
    count = 0
    for a in db.scalars(select(Assignment).join(Rider, Rider.id == Assignment.rider_id).where(Assignment.status == "CLAIMED", Rider.demo_silenced.is_(False)).order_by(Assignment.id)).all():
        try:
            pickup_cargo(db, a, now, str(user.id))
            transition(db, a, "IN_TRANSIT", now, str(user.id))
            count += 1
        except ApiError as exc:
            if exc.code not in ("CLAIM_EXPIRED", "NO_VALID_DESTINATION", "NOT_READY"):
                raise
    db.commit()
    return {"accepted": count}


@router.post("/reset-clock")
def reset_clock(user=Depends(allowed), db=Depends(write_db)):
    now = clock.now(db)
    offset = clock.get_offset(db)
    # Shift timestamps with the clock so reset cannot create future heartbeats.
    for r in db.scalars(select(Rider)):
        r.last_heartbeat_at -= offset
    db.execute(text("update app_state set value='0' where key='clock_offset_s'"))
    db.flush()
    run_sweep(db, now - offset)
    db.commit()
    return {"now": now - offset}
