# mutex.py
from sqlalchemy import text
from app.constants import ENGINE_LOCK_KEY

def engine_lock(db, blocking=True) -> bool:
    if blocking:
        db.execute(text("select pg_advisory_xact_lock(:k)"), {"k": ENGINE_LOCK_KEY})
        return True
    return bool(db.execute(text("select pg_try_advisory_xact_lock(:k)"),
                           {"k": ENGINE_LOCK_KEY}).scalar())

