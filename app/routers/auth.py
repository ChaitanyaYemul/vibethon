from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from app import clock
from app.db import get_db
from app.deps import PROFILES, current_user, entity_id
from app.engine.mutex import engine_lock
from app.errors import ApiError
from app.models import User
from app.schemas import Register, Login
from app.security import limiter, hash_password, check_password, token_for

router = APIRouter()


def credentials(db, user):
    return {"token": token_for(user), "role": user.role, "entity_id": entity_id(db, user)}


@router.post("/auth/register", status_code=201)
@limiter.limit("10/minute")
def register(request: Request, body: Register, db=Depends(get_db)):
    engine_lock(db)
    now = clock.now(db)
    if db.scalar(select(User).where(User.email == body.email)):
        raise ApiError(409, "EMAIL_EXISTS", "Email already registered")
    user = User(name=body.name, email=body.email, password_hash=hash_password(body.password), role=body.role, created_at=now)
    db.add(user)
    db.flush()
    fields = dict(user_id=user.id, name=body.name, lat=body.lat, lng=body.lng)
    if body.role == "rider":
        fields.update(capacity_units=body.capacity_units, speed_kmph=body.speed_kmph, last_heartbeat_at=now)
    elif body.role == "shelter":
        fields.update(has_clean_water=body.has_clean_water, has_cooking_fuel=body.has_cooking_fuel, has_refrigeration=body.has_refrigeration)
    db.add(PROFILES[body.role](**fields))
    db.flush()
    result = credentials(db, user)
    db.commit()
    return result


@router.post("/auth/login")
@limiter.limit("10/minute")
def login(request: Request, body: Login, db=Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if not user or not check_password(body.password, user.password_hash):
        raise ApiError(401, "UNAUTHORIZED", "Invalid credentials")
    return credentials(db, user)


@router.get("/me")
def me(user=Depends(current_user), db=Depends(get_db)):
    return {"user_id": str(user.id), "role": user.role, "entity_id": entity_id(db, user)}
