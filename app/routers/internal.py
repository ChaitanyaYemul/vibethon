import hmac
from fastapi import APIRouter, Depends, Header
from app import clock
from app.config import settings
from app.db import get_db
from app.engine.mutex import engine_lock
from app.engine.sweeper import run_sweep
from app.errors import ApiError

router = APIRouter(prefix="/internal")


@router.api_route("/sweep", methods=["GET", "POST"])
def sweep(authorization: str = Header(default=""), db=Depends(get_db)):
    if not hmac.compare_digest(authorization, "Bearer " + settings.cron_secret):
        raise ApiError(401, "UNAUTHORIZED", "Invalid cron credentials")
    engine_lock(db)
    result = run_sweep(db, clock.now(db))
    db.commit()
    return result
