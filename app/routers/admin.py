from fastapi import APIRouter, Depends
from sqlalchemy import select
from app import clock
from app.constants import ACTIVE, IN_FLIGHT
from app.deps import read_db, require_role
from app.models import Donor, Rider, Shelter, Batch, Assignment, Event
from app.serializers import record, batch_detail, assignment_detail
from app.routers.shelters import shelter_detail

router = APIRouter(prefix="/admin")


@router.get("/overview")
def overview(user=Depends(require_role("admin")), db=Depends(read_db)):
    now = clock.now(db)
    batches = db.scalars(select(Batch).order_by(Batch.id)).all()
    assignments = db.scalars(select(Assignment).order_by(Assignment.id)).all()
    kpis = {"delivered": sum(a.quantity for a in assignments if a.status == "DELIVERED"),
            "in_flight": sum(a.quantity for a in assignments if a.status in IN_FLIGHT),
            "wasted": sum(b.quantity_wasted for b in batches), "lost": sum(b.quantity_lost for b in batches),
            "unassigned": sum(b.quantity_unassigned for b in batches),
            "claims_expired": sum(a.status == "CLAIM_EXPIRED" for a in assignments),
            "reroutes": sum(a.reroute_count for a in assignments),
            "stranded": sum(a.status == "STRANDED" for a in assignments)}
    return {"now": now, "kpis": kpis,
            "donors": [record(x) for x in db.scalars(select(Donor).order_by(Donor.id))],
            "riders": [record(x) for x in db.scalars(select(Rider).order_by(Rider.id))],
            "shelters": [shelter_detail(db, x) for x in db.scalars(select(Shelter).order_by(Shelter.id))],
            "batches": [batch_detail(db, x, now) for x in batches],
            "assignments": [assignment_detail(db, x, now) for x in assignments if x.status in ACTIVE],
            "events": [record(x) for x in db.scalars(select(Event).order_by(Event.id.desc()).limit(50))]}
