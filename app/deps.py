from uuid import UUID
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from app.db import get_db
from app.engine.mutex import engine_lock
from app.errors import ApiError
from app.models import User, Donor, Rider, Shelter
from app.security import decode_token

bearer = HTTPBearer(auto_error=False)
PROFILES = {"donor": Donor, "rider": Rider, "shelter": Shelter}


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db=Depends(get_db)):
    if not credentials:
        raise ApiError(401, "UNAUTHORIZED", "Authentication required")
    claims = decode_token(credentials.credentials)
    try:
        uid = UUID(claims["sub"])
    except (ValueError, TypeError):
        raise ApiError(401, "UNAUTHORIZED", "Invalid token subject") from None
    user = db.get(User, uid)
    if not user or user.role != claims["role"]:
        raise ApiError(401, "UNAUTHORIZED", "Unknown user")
    return user


def require_role(*roles):
    def dependency(user=Depends(current_user)):
        if user.role not in roles:
            raise ApiError(403, "FORBIDDEN", "Role is not allowed")
        return user
    return dependency


def entity_id(db, user):
    model = PROFILES.get(user.role)
    return db.scalar(select(model.id).where(model.user_id == user.id)) if model else None


def own(db, model, ident, user):
    entity = db.get(model, ident)
    if entity is None or (user.role != "admin" and entity.user_id != user.id):
        raise ApiError(404, "NOT_FOUND", "Entity not found")
    return entity


def read_db(db=Depends(get_db)):
    from app.engine.sweeper import maybe_sweep
    if maybe_sweep(db):
        db.commit()
    return db


def write_db(db=Depends(get_db)):
    engine_lock(db)
    from app.engine.sweeper import maybe_sweep
    maybe_sweep(db)
    return db
