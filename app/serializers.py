from sqlalchemy import inspect, select
from app.models import Assignment, Batch, Rider, Shelter
from app.engine.feasibility import eta_for


def record(obj):
    return {c.key: getattr(obj, c.key) for c in inspect(obj).mapper.column_attrs}


def assignment_detail(db, a, now):
    result = record(a)
    b, r = db.get(Batch, a.batch_id), db.get(Rider, a.rider_id)
    s = db.get(Shelter, a.shelter_id) if a.shelter_id else None
    result.update(item_name=b.item_name, category=b.category, rider_name=r.name,
                  rider_status=r.status, pickup={"lat": b.lat, "lng": b.lng},
                  destination={"id": s.id, "name": s.name, "lat": s.lat, "lng": s.lng} if s else None,
                  slack_min=eta_for(now, r, b, s, a.status != "CLAIMED").slack_min if s else None,
                  eta=eta_for(now, r, b, s, a.status != "CLAIMED").arrival if s else None)
    return result


def batch_detail(db, b, now):
    result = record(b)
    result["assignments"] = [assignment_detail(db, a, now) for a in db.scalars(select(Assignment).where(Assignment.batch_id == b.id).order_by(Assignment.id))]
    reasons = b.unfulfilled_reason.split(",") if b.unfulfilled_reason else []
    result.update(reasons=reasons, unfulfilled={"quantity_unassigned": b.quantity_unassigned,
                  "reasons": reasons, "wasted": b.quantity_wasted, "lost": b.quantity_lost})
    return result
