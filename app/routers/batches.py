from dataclasses import asdict
from fastapi import APIRouter, Depends, Header
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from app import clock
from app.deps import read_db, write_db, require_role, own
from app.models import Batch, Donor, IdempotencyKey
from app.errors import ApiError
from app.schemas import BatchCreate
from app.engine.batches import create_batch, best_case_eta
from app.engine.planner import plan_batch
from app.serializers import batch_detail

router = APIRouter(prefix="/batches")
allowed = require_role("donor", "admin")


def owned_batch(db, ident, user):
    b = db.get(Batch, ident)
    if not b:
        raise ApiError(404, "NOT_FOUND", "Batch not found")
    own(db, Donor, b.donor_id, user)
    return b


def replay(db, key, user):
    if key:
        row = db.get(IdempotencyKey, (key, user.id))
        if row:
            return JSONResponse(status_code=row.response["status"], content=row.response["body"])


def finish(db, key, user, status, body):
    body = jsonable_encoder(body)
    if key:
        db.add(IdempotencyKey(key=key, user_id=user.id, response={"status": status, "body": body}))
    db.commit()
    return JSONResponse(status_code=status, content=body)


@router.post("")
def create(body: BatchCreate, user=Depends(allowed), db=Depends(write_db), key: str | None = Header(default=None, alias="Idempotency-Key", max_length=120)):
    now = clock.now(db)
    cached = replay(db, key, user)
    if cached is not None:
        return cached
    donor = own(db, Donor, body.donor_id, user) if body.donor_id else db.scalar(select(Donor).where(Donor.user_id == user.id))
    if not donor:
        raise ApiError(422, "VALIDATION_ERROR", "Select a donor")
    batch, plan = create_batch(db, now, donor, body, str(user.id))
    result = batch_detail(db, batch, now)
    status = 201
    if batch.status == "INFEASIBLE":
        status = 422
        result.update(code="INFEASIBLE", message="No safe delivery is feasible", batch_id=batch.id, best_case_eta_min=best_case_eta(db, batch, now))
    return finish(db, key, user, status, result)


@router.get("")
def listing(user=Depends(allowed), db=Depends(read_db)):
    query = select(Batch).order_by(Batch.id.desc())
    if user.role != "admin":
        query = query.join(Donor).where(Donor.user_id == user.id)
    now = clock.now(db)
    return [batch_detail(db, b, now) for b in db.scalars(query)]


@router.get("/{ident}")
def detail(ident: int, user=Depends(allowed), db=Depends(read_db)):
    return batch_detail(db, owned_batch(db, ident, user), clock.now(db))


@router.post("/{ident}/plan")
def dry_plan(ident: int, user=Depends(allowed), db=Depends(write_db)):
    b = owned_batch(db, ident, user)
    # Re-evaluate flagged batches in memory; roll back so a dry run writes nothing.
    old = b.status
    if old == "INFEASIBLE":
        b.status = "AVAILABLE"
    result = asdict(plan_batch(db, b.id, clock.now(db), commit=False))
    db.rollback()
    return result


@router.post("/{ident}/dispatch")
def dispatch(ident: int, user=Depends(allowed), db=Depends(write_db), key: str | None = Header(default=None, alias="Idempotency-Key", max_length=120)):
    cached = replay(db, key, user)
    if cached is not None:
        return cached
    b = owned_batch(db, ident, user)
    now = clock.now(db)
    if b.status == "INFEASIBLE":
        b.status = "AVAILABLE"
    result = plan_batch(db, b.id, now, commit=True)
    return finish(db, key, user, 200, {**asdict(result), "batch": batch_detail(db, b, now)})
