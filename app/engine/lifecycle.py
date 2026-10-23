from sqlalchemy import select, func
from app.constants import ACTIVE
from app.models import Assignment

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
