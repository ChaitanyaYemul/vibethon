from sqlalchemy import select, func
from app.models import Batch, Assignment, Rider, ShelterDemand
from app.errors import ApiError
from app.engine.events import emit
from app.constants import ACTIVE
ALLOWED = {
    "CLAIMED":   {"PICKED_UP", "CLAIM_EXPIRED", "CANCELLED", "LOST"},
    "PICKED_UP": {"IN_TRANSIT", "STRANDED", "LOST"},
    "IN_TRANSIT":{"DELIVERED", "STRANDED", "LOST"},
    "STRANDED":  {"IN_TRANSIT", "LOST"},
}

def transition(db, a, to, now, actor, reason=None):
    if to not in ALLOWED.get(a.status, set()):
        raise ApiError(409, "INVALID_TRANSITION", f"{a.status} -> {to} not allowed")
    batch = db.execute(select(Batch).where(Batch.id == a.batch_id).with_for_update()).scalar_one()
    frm, old_shelter = a.status, a.shelter_id
    a.status, a.reason, a.updated_at = to, reason, now
    if to == "PICKED_UP":
        a.picked_up_at = now
    if to in ("CLAIM_EXPIRED", "CANCELLED"):
        batch.quantity_unassigned += a.quantity
        _release_demand(db, a, batch)
        _release_rider(db, a.rider_id)
    elif to == "LOST":
        if a.shelter_id:
            _release_demand(db, a, batch)
        if reason == "SPOILED": batch.quantity_wasted += a.quantity
        else:                   batch.quantity_lost   += a.quantity   # RIDER_SILENT
        _release_rider(db, a.rider_id)
    elif to == "DELIVERED":
        d = _demand(db, a.shelter_id, batch.category)
        d.in_transit -= a.quantity
        d.received += a.quantity
        a.delivered_at = now
        _release_rider(db, a.rider_id)
    elif to == "STRANDED":
        _release_demand(db, a, batch)
        a.shelter_id = None
    db.flush()
    recompute_batch_status(db, batch, now)
    emit(db, now, actor, "assignment", a.id, {
        "CLAIM_EXPIRED": "CLAIM_EXPIRED", "CANCELLED": "ASSIGNMENT_CANCELLED",
        "PICKED_UP": "PICKED_UP", "IN_TRANSIT": "IN_TRANSIT", "DELIVERED": "DELIVERED",
        "STRANDED": "STRANDED", "LOST": "SPOILED" if reason == "SPOILED" else "RIDER_SILENT",
    }[to], {"from": frm, "batch_id": a.batch_id, "rider_id": a.rider_id,
            "shelter_id": old_shelter, "quantity": a.quantity, "reason": reason})

def _demand(db, shelter_id, category):
    return db.execute(select(ShelterDemand).where(
        ShelterDemand.shelter_id == shelter_id, ShelterDemand.category == category
    ).with_for_update()).scalar_one()

def _release_demand(db, a, batch):
    if a.shelter_id:
        d = _demand(db, a.shelter_id, batch.category)
        d.in_transit = max(0, d.in_transit - a.quantity)

def _release_rider(db, rider_id):
    r = db.execute(select(Rider).where(Rider.id == rider_id).with_for_update()).scalar_one()
    if r.status == "BUSY":          # never flip UNRESPONSIVE / OFFLINE back automatically
        r.status = "AVAILABLE"

def recompute_batch_status(db, batch, now):
    if batch.status == "INFEASIBLE":
        return
    db.flush()
    active = db.scalar(select(func.count()).where(Assignment.batch_id == batch.id,
                                                  Assignment.status.in_(ACTIVE)))
    delivered = db.scalar(select(func.coalesce(func.sum(Assignment.quantity), 0)).where(
        Assignment.batch_id == batch.id, Assignment.status == "DELIVERED"))
    if batch.quantity_unassigned > 0:
        batch.status = "PARTIALLY_ASSIGNED" if (active or delivered) else "AVAILABLE"
    else:
        batch.status = "FULLY_ASSIGNED" if active else ("COMPLETED" if delivered else "EXPIRED")
