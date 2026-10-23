from app.models import Batch, Assignment, Rider, Shelter, ShelterDemand
from app.engine.events import emit
from app.engine import lifecycle
from dataclasses import dataclass, field
from datetime import timedelta
from sqlalchemy import select
from app import rules
from app.constants import *
from app.engine.feasibility import eta_for, batch_deadline, pickup_deadline, leg_min
from app.geo import haversine_km, travel_minutes

@dataclass
class Slice:
    rider_id: int; shelter_id: int; quantity: int
    arrival: object; slack_min: float

@dataclass
class PlanResult:
    slices: list = field(default_factory=list)
    unfulfilled: int = 0
    reasons: list = field(default_factory=list)   # NO_RIDER | NO_SHELTER_CAPACITY | INCOMPATIBLE | TIME_INFEASIBLE

def claim_ttl(now, deadline) -> timedelta:
    secs = (deadline - now).total_seconds()
    return timedelta(seconds=min(max(CLAIM_TTL_FRACTION * secs, CLAIM_TTL_MIN_S), CLAIM_TTL_MAX_S))

def plan_batch(db, batch_id, now, commit: bool, extra_exclude=()) -> PlanResult:
    batch = db.execute(select(Batch).where(Batch.id == batch_id).with_for_update()).scalar_one()
    deadline = batch_deadline(batch)
    remaining = batch.quantity_unassigned
    if batch.status in ("COMPLETED", "EXPIRED", "INFEASIBLE") or remaining == 0:
        return PlanResult([], remaining, [])
    if now >= deadline:
        return PlanResult([], remaining, ["TIME_INFEASIBLE"])

    # riders that already failed this batch are excluded (ghost-claim protection)
    excluded = set(extra_exclude) | set(db.scalars(
        select(Assignment.rider_id).where(
            Assignment.batch_id == batch.id,
            Assignment.status.in_(("CLAIM_EXPIRED", "LOST")))))
    rows = db.execute(
        select(ShelterDemand, Shelter)
        .join(Shelter, Shelter.id == ShelterDemand.shelter_id)
        .where(ShelterDemand.category == batch.category)
        .order_by(ShelterDemand.id)
        .with_for_update(of=ShelterDemand)).all()
    riders = db.scalars(
        select(Rider).where(
            Rider.status == "AVAILABLE",
            Rider.last_heartbeat_at >= now - timedelta(seconds=HEARTBEAT_TIMEOUT_S),
            Rider.id.notin_(excluded))
        .order_by(Rider.id).with_for_update()).all()

    open_rows   = [(d, s) for d, s in rows if s.status == "OPEN" and d.open_capacity > 0]
    compat_rows = [(d, s) for d, s in open_rows if not rules.missing(batch.category, batch.cold_chain, s)]
    caps = {d.id: d.open_capacity for d, _ in compat_rows}
    riders.sort(key=lambda r: leg_min(r.lat, r.lng, batch.lat, batch.lng, r.speed_kmph))

    slices, any_time_ok = [], False
    for r in riders:
        if remaining <= 0:
            break
        cands = []
        for d, s in compat_rows:
            if caps[d.id] <= 0:
                continue
            e = eta_for(now, r, batch, s, picked_up=False)
            if e.feasible:
                cands.append((d, s, e))
        if not cands:
            continue
        any_time_ok = True
        chunk = min(r.capacity_units, remaining)
        # prefer a shelter that can take the whole chunk, then earliest arrival
        d, s, e = min(cands, key=lambda c: (0 if caps[c[0].id] >= chunk else 1, c[2].arrival))
        qty = min(chunk, caps[d.id])
        slices.append(Slice(r.id, s.id, qty, e.arrival, e.slack_min))
        caps[d.id] -= qty
        remaining -= qty
        if commit:
            d.in_transit += qty
            batch.quantity_unassigned -= qty
            r.status = "BUSY"
            ttl = claim_ttl(now, deadline)
            pdl = pickup_deadline(batch, s, r)
            db.add(Assignment(
                batch_id=batch.id, rider_id=r.id, shelter_id=s.id, quantity=qty,
                status="CLAIMED", claim_expires_at=min(now + ttl, pdl),
                pickup_deadline=pdl, deliver_by=deadline, created_at=now, updated_at=now))
            emit(db, now, "system", "batch", batch.id, "ASSIGNED",
                 {"rider_id": r.id, "shelter_id": s.id, "quantity": qty, "slack_min": round(e.slack_min, 1)})

    reasons = []
    if remaining > 0:
        if not riders:               reasons = ["NO_RIDER"]
        elif not open_rows:          reasons = ["NO_SHELTER_CAPACITY"]
        elif not compat_rows:        reasons = ["INCOMPATIBLE"]
        elif not any_time_ok:        reasons = ["TIME_INFEASIBLE"]
        elif all(v <= 0 for v in caps.values()): reasons = ["NO_SHELTER_CAPACITY"]
        else:                        reasons = ["NO_RIDER"]
    if commit:
        batch.unfulfilled_reason = ",".join(reasons) or None
        db.flush()
        lifecycle.recompute_batch_status(db, batch, now)
        if remaining > 0 and slices:
            emit(db, now, "system", "batch", batch.id, "PLAN_PARTIAL",
                 {"unfulfilled": remaining, "reasons": reasons})
    return PlanResult(slices, remaining, reasons)
