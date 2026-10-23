from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from app.config import settings
from app.deps import read_db, require_role
from app.models import Event
from app.serializers import record

router = APIRouter()


@router.get("/events")
def events(since: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=1000), user=Depends(require_role("donor", "rider", "shelter", "admin")), db=Depends(read_db)):
    return [record(e) for e in db.scalars(select(Event).where(Event.id > since).order_by(Event.id).limit(limit))]


@router.get("/config")
def config():
    return {"supabase_url": settings.supabase_url, "supabase_anon_key": settings.supabase_anon_key, "demo_mode": settings.demo_mode}
