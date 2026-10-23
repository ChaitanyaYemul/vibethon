from sqlalchemy import text
from datetime import datetime, timedelta, timezone

def real_now() -> datetime:
    return datetime.now(timezone.utc)

def get_offset(db) -> timedelta:
    v = db.execute(text("select value from app_state where key='clock_offset_s'")).scalar()
    return timedelta(seconds=float(v or 0))

def now(db) -> datetime:
    return real_now() + get_offset(db)
