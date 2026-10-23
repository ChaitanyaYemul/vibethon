import csv
from pathlib import Path
from uuid import uuid5, NAMESPACE_DNS
from sqlalchemy import text
from app import clock
from app.config import settings
from app.db import SessionLocal
from app.models import User, Donor, Rider, Shelter, ShelterDemand
from app.schemas import Register, Demand
from app.security import hash_password
from app.engine.mutex import engine_lock

DATA = Path(__file__).resolve().parents[1] / "data"


def rows(name):
    with (DATA / (name + ".csv")).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def seed(db, now):
    engine_lock(db)
    db.execute(text("truncate assignments, batches, shelter_demands, shelters, riders, donors, users, events, idempotency_keys restart identity cascade"))
    db.expunge_all()
    db.execute(text("update app_state set value='0' where key in ('clock_offset_s','last_sweep_real')"))
    hashed = hash_password(settings.demo_password)
    admin = User(id=uuid5(NAMESPACE_DNS, "admin@demo.local"), name="Admin", email="admin@demo.local", password_hash=hashed, role="admin", created_at=now)
    db.add(admin)
    db.flush()
    shelters = {}
    for role, model in [("donor", Donor), ("rider", Rider), ("shelter", Shelter)]:
        for row in rows(role + "s"):
            status = row.pop("status", None)
            body = Register(**row, role=role, email=row["name"].lower().replace(" ", ".") + "@demo.local", password=settings.demo_password)
            user = User(id=uuid5(NAMESPACE_DNS, body.email), name=body.name, email=body.email, password_hash=hashed, role=role, created_at=now)
            db.add(user)
            db.flush()
            fields = dict(user_id=user.id, name=body.name, lat=body.lat, lng=body.lng)
            if role == "rider":
                if status not in ("AVAILABLE", "BUSY", "UNRESPONSIVE", "OFFLINE"):
                    raise ValueError("Invalid rider status")
                fields.update(capacity_units=body.capacity_units, speed_kmph=body.speed_kmph, status=status, last_heartbeat_at=now)
            elif role == "shelter":
                if status not in ("OPEN", "CLOSED"):
                    raise ValueError("Invalid shelter status")
                fields.update(status=status, has_clean_water=body.has_clean_water, has_cooking_fuel=body.has_cooking_fuel, has_refrigeration=body.has_refrigeration)
            profile = model(**fields)
            db.add(profile)
            db.flush()
            if role == "shelter":
                shelters[body.name] = profile.id
    for row in rows("shelter_demands"):
        sid = shelters[row.pop("shelter_name")]
        body = Demand(**row)
        db.add(ShelterDemand(shelter_id=sid, category=body.category, required=body.required))
    db.flush()
    return admin


if __name__ == "__main__":
    with SessionLocal() as db:
        seed(db, clock.real_now())
        db.commit()
    print("Seeded. Login: admin@demo.local (password from DEMO_PASSWORD).")
