from collections import defaultdict
from datetime import timedelta
from sqlalchemy import select, text
from app import clock
from app.constants import *
from app.models import Assignment, Rider, Batch
from app.engine.mutex import engine_lock
from app.engine.lifecycle import transition, recompute_batch_status
from app.engine.events import emit
from app.engine.batches import create_recovery_batch
from app.engine.feasibility import batch_deadline
from app.engine.planner import plan_batch

def run_sweep(db, now) -> dict:
    if not engine_lock(db, blocking=False):
        return {"skipped": True}
    stats = defaultdict(int)

    # 1. Expire ghost claims
    for a in db.scalars(select(Assignment).where(
            Assignment.status == "CLAIMED", Assignment.claim_expires_at < now).order_by(Assignment.id)).all():
        transition(db, a, "CLAIM_EXPIRED", now, "system", "TTL"); stats["claims_expired"] += 1

    # 2. Heartbeat: stale riders -> UNRESPONSIVE, handle cargo
    stale = db.scalars(select(Rider).where(
        Rider.status.in_(("AVAILABLE", "BUSY")),
        Rider.last_heartbeat_at < now - timedelta(seconds=HEARTBEAT_TIMEOUT_S)).order_by(Rider.id)).all()
    for r in stale:
        r.status = "UNRESPONSIVE"            # set first so release hooks won't flip it back
        emit(db, now, "system", "rider", r.id, "RIDER_SILENT", {})
        for a in db.scalars(select(Assignment).where(
                Assignment.rider_id == r.id, Assignment.status.in_(ACTIVE))).all():
            if a.status == "CLAIMED":
                transition(db, a, "CLAIM_EXPIRED", now, "system", "RIDER_SILENT")
            else:                            # cargo physically with the silent rider
                orig = db.get(Batch, a.batch_id)
                if now > a.deliver_by:
                    transition(db, a, "LOST", now, "system", "SPOILED")
                    stats["spoiled"] += 1
                else:
                    transition(db, a, "LOST", now, "system", "RIDER_SILENT")
                    create_recovery_batch(db, now, orig, a, r)
        stats["riders_silent"] += 1

    # 3. Spoilage: in-flight past deliver_by is never deliverable
    for a in db.scalars(select(Assignment).where(
            Assignment.status.in_(IN_FLIGHT), Assignment.deliver_by < now)).all():
        transition(db, a, "LOST", now, "system", "SPOILED"); stats["spoiled"] += 1

    from app.engine import rerouter

    # 4. Destinations that became invalid (closed / incompatible / overfilled)
    for sid in db.scalars(select(Assignment.shelter_id).where(
            Assignment.status.in_(ACTIVE), Assignment.shelter_id.is_not(None)).distinct()).all():
        stats["rerouted"] += rerouter.handle_shelter_change(db, sid, now)

    # 5. Retry stranded assignments (a shelter may have reopened / got demand)
    for a in db.scalars(select(Assignment).where(Assignment.status == "STRANDED")).all():
        rerouter.reroute_assignment(db, a, now, "RETRY")

    # 6. Batch cutoff: unassigned quantity past the safe deadline is wasted
    for b in db.scalars(select(Batch).where(
            Batch.status.in_(("AVAILABLE", "PARTIALLY_ASSIGNED")), Batch.quantity_unassigned > 0)).all():
        if now >= batch_deadline(b):
            b.quantity_wasted += b.quantity_unassigned
            emit(db, now, "system", "batch", b.id, "BATCH_CUTOFF", {"wasted": b.quantity_unassigned})
            b.quantity_unassigned = 0
            recompute_batch_status(db, b, now)

    # 7. Auto-plan, most urgent first
    for b in db.scalars(select(Batch).where(
            Batch.status.in_(("AVAILABLE", "PARTIALLY_ASSIGNED")), Batch.quantity_unassigned > 0)
            .order_by(Batch.expires_at.asc(), Batch.quantity_unassigned.desc())).all():
        plan_batch(db, b.id, now, commit=True)
    db.flush()
    return dict(stats)


def maybe_sweep(db, force=False):
    """Stage an idempotent sweep; the calling dependency/router owns commit."""
    if not engine_lock(db, blocking=False):
        return False
    real = clock.real_now()
    last = float(db.execute(text("select value from app_state where key='last_sweep_real'")).scalar() or 0)
    if not force and real.timestamp() - last < SWEEP_THROTTLE_S:
        return False
    run_sweep(db, clock.now(db))
    db.execute(text("update app_state set value=:v where key='last_sweep_real'"), {"v": str(real.timestamp())})
    return True

