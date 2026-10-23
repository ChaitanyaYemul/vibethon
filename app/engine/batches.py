from datetime import timedelta
from sqlalchemy import select
from app.models import Batch, Rider, Shelter
from app.engine.planner import plan_batch, PlanResult
from app.engine.events import emit
from app.engine.feasibility import eta_for
from app import rules

def best_case_eta(db, batch, now):
    riders = db.scalars(select(Rider).order_by(Rider.id)).all()
    shelters = db.scalars(select(Shelter).where(Shelter.status == "OPEN").order_by(Shelter.id)).all()
    arrivals = [eta_for(now, r, batch, s).arrival for r in riders for s in shelters
                if not rules.missing(batch.category, batch.cold_chain, s)]
    return round((min(arrivals) - now).total_seconds() / 60, 2) if arrivals else None

def create_batch(db, now, donor, body, actor) -> tuple[Batch, PlanResult | None]:
    b = Batch(donor_id=donor.id, category=body.category, item_name=body.item_name,
              cold_chain=body.cold_chain, quantity_total=body.quantity_total,
              quantity_unassigned=body.quantity_total,
              ready_at=now + timedelta(minutes=body.ready_in_min),
              expires_at=now + timedelta(minutes=body.expires_in_min),
              safety_buffer_min=body.safety_buffer_min, lat=donor.lat, lng=donor.lng)
    db.add(b); db.flush()
    emit(db, now, actor, "batch", b.id, "BATCH_CREATED",
         {"category": b.category, "quantity": b.quantity_total, "expires_in_min": body.expires_in_min})
    dry = plan_batch(db, b.id, now, commit=False)
    # Physical feasibility is independent of current demand. The tight demo has
    # no MEDICAL demand, but still needs its impossible journey diagnosed.
    best = best_case_eta(db, b, now)
    if best is not None and best > body.expires_in_min - body.safety_buffer_min:
        dry = PlanResult([], b.quantity_unassigned, ["TIME_INFEASIBLE"])
    if not dry.slices and set(dry.reasons) & {"TIME_INFEASIBLE", "INCOMPATIBLE"}:
        b.status, b.unfulfilled_reason = "INFEASIBLE", ",".join(dry.reasons)
        emit(db, now, "system", "batch", b.id, "BATCH_INFEASIBLE", {"reasons": dry.reasons})
        return b, dry                      # router returns HTTP 422 (batch persisted, flagged)
    return b, (plan_batch(db, b.id, now, commit=True) if body.auto_dispatch else None)

def create_recovery_batch(db, now, orig, a, rider):
    """Cargo is stuck with a silent rider at its last known position: rescue it."""
    rb = Batch(donor_id=orig.donor_id, parent_batch_id=orig.id, is_recovery=True,
               category=orig.category, item_name=orig.item_name, cold_chain=orig.cold_chain,
               quantity_total=a.quantity, quantity_unassigned=a.quantity,
               ready_at=now, expires_at=orig.expires_at, safety_buffer_min=orig.safety_buffer_min,
               lat=rider.lat, lng=rider.lng)
    db.add(rb); db.flush()
    emit(db, now, "system", "batch", rb.id, "RECOVERY_CREATED", {"parent": orig.id, "quantity": a.quantity})
    dry = plan_batch(db, rb.id, now, commit=False)
    if not dry.slices and dry.reasons and dry.reasons[0] != "NO_RIDER":
        rb.status, rb.unfulfilled_reason = "INFEASIBLE", ",".join(dry.reasons)
        emit(db, now, "system", "batch", rb.id, "RECOVERY_INFEASIBLE", {"reasons": dry.reasons})
