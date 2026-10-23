from sqlalchemy import select
from app import rules
from app.constants import ACTIVE, IN_FLIGHT
from app.models import Batch, Rider, Assignment, Shelter, ShelterDemand
from app.engine.feasibility import eta_for, pickup_deadline
from app.engine.lifecycle import transition
from app.engine.events import emit

def reroute_assignment(db, a, now, reason) -> str:
    """Returns REROUTED | STRANDED | CANCELLED."""
    batch = db.execute(select(Batch).where(Batch.id == a.batch_id).with_for_update()).scalar_one()
    rider = db.get(Rider, a.rider_id)
    picked = a.status in IN_FLIGHT
    rows = db.execute(                          # ONE locked query, ordered by id
        select(ShelterDemand, Shelter).join(Shelter, Shelter.id == ShelterDemand.shelter_id)
        .where(ShelterDemand.category == batch.category).order_by(ShelterDemand.id)
        .with_for_update(of=ShelterDemand)).all()
    old_id = a.shelter_id
    old_d = next((d for d, s in rows if s.id == old_id), None)
    best = None
    for d, s in rows:
        if s.id == old_id or s.status != "OPEN": continue
        if d.open_capacity < a.quantity: continue
        if rules.missing(batch.category, batch.cold_chain, s): continue
        e = eta_for(now, rider, batch, s, picked_up=picked)
        if e.feasible and (best is None or e.arrival < best[2].arrival):
            best = (d, s, e)
    if best:
        d, s, e = best
        if old_d: old_d.in_transit = max(0, old_d.in_transit - a.quantity)
        d.in_transit += a.quantity
        a.shelter_id, a.reroute_count = s.id, a.reroute_count + 1
        if a.status == "CLAIMED":
            a.pickup_deadline = pickup_deadline(batch, s, rider)
            a.claim_expires_at = min(a.claim_expires_at, a.pickup_deadline)
        was = a.status
        if was == "STRANDED":
            transition(db, a, "IN_TRANSIT", now, "system", "REROUTED")
        emit(db, now, "system", "assignment", a.id, "REROUTED",
             {"from_shelter": old_id, "to_shelter": s.id, "quantity": a.quantity,
              "reason": reason, "slack_min": round(e.slack_min, 1)})
        a.updated_at = now
        db.flush()
        return "REROUTED"
    if a.status == "CLAIMED":
        transition(db, a, "CANCELLED", now, "system", "NO_VALID_DESTINATION")
        return "CANCELLED"                      # quantity returns to pool, sweeper re-plans
    if a.status != "STRANDED":
        transition(db, a, "STRANDED", now, "system", reason)   # alert on ops console
    return "STRANDED"

def handle_shelter_change(db, shelter_id, now) -> int:
    """Call synchronously after: close, flags change, demand change, walk-in donation."""
    moved = 0
    shelter = db.get(Shelter, shelter_id)
    for a in db.scalars(select(Assignment).where(
            Assignment.shelter_id == shelter_id, Assignment.status.in_(ACTIVE)).order_by(Assignment.id)).all():
        batch = db.get(Batch, a.batch_id)
        if shelter.status != "OPEN" or rules.missing(batch.category, batch.cold_chain, shelter):
            reroute_assignment(db, a, now, "SHELTER_UNAVAILABLE"); moved += 1
    order = {"CLAIMED": 0, "PICKED_UP": 1, "IN_TRANSIT": 2}      # move least-progressed first
    for d in db.scalars(select(ShelterDemand).where(ShelterDemand.shelter_id == shelter_id)).all():
        while d.received + d.in_transit > d.required:
            cands = db.scalars(select(Assignment).join(Batch, Batch.id == Assignment.batch_id).where(
                Assignment.shelter_id == shelter_id, Batch.category == d.category,
                Assignment.status.in_(tuple(order)))).all()
            if not cands: break
            a = sorted(cands, key=lambda x: (order[x.status], -x.id))[0]
            reroute_assignment(db, a, now, "DEMAND_FILLED"); moved += 1
            db.flush()
            db.refresh(d)
    return moved
