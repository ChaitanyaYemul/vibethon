# AGENTS.md — Last-Mile Spoilage Trap: Dispatch Platform

> Implementation spec for Codex. **Read all of it before writing code.**
> Build exactly what is specified. Do not ask questions; where something is unspecified, choose the simplest option consistent with this file.
> **Budget: 2 hours. No AI/ML. Deterministic, rule-based Python only.**

---

## 0. Operating Rules for the Agent

1. Follow the **build order in section 16**. After each milestone: run `pytest -q`, then `git add -A && git commit -m "<message from section 16>"`. Never batch milestones into one commit.
2. Engine code (`app/engine/*`) **never calls `datetime.now()`**. It receives `now` as a parameter. Only `app/clock.py` reads wall time.
3. Engine code **never commits**. Routers commit explicitly (`db.commit()`); `get_db` rolls back on exception.
4. Assignment status changes happen **only** through `lifecycle.transition()`.
5. Every mutating operation first takes the **engine mutex** (section 7.1). This serializes the engine, so no deadlocks and no oversubscription.
6. Quantity is never lost silently: `total = unassigned + wasted + lost + Σactive + Σdelivered` must hold for every batch at all times (tested).
7. Compatibility is enforced in the planner, the re-router, and at pickup/deliver.
8. Every route has a role check and an ownership check (admin bypasses ownership).
9. No secrets in git. `.env` is gitignored; `.env.example` is committed.
10. If time runs short, cut UI polish first. Never cut engine logic or tests.

---

## 1. Mission and Judge Matrix

Real-time coordination platform connecting **Donors → Riders → Shelters** for perishable aid. Time, rider capacity, shelter demand, item compatibility and rider unreliability are first-class variables.

| # | Judge test | Required behavior | Where |
|---|---|---|---|
| J1 | Batch with tight expiry | Flag `INFEASIBLE`, HTTP 422 with reason and best-case ETA; urgent batches planned first | 7.3, 7.5 |
| J2 | Split pickup across riders | Several assignments, Σ qty + leftover = batch qty, leftover has a reason code | 7.5 |
| J3 | Unresponsive volunteer / ghost claim | Claim auto-expires, quantity returns to pool, batch re-planned excluding that rider | 7.6, 7.8 |
| J4 | Shelter closes or fills while rider in transit | Rider re-routed synchronously to best feasible fallback, else `STRANDED` with alert | 7.9 |
| J5 | Unsafe pairing (rations to no-fuel site, formula to no-water site) | Blocked in planner, re-router and pickup | 7.2 |
| J6 | Live visibility | All roles see changes in ~1–2 s (Supabase Realtime, polling fallback) | 10 |
| J7 | Commit history | 11 milestone commits minimum | 16 |

---

## 2. Architecture and Platform Constraints

| Constraint | Decision |
|---|---|
| Backend | FastAPI, Python 3.12 |
| DB | PostgreSQL 16 locally (Docker); Supabase Postgres in prod |
| Hosting | Vercel (FastAPI serverless function + static `public/`) |
| Real-time | **Supabase Realtime** (browser ↔ Supabase WebSocket on the `events` table). Fallback: poll `GET /api/events?since=` every 2 s. **No FastAPI WebSockets** (Vercel serverless cannot hold them) |
| Background jobs | None persistent. One idempotent `run_sweep(db, now)` is invoked (a) lazily at the start of every request (throttled to 1/s), (b) by Vercel Cron, (c) forced after time-travel and shelter/rider state changes |
| Time | Simulated clock offset stored **in the DB** (`app_state`), because serverless instances share no memory |
| Data | Synthetic CSVs in `data/` seed donors, riders, shelters, demand and demo batches. **Rules stay in code**, data in CSV |

```
Browser ──REST (JWT)──▶ Vercel FastAPI ──SQL──▶ Supabase Postgres
   ▲                                                  │ INSERT into events
   └──────── Supabase Realtime (WebSocket) ◀──────────┘
```

---

## 3. Stack, Dependencies, Local Setup

`requirements.txt`
```
fastapi>=0.115
uvicorn[standard]>=0.30
sqlalchemy>=2.0.30
psycopg[binary]>=3.2
pydantic>=2.7
pydantic-settings>=2.3
PyJWT>=2.8
bcrypt>=4.1
slowapi>=0.1.9
pytest>=8
httpx>=0.27
```
(Use `bcrypt` directly, not passlib.)

`docker-compose.yml`
```yaml
services:
  db:
    image: postgres:16
    environment: { POSTGRES_PASSWORD: postgres, POSTGRES_DB: spoilage }
    ports: ["5432:5432"]
  db_test:
    image: postgres:16
    environment: { POSTGRES_PASSWORD: postgres, POSTGRES_DB: spoilage_test }
    ports: ["5433:5432"]
```

`.env.example`
```
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/spoilage
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/spoilage_test
JWT_SECRET=change-me-long-random
JWT_EXP_MINUTES=60
CRON_SECRET=change-me
ALLOWED_ORIGINS=http://localhost:8000
DEMO_MODE=true
DEMO_PASSWORD=demo1234
SUPABASE_URL=
SUPABASE_ANON_KEY=
```

Run: `docker compose up -d` → `psql < schema.sql` (or `python -m scripts.init_db`) → `python -m scripts.seed` → `uvicorn app.main:app --reload` → open `http://localhost:8000`.

---

## 4. Repo Layout

```
/
├── AGENTS.md  README.md  schema.sql  supabase_extras.sql
├── requirements.txt  docker-compose.yml  vercel.json  .env.example  .gitignore
├── api/index.py                  # from app.main import app
├── app/
│   ├── main.py                   # app, CORS, error handlers, routers, static mount
│   ├── config.py  constants.py  db.py  models.py  schemas.py
│   ├── clock.py  geo.py  rules.py  errors.py  security.py  deps.py
│   ├── engine/
│   │   ├── mutex.py  events.py  feasibility.py  planner.py
│   │   ├── lifecycle.py  rerouter.py  sweeper.py  batches.py
│   └── routers/
│       ├── auth.py  batches.py  riders.py  shelters.py  assignments.py
│       ├── events.py  admin.py  demo.py  internal.py
├── public/index.html             # single-file UI
├── data/                         # synthetic CSVs (section 11)
├── scripts/init_db.py  scripts/seed.py
└── tests/ conftest.py + test_*.py
```

All HTTP routes are prefixed **`/api`**. Static UI is served from `public/` (Vercel serves it directly; locally FastAPI mounts it at `/` **after** the API routers).

---

## 5. Database (`schema.sql`, source of truth)

```sql
create extension if not exists pgcrypto;

create table users (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  email text not null unique,
  password_hash text not null,
  role text not null check (role in ('donor','rider','shelter','admin')),
  created_at timestamptz not null default now()
);

create table donors (
  id serial primary key,
  user_id uuid not null references users(id) on delete cascade,
  name text not null,
  lat double precision not null check (lat between -90 and 90),
  lng double precision not null check (lng between -180 and 180)
);

create table riders (
  id serial primary key,
  user_id uuid not null references users(id) on delete cascade,
  name text not null,
  capacity_units int not null check (capacity_units > 0),
  speed_kmph double precision not null default 25 check (speed_kmph > 0),
  lat double precision not null,
  lng double precision not null,
  status text not null default 'AVAILABLE'
    check (status in ('AVAILABLE','BUSY','UNRESPONSIVE','OFFLINE')),
  last_heartbeat_at timestamptz not null default now(),
  demo_silenced boolean not null default false
);

create table shelters (
  id serial primary key,
  user_id uuid not null references users(id) on delete cascade,
  name text not null,
  lat double precision not null,
  lng double precision not null,
  status text not null default 'OPEN' check (status in ('OPEN','CLOSED')),
  has_clean_water boolean not null default true,
  has_cooking_fuel boolean not null default true,
  has_refrigeration boolean not null default false
);

create table shelter_demands (
  id serial primary key,
  shelter_id int not null references shelters(id) on delete cascade,
  category text not null check (category in
    ('COOKED_FOOD','RAW_RATION','INFANT_FORMULA','MEDICAL','KIT')),
  required int not null check (required >= 0),
  received int not null default 0 check (received >= 0),
  in_transit int not null default 0 check (in_transit >= 0),
  unique (shelter_id, category)
  -- NOTE: no (received+in_transit <= required) CHECK. Overflow is a legal transient
  -- state (walk-in donation, demand cut) that the re-router resolves in the same transaction.
);

create table batches (
  id serial primary key,
  donor_id int not null references donors(id) on delete cascade,
  parent_batch_id int references batches(id),
  is_recovery boolean not null default false,
  category text not null check (category in
    ('COOKED_FOOD','RAW_RATION','INFANT_FORMULA','MEDICAL','KIT')),
  item_name text not null,
  cold_chain boolean not null default false,
  quantity_total int not null check (quantity_total > 0),
  quantity_unassigned int not null check (quantity_unassigned >= 0),
  quantity_wasted int not null default 0 check (quantity_wasted >= 0),
  quantity_lost int not null default 0 check (quantity_lost >= 0),
  ready_at timestamptz not null,
  expires_at timestamptz not null,
  safety_buffer_min int not null default 10 check (safety_buffer_min >= 0),
  lat double precision not null,
  lng double precision not null,
  status text not null default 'AVAILABLE' check (status in
    ('AVAILABLE','PARTIALLY_ASSIGNED','FULLY_ASSIGNED','COMPLETED','EXPIRED','INFEASIBLE')),
  unfulfilled_reason text,
  created_at timestamptz not null default now(),
  check (expires_at > ready_at),
  check (quantity_unassigned + quantity_wasted + quantity_lost <= quantity_total)
);
create index on batches (status, expires_at);

create table assignments (
  id serial primary key,
  batch_id int not null references batches(id) on delete cascade,
  rider_id int not null references riders(id),
  shelter_id int references shelters(id),          -- null only while STRANDED
  quantity int not null check (quantity > 0),
  status text not null check (status in
    ('CLAIMED','PICKED_UP','IN_TRANSIT','DELIVERED','STRANDED',
     'CLAIM_EXPIRED','CANCELLED','LOST')),
  reason text,
  claim_expires_at timestamptz not null,
  pickup_deadline timestamptz not null,
  deliver_by timestamptz not null,
  picked_up_at timestamptz,
  delivered_at timestamptz,
  reroute_count int not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index on assignments (status, claim_expires_at);
create index on assignments (batch_id);
-- DB-level guarantee: a rider holds at most one active assignment
create unique index one_active_per_rider on assignments (rider_id)
  where status in ('CLAIMED','PICKED_UP','IN_TRANSIT','STRANDED');

create table events (
  id bigserial primary key,
  ts timestamptz not null,
  actor text not null,
  entity_type text not null,
  entity_id int,
  event_type text not null,
  payload jsonb not null default '{}'::jsonb   -- NO personal data, ids/qty/status only
);

create table idempotency_keys (
  key text not null,
  user_id uuid not null,
  response jsonb not null,
  created_at timestamptz not null default now(),
  primary key (key, user_id)
);

create table app_state (key text primary key, value text not null);
insert into app_state values ('clock_offset_s','0'), ('last_sweep_real','0');
```

`supabase_extras.sql` (run manually in the Supabase SQL editor; not needed locally)
```sql
do $$ begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    alter publication supabase_realtime add table events;
  end if;
end $$;

alter table users enable row level security;
alter table donors enable row level security;
alter table riders enable row level security;
alter table shelters enable row level security;
alter table shelter_demands enable row level security;
alter table batches enable row level security;
alter table assignments enable row level security;
alter table events enable row level security;
alter table idempotency_keys enable row level security;
alter table app_state enable row level security;

-- Only `events` is readable by the browser (anon) for Realtime. Payloads hold no PII.
create policy "events_read" on events for select to anon using (true);
```
FastAPI connects with the owner role (bypasses RLS). The browser can read **only** `events`.

### Model helper properties (`models.py`, SQLAlchemy 2.0 `Mapped[...]` style, mirror the schema)
```python
class ShelterDemand(Base):
    ...
    @property
    def open_capacity(self) -> int:
        return max(0, self.required - self.received - self.in_transit)
```

---

## 6. Constants (`app/constants.py`)

```python
LOAD_MIN = 3                 # minutes to load cargo at donor
ROAD_FACTOR = 1.3            # haversine -> road distance
DEFAULT_SPEED_KMPH = 25
HEARTBEAT_TIMEOUT_S = 90
CLAIM_TTL_FRACTION = 0.20    # of time-to-deadline
CLAIM_TTL_MIN_S = 120
CLAIM_TTL_MAX_S = 300
SWEEP_THROTTLE_S = 1
ENGINE_LOCK_KEY = 727001
ACTIVE = ("CLAIMED", "PICKED_UP", "IN_TRANSIT", "STRANDED")
IN_FLIGHT = ("PICKED_UP", "IN_TRANSIT", "STRANDED")
```

---

## 7. Engine (implement these modules as written, complete the plumbing)

### 7.1 `engine/mutex.py` and `clock.py`

```python
# mutex.py
from sqlalchemy import text
from app.constants import ENGINE_LOCK_KEY

def engine_lock(db, blocking=True) -> bool:
    if blocking:
        db.execute(text("select pg_advisory_xact_lock(:k)"), {"k": ENGINE_LOCK_KEY})
        return True
    return bool(db.execute(text("select pg_try_advisory_xact_lock(:k)"),
                           {"k": ENGINE_LOCK_KEY}).scalar())

# clock.py  (the ONLY place wall time is read)
from datetime import datetime, timedelta, timezone

def real_now() -> datetime:
    return datetime.now(timezone.utc)

def get_offset(db) -> timedelta:
    v = db.execute(text("select value from app_state where key='clock_offset_s'")).scalar()
    return timedelta(seconds=float(v or 0))

def now(db) -> datetime:
    return real_now() + get_offset(db)
```
Every mutating route: `engine_lock(db)` first, then `now = clock.now(db)`, then pass `now` down.

### 7.2 `rules.py` and `geo.py`

```python
# rules.py — single source of truth for compatibility
REQUIREMENTS = {
    "RAW_RATION":     ("has_cooking_fuel", "has_clean_water"),
    "INFANT_FORMULA": ("has_clean_water",),
    "COOKED_FOOD":    (),
    "KIT":            (),
    "MEDICAL":        (),
}
def missing(category: str, cold_chain: bool, shelter) -> list[str]:
    """Return unmet requirements. Empty list == compatible."""
    miss = [f for f in REQUIREMENTS[category] if not getattr(shelter, f)]
    if cold_chain and not shelter.has_refrigeration:
        miss.append("has_refrigeration")
    return miss

# geo.py
from math import radians, sin, cos, asin, sqrt
from app.constants import ROAD_FACTOR
def haversine_km(lat1, lng1, lat2, lng2):
    p1, p2 = radians(lat1), radians(lat2)
    dphi, dl = p2 - p1, radians(lng2 - lng1)
    a = sin(dphi/2)**2 + cos(p1)*cos(p2)*sin(dl/2)**2
    return 2 * 6371.0 * asin(sqrt(a))
def travel_minutes(km, speed_kmph):
    return km * ROAD_FACTOR / speed_kmph * 60
```

### 7.3 `engine/feasibility.py`

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
from app.constants import LOAD_MIN
from app.geo import haversine_km, travel_minutes

@dataclass
class Eta:
    arrival: datetime
    deadline: datetime
    slack_min: float
    feasible: bool

def batch_deadline(batch) -> datetime:
    return batch.expires_at - timedelta(minutes=batch.safety_buffer_min)

def leg_min(a_lat, a_lng, b_lat, b_lng, speed):
    return travel_minutes(haversine_km(a_lat, a_lng, b_lat, b_lng), speed)

def eta_for(now, rider, batch, shelter, picked_up=False) -> Eta:
    dl = batch_deadline(batch)
    if picked_up:   # cargo already on the rider: rider position -> shelter
        arrival = now + timedelta(minutes=leg_min(rider.lat, rider.lng, shelter.lat, shelter.lng, rider.speed_kmph))
    else:           # rider -> donor (wait until ready) -> load -> shelter
        reach = now + timedelta(minutes=leg_min(rider.lat, rider.lng, batch.lat, batch.lng, rider.speed_kmph))
        pickup = max(reach, batch.ready_at)
        arrival = (pickup + timedelta(minutes=LOAD_MIN)
                   + timedelta(minutes=leg_min(batch.lat, batch.lng, shelter.lat, shelter.lng, rider.speed_kmph)))
    slack = (dl - arrival).total_seconds() / 60
    return Eta(arrival, dl, slack, slack >= 0)

def pickup_deadline(batch, shelter, rider) -> datetime:
    """Latest moment pickup can start and still reach the shelter before the deadline."""
    return (batch_deadline(batch)
            - timedelta(minutes=LOAD_MIN)
            - timedelta(minutes=leg_min(batch.lat, batch.lng, shelter.lat, shelter.lng, rider.speed_kmph)))
```

### 7.4 `engine/events.py`

```python
def emit(db, now, actor, entity_type, entity_id, event_type, payload=None):
    db.add(Event(ts=now, actor=actor, entity_type=entity_type, entity_id=entity_id,
                 event_type=event_type, payload=payload or {}))
```
Event types: `BATCH_CREATED, BATCH_INFEASIBLE, ASSIGNED, PLAN_PARTIAL, CLAIM_EXPIRED, RIDER_SILENT, PICKED_UP, IN_TRANSIT, REROUTED, STRANDED, DELIVERED, SPOILED, RECOVERY_CREATED, RECOVERY_INFEASIBLE, BATCH_CUTOFF, SHELTER_CLOSED, SHELTER_OPENED, SHELTER_FLAGS, WALK_IN, DEMAND_CHANGED, RIDER_ONLINE, CLOCK_JUMP, ASSIGNMENT_CANCELLED`.
Payloads: ids, quantities, statuses, reason codes only. **Never** names, emails or coordinates of people.

### 7.5 `engine/planner.py` — time-aware allocation and splitting (J1, J2)

```python
from dataclasses import dataclass, field
from datetime import timedelta
from sqlalchemy import select
from app import rules
from app.constants import *
from app.engine.feasibility import eta_for, batch_deadline, pickup_deadline, leg_min
from app.geo import haversine_km, travel_minutes

@dataclass
class Slice:
    rider_id: int; shelter_id: int; quantity: int
    arrival: object; slack_min: float

@dataclass
class PlanResult:
    slices: list = field(default_factory=list)
    unfulfilled: int = 0
    reasons: list = field(default_factory=list)   # NO_RIDER | NO_SHELTER_CAPACITY | INCOMPATIBLE | TIME_INFEASIBLE

def claim_ttl(now, deadline) -> timedelta:
    secs = (deadline - now).total_seconds()
    return timedelta(seconds=min(max(CLAIM_TTL_FRACTION * secs, CLAIM_TTL_MIN_S), CLAIM_TTL_MAX_S))

def plan_batch(db, batch_id, now, commit: bool, extra_exclude=()) -> PlanResult:
    batch = db.execute(select(Batch).where(Batch.id == batch_id).with_for_update()).scalar_one()
    deadline = batch_deadline(batch)
    remaining = batch.quantity_unassigned
    if batch.status in ("COMPLETED", "EXPIRED", "INFEASIBLE") or remaining == 0:
        return PlanResult([], remaining, [])
    if now >= deadline:
        return PlanResult([], remaining, ["TIME_INFEASIBLE"])

    # riders that already failed this batch are excluded (ghost-claim protection)
    excluded = set(extra_exclude) | set(db.scalars(
        select(Assignment.rider_id).where(
            Assignment.batch_id == batch.id,
            Assignment.status.in_(("CLAIM_EXPIRED", "LOST")))))
    rows = db.execute(
        select(ShelterDemand, Shelter)
        .join(Shelter, Shelter.id == ShelterDemand.shelter_id)
        .where(ShelterDemand.category == batch.category)
        .order_by(ShelterDemand.id)
        .with_for_update(of=ShelterDemand)).all()
    riders = db.scalars(
        select(Rider).where(
            Rider.status == "AVAILABLE",
            Rider.last_heartbeat_at >= now - timedelta(seconds=HEARTBEAT_TIMEOUT_S),
            Rider.id.notin_(excluded))
        .order_by(Rider.id).with_for_update()).all()

    open_rows   = [(d, s) for d, s in rows if s.status == "OPEN" and d.open_capacity > 0]
    compat_rows = [(d, s) for d, s in open_rows if not rules.missing(batch.category, batch.cold_chain, s)]
    caps = {d.id: d.open_capacity for d, _ in compat_rows}
    riders.sort(key=lambda r: leg_min(r.lat, r.lng, batch.lat, batch.lng, r.speed_kmph))

    slices, any_time_ok = [], False
    for r in riders:
        if remaining <= 0:
            break
        cands = []
        for d, s in compat_rows:
            if caps[d.id] <= 0:
                continue
            e = eta_for(now, r, batch, s, picked_up=False)
            if e.feasible:
                cands.append((d, s, e))
        if not cands:
            continue
        any_time_ok = True
        chunk = min(r.capacity_units, remaining)
        # prefer a shelter that can take the whole chunk, then earliest arrival
        d, s, e = min(cands, key=lambda c: (0 if caps[c[0].id] >= chunk else 1, c[2].arrival))
        qty = min(chunk, caps[d.id])
        slices.append(Slice(r.id, s.id, qty, e.arrival, e.slack_min))
        caps[d.id] -= qty
        remaining -= qty
        if commit:
            d.in_transit += qty
            batch.quantity_unassigned -= qty
            r.status = "BUSY"
            ttl = claim_ttl(now, deadline)
            pdl = pickup_deadline(batch, s, r)
            db.add(Assignment(
                batch_id=batch.id, rider_id=r.id, shelter_id=s.id, quantity=qty,
                status="CLAIMED", claim_expires_at=min(now + ttl, pdl),
                pickup_deadline=pdl, deliver_by=deadline, created_at=now, updated_at=now))
            emit(db, now, "system", "batch", batch.id, "ASSIGNED",
                 {"rider_id": r.id, "shelter_id": s.id, "quantity": qty, "slack_min": round(e.slack_min, 1)})

    reasons = []
    if remaining > 0:
        if not riders:               reasons = ["NO_RIDER"]
        elif not open_rows:          reasons = ["NO_SHELTER_CAPACITY"]
        elif not compat_rows:        reasons = ["INCOMPATIBLE"]
        elif not any_time_ok:        reasons = ["TIME_INFEASIBLE"]
        elif all(v <= 0 for v in caps.values()): reasons = ["NO_SHELTER_CAPACITY"]
        else:                        reasons = ["NO_RIDER"]
    if commit:
        batch.unfulfilled_reason = ",".join(reasons) or None
        db.flush()
        lifecycle.recompute_batch_status(db, batch, now)
        if remaining > 0 and slices:
            emit(db, now, "system", "batch", batch.id, "PLAN_PARTIAL",
                 {"unfulfilled": remaining, "reasons": reasons})
    return PlanResult(slices, remaining, reasons)
```

**Auto-plan priority:** whenever many batches need planning (sweeper), process `batches` where `quantity_unassigned > 0 AND status IN ('AVAILABLE','PARTIALLY_ASSIGNED')` ordered by `expires_at ASC, quantity_unassigned DESC`.

### 7.6 `engine/lifecycle.py` — guarded state machine (J3)

```python
from app.constants import ACTIVE
ALLOWED = {
    "CLAIMED":   {"PICKED_UP", "CLAIM_EXPIRED", "CANCELLED", "LOST"},
    "PICKED_UP": {"IN_TRANSIT", "STRANDED", "LOST"},
    "IN_TRANSIT":{"DELIVERED", "STRANDED", "LOST"},
    "STRANDED":  {"IN_TRANSIT", "LOST"},
}

def transition(db, a, to, now, actor, reason=None):
    if to not in ALLOWED.get(a.status, set()):
        raise ApiError(409, "INVALID_TRANSITION", f"{a.status} -> {to} not allowed")
    batch = db.execute(select(Batch).where(Batch.id == a.batch_id).with_for_update()).scalar_one()
    frm, old_shelter = a.status, a.shelter_id
    a.status, a.reason, a.updated_at = to, reason, now
    if to == "PICKED_UP":
        a.picked_up_at = now
    if to in ("CLAIM_EXPIRED", "CANCELLED"):
        batch.quantity_unassigned += a.quantity
        _release_demand(db, a, batch)
        _release_rider(db, a.rider_id)
    elif to == "LOST":
        if a.shelter_id:
            _release_demand(db, a, batch)
        if reason == "SPOILED": batch.quantity_wasted += a.quantity
        else:                   batch.quantity_lost   += a.quantity   # RIDER_SILENT
        _release_rider(db, a.rider_id)
    elif to == "DELIVERED":
        d = _demand(db, a.shelter_id, batch.category)
        d.in_transit -= a.quantity
        d.received += a.quantity
        a.delivered_at = now
        _release_rider(db, a.rider_id)
    elif to == "STRANDED":
        _release_demand(db, a, batch)
        a.shelter_id = None
    db.flush()
    recompute_batch_status(db, batch, now)
    emit(db, now, actor, "assignment", a.id, {
        "CLAIM_EXPIRED": "CLAIM_EXPIRED", "CANCELLED": "ASSIGNMENT_CANCELLED",
        "PICKED_UP": "PICKED_UP", "IN_TRANSIT": "IN_TRANSIT", "DELIVERED": "DELIVERED",
        "STRANDED": "STRANDED", "LOST": "SPOILED" if reason == "SPOILED" else "RIDER_SILENT",
    }[to], {"from": frm, "batch_id": a.batch_id, "rider_id": a.rider_id,
            "shelter_id": old_shelter, "quantity": a.quantity, "reason": reason})

def _demand(db, shelter_id, category):
    return db.execute(select(ShelterDemand).where(
        ShelterDemand.shelter_id == shelter_id, ShelterDemand.category == category
    ).with_for_update()).scalar_one()

def _release_demand(db, a, batch):
    if a.shelter_id:
        d = _demand(db, a.shelter_id, batch.category)
        d.in_transit = max(0, d.in_transit - a.quantity)

def _release_rider(db, rider_id):
    r = db.execute(select(Rider).where(Rider.id == rider_id).with_for_update()).scalar_one()
    if r.status == "BUSY":          # never flip UNRESPONSIVE / OFFLINE back automatically
        r.status = "AVAILABLE"

def recompute_batch_status(db, batch, now):
    if batch.status == "INFEASIBLE":
        return
    db.flush()
    active = db.scalar(select(func.count()).where(Assignment.batch_id == batch.id,
                                                  Assignment.status.in_(ACTIVE)))
    delivered = db.scalar(select(func.coalesce(func.sum(Assignment.quantity), 0)).where(
        Assignment.batch_id == batch.id, Assignment.status == "DELIVERED"))
    if batch.quantity_unassigned > 0:
        batch.status = "PARTIALLY_ASSIGNED" if (active or delivered) else "AVAILABLE"
    else:
        batch.status = "FULLY_ASSIGNED" if active else ("COMPLETED" if delivered else "EXPIRED")
```

State diagram:
```
CLAIMED ──pickup──▶ PICKED_UP ──depart──▶ IN_TRANSIT ──deliver──▶ DELIVERED
   │ ttl/cancel         │                      │
   ▼                    └──── shelter invalid ─┴──▶ STRANDED ──fallback found──▶ IN_TRANSIT
CLAIM_EXPIRED/CANCELLED          any active ──silent/spoiled──▶ LOST
```

### 7.7 `engine/batches.py` — create batch, feasibility gate, recovery

```python
def create_batch(db, now, donor, body, actor) -> tuple[Batch, PlanResult | None]:
    b = Batch(donor_id=donor.id, category=body.category, item_name=body.item_name,
              cold_chain=body.cold_chain, quantity_total=body.quantity_total,
              quantity_unassigned=body.quantity_total,
              ready_at=now + timedelta(minutes=body.ready_in_min),
              expires_at=now + timedelta(minutes=body.expires_in_min),
              safety_buffer_min=body.safety_buffer_min, lat=donor.lat, lng=donor.lng)
    db.add(b); db.flush()
    emit(db, now, actor, "batch", b.id, "BATCH_CREATED",
         {"category": b.category, "quantity": b.quantity_total, "expires_in_min": body.expires_in_min})
    dry = plan_batch(db, b.id, now, commit=False)
    if not dry.slices and set(dry.reasons) & {"TIME_INFEASIBLE", "INCOMPATIBLE"}:
        b.status, b.unfulfilled_reason = "INFEASIBLE", ",".join(dry.reasons)
        emit(db, now, "system", "batch", b.id, "BATCH_INFEASIBLE", {"reasons": dry.reasons})
        return b, dry                      # router returns HTTP 422 (batch persisted, flagged)
    return b, (plan_batch(db, b.id, now, commit=True) if body.auto_dispatch else None)

def create_recovery_batch(db, now, orig, a, rider):
    """Cargo is stuck with a silent rider at its last known position: rescue it."""
    rb = Batch(donor_id=orig.donor_id, parent_batch_id=orig.id, is_recovery=True,
               category=orig.category, item_name=orig.item_name, cold_chain=orig.cold_chain,
               quantity_total=a.quantity, quantity_unassigned=a.quantity,
               ready_at=now, expires_at=orig.expires_at, safety_buffer_min=orig.safety_buffer_min,
               lat=rider.lat, lng=rider.lng)
    db.add(rb); db.flush()
    emit(db, now, "system", "batch", rb.id, "RECOVERY_CREATED", {"parent": orig.id, "quantity": a.quantity})
    dry = plan_batch(db, rb.id, now, commit=False)
    if not dry.slices and dry.reasons and dry.reasons[0] != "NO_RIDER":
        rb.status, rb.unfulfilled_reason = "INFEASIBLE", ",".join(dry.reasons)
        emit(db, now, "system", "batch", rb.id, "RECOVERY_INFEASIBLE", {"reasons": dry.reasons})
```
(If the recovery batch is feasible-but-no-rider, the sweeper plans it when a rider becomes available.)

### 7.8 `engine/sweeper.py` — idempotent, ordered (J3)

```python
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
                transition(db, a, "LOST", now, "system", "RIDER_SILENT")
                create_recovery_batch(db, now, orig, a, r)
        stats["riders_silent"] += 1

    # 3. Spoilage: in-flight past deliver_by is never deliverable
    for a in db.scalars(select(Assignment).where(
            Assignment.status.in_(IN_FLIGHT), Assignment.deliver_by < now)).all():
        transition(db, a, "LOST", now, "system", "SPOILED"); stats["spoiled"] += 1

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
    real = clock.real_now()
    last = float(db.execute(text("select value from app_state where key='last_sweep_real'")).scalar() or 0)
    if not force and real.timestamp() - last < SWEEP_THROTTLE_S:
        return
    db.execute(text("update app_state set value=:v where key='last_sweep_real'"), {"v": str(real.timestamp())})
    run_sweep(db, clock.now(db))
    db.commit()
```
Idempotency rule: running `run_sweep` twice with the same `now` must change nothing the second time (tested).

### 7.9 `engine/rerouter.py` — mid-transit recovery (J4)

```python
def reroute_assignment(db, a, now, reason) -> str:
    """Returns REROUTED | STRANDED | CANCELLED."""
    batch = db.execute(select(Batch).where(Batch.id == a.batch_id).with_for_update()).scalar_one()
    rider = db.get(Rider, a.rider_id)
    picked = a.status in IN_FLIGHT
    rows = db.execute(                          # ONE locked query, ordered by id
        select(ShelterDemand, Shelter).join(Shelter, Shelter.id == ShelterDemand.shelter_id)
        .where(ShelterDemand.category == batch.category).order_by(ShelterDemand.id)
        .with_for_update(of=ShelterDemand)).all()
    old_id = a.shelter_id
    old_d = next((d for d, s in rows if s.id == old_id), None)
    best = None
    for d, s in rows:
        if s.id == old_id or s.status != "OPEN": continue
        if d.open_capacity < a.quantity: continue
        if rules.missing(batch.category, batch.cold_chain, s): continue
        e = eta_for(now, rider, batch, s, picked_up=picked)
        if e.feasible and (best is None or e.arrival < best[2].arrival):
            best = (d, s, e)
    if best:
        d, s, e = best
        if old_d: old_d.in_transit = max(0, old_d.in_transit - a.quantity)
        d.in_transit += a.quantity
        a.shelter_id, a.reroute_count = s.id, a.reroute_count + 1
        if a.status == "CLAIMED":
            a.pickup_deadline = pickup_deadline(batch, s, rider)
            a.claim_expires_at = min(a.claim_expires_at, a.pickup_deadline)
        was = a.status
        if was == "STRANDED":
            transition(db, a, "IN_TRANSIT", now, "system", "REROUTED")
        emit(db, now, "system", "assignment", a.id, "REROUTED",
             {"from_shelter": old_id, "to_shelter": s.id, "quantity": a.quantity,
              "reason": reason, "slack_min": round(e.slack_min, 1)})
        return "REROUTED"
    if a.status == "CLAIMED":
        transition(db, a, "CANCELLED", now, "system", "NO_VALID_DESTINATION")
        return "CANCELLED"                      # quantity returns to pool, sweeper re-plans
    if a.status != "STRANDED":
        transition(db, a, "STRANDED", now, "system", reason)   # alert on ops console
    return "STRANDED"

def handle_shelter_change(db, shelter_id, now) -> int:
    """Call synchronously after: close, flags change, demand change, walk-in donation."""
    moved = 0
    shelter = db.get(Shelter, shelter_id)
    for a in db.scalars(select(Assignment).where(
            Assignment.shelter_id == shelter_id, Assignment.status.in_(ACTIVE)).order_by(Assignment.id)).all():
        batch = db.get(Batch, a.batch_id)
        if shelter.status != "OPEN" or rules.missing(batch.category, batch.cold_chain, shelter):
            reroute_assignment(db, a, now, "SHELTER_UNAVAILABLE"); moved += 1
    order = {"CLAIMED": 0, "PICKED_UP": 1, "IN_TRANSIT": 2}      # move least-progressed first
    for d in db.scalars(select(ShelterDemand).where(ShelterDemand.shelter_id == shelter_id)).all():
        while d.received + d.in_transit > d.required:
            cands = db.scalars(select(Assignment).join(Batch, Batch.id == Assignment.batch_id).where(
                Assignment.shelter_id == shelter_id, Batch.category == d.category,
                Assignment.status.in_(tuple(order)))).all()
            if not cands: break
            a = sorted(cands, key=lambda x: (order[x.status], -x.id))[0]
            reroute_assignment(db, a, now, "DEMAND_FILLED"); moved += 1
            db.refresh(d)
    return moved
```
Both outcomes of a failed reroute (CANCELLED or STRANDED) reduce `in_transit`, so the overflow loop always terminates.

---

## 8. API Spec

Prefix `/api`. JSON. `Authorization: Bearer <jwt>`. Errors: `{"code": "...", "message": "..."}`.
Codes: `INVALID_TRANSITION, CLAIM_EXPIRED, SPOILED, INFEASIBLE, INCOMPATIBLE, NO_VALID_DESTINATION, NOT_FOUND, FORBIDDEN, UNAUTHORIZED, VALIDATION_ERROR, RATE_LIMITED`.
Ownership failures return **404** (not 403) to avoid leaking existence. Admin may act on any entity.

**Dependencies (`deps.py`):** `current_user`, `require_role(*roles)`, `read_db` (db + `maybe_sweep` first), `write_db` (db + `engine_lock` first). All routers except `/internal` and `/auth` use these.

| Method | Route | Role | Body → Behavior |
|---|---|---|---|
| POST | `/auth/register` | public | `{name,email,password,role in donor/rider/shelter}` (+ profile fields lat,lng,capacity). Admin created only by seed |
| POST | `/auth/login` | public | `{email,password}` → `{token, role, entity_id}` |
| GET | `/me` | any | `{user_id, role, entity_id}` (entity_id = donor/rider/shelter id) |
| POST | `/batches` | donor, admin | `{donor_id?, category, item_name, cold_chain=false, quantity_total 1..5000, ready_in_min=0 (0..720), expires_in_min 1..1440, safety_buffer_min=10 (0..60), auto_dispatch=true}` → 201 batch detail; **422 `INFEASIBLE`** if flagged (body includes `batch_id`, `reasons`, `best_case_eta_min`). Supports `Idempotency-Key` |
| GET | `/batches` | donor (own), admin (all) | list with assignment breakdown |
| GET | `/batches/{id}` | owner/admin | detail: batch fields, `assignments[]`, `unfulfilled`, `reasons`, per-assignment slack |
| POST | `/batches/{id}/plan` | owner/admin | dry run → `PlanResult` (no writes) |
| POST | `/batches/{id}/dispatch` | owner/admin | commit `plan_batch(commit=True)`; supports `Idempotency-Key`; re-runs for INFEASIBLE batches too (re-evaluates) |
| GET | `/riders/{id}/assignment` | rider owner/admin | active assignment: item, qty, status, `claim_expires_at`, `deliver_by`, pickup `{lat,lng}`, destination `{id,name,lat,lng}`, `reroute_count` |
| POST | `/riders/{id}/heartbeat` | rider owner/admin | `{lat,lng}`; if `demo_silenced`: return 200, **do not update**. Else update pos + `last_heartbeat_at`; if UNRESPONSIVE and no active assignment → AVAILABLE |
| POST | `/riders/{id}/status` | rider owner/admin | `{online: bool}`; online → AVAILABLE + heartbeat now + event `RIDER_ONLINE` + force sweep. Offline rejected with 409 if active assignment |
| POST | `/assignments/{id}/pickup` | rider owner/admin | needs `CLAIMED`. If `now > claim_expires_at` → expire it, commit, 409 `CLAIM_EXPIRED`. Re-validate destination (open + compatible + capacity); else `reroute_assignment`; if result `CANCELLED` → 409 `NO_VALID_DESTINATION`. Then `PICKED_UP` |
| POST | `/assignments/{id}/depart` | rider owner/admin | `PICKED_UP → IN_TRANSIT` |
| POST | `/assignments/{id}/deliver` | rider owner/admin | needs `IN_TRANSIT`. If `now > deliver_by` → `LOST/SPOILED`, commit, 409 `SPOILED`. If destination invalid → reroute, 409 `NO_VALID_DESTINATION` or new destination in body. Else `DELIVERED` |
| POST | `/assignments/{id}/cancel` | rider owner/admin | only `CLAIMED` → `CANCELLED` (reason `RIDER_CANCEL`), re-plan batch with `extra_exclude={rider}` |
| PUT | `/shelters/{id}/demand` | shelter owner/admin | `{category, required>=0}` upsert, then `handle_shelter_change` |
| POST | `/shelters/{id}/walk-in` | shelter owner/admin | `{category, quantity 1..5000}` → `received += qty` (**the PS "citizen drops food directly" case**), then `handle_shelter_change` |
| POST | `/shelters/{id}/close` , `/open` | shelter owner/admin | set status; close → `handle_shelter_change` (sync reroute) |
| PUT | `/shelters/{id}/flags` | shelter owner/admin | partial `{has_clean_water?, has_cooking_fuel?, has_refrigeration?}` then `handle_shelter_change` |
| GET | `/shelters/{id}/incoming` | shelter owner/admin | active assignments to this shelter with item, qty, rider status, ETA |
| GET | `/events?since=<id>&limit=100` | any auth | events with `id > since` ascending |
| GET | `/config` | public | `{supabase_url, supabase_anon_key, demo_mode}` |
| GET | `/admin/overview` | admin | KPIs + lists for map: donors, riders, shelters (+demands), batches, active assignments, last 50 events. KPIs: delivered, in_flight, wasted, lost, unassigned, claims_expired, reroutes, stranded |
| GET, POST | `/internal/sweep` | `Authorization: Bearer $CRON_SECRET` | `run_sweep` + commit (Vercel Cron sends GET with that header) |
| POST | `/demo/seed` | admin + `DEMO_MODE` | reload CSV entities, reset clock offset, truncate runtime tables |
| POST | `/demo/batches/{scenario}` | admin + `DEMO_MODE` | create the CSV batches of that scenario through `create_batch` (`main`, `tight`, `compat`) |
| POST | `/demo/time-travel` | admin + `DEMO_MODE` | `{minutes 1..120}`: offset += minutes; **shift `last_heartbeat_at` of all non-silenced riders by the same delta**; force sweep; event `CLOCK_JUMP` |
| POST | `/demo/riders/{id}/silence` | admin + `DEMO_MODE` | `{on: bool}` → `demo_silenced` |
| POST | `/demo/riders/accept-all` | admin + `DEMO_MODE` | for every non-silenced rider with a `CLAIMED` assignment: pickup then depart |
| POST | `/demo/reset-clock` | admin + `DEMO_MODE` | offset = 0 |

Demo routers are **only registered when `DEMO_MODE=true`**.

**Batch detail unfulfilled block:** `{"quantity_unassigned": 20, "reasons": ["NO_RIDER"], "wasted": 0, "lost": 0}`.

**Idempotency:** if `Idempotency-Key` present, look up `(key,user_id)` → return stored response; else run, then store the response JSON in the same transaction.

---

## 9. Security (all mandatory)

| Area | Implementation |
|---|---|
| Passwords | `bcrypt.hashpw/checkpw`; hash `DEMO_PASSWORD` once in seed and reuse |
| Auth | PyJWT HS256, claims `sub`(user id), `role`, `exp` (60 min), secret from env; reject expired/invalid → 401 |
| RBAC | `require_role(...)` on every route |
| Ownership | Resolve entity via `user_id == current_user.id`; mismatch → 404; admin bypass |
| Validation | Pydantic bounds exactly as in section 8; `Literal`/enum for category; `extra="forbid"`; max string length 120; lat/lng ranges |
| SQLi | SQLAlchemy ORM / bound parameters only; no f-string SQL |
| Rate limit | `slowapi`, key = first IP in `X-Forwarded-For` else remote addr; `/auth/*` 10/min, other writes 120/min |
| Concurrency | Engine advisory mutex + row locks + DB CHECKs + `one_active_per_rider` unique index |
| Idempotency | `Idempotency-Key` on batch create and dispatch |
| CORS | allowlist from `ALLOWED_ORIGINS`; never `*` when `DEMO_MODE=false` |
| Errors | Global handler returns `{code,message}`; never stack traces; log server-side |
| Internal | `/internal/sweep` checks `Authorization: Bearer $CRON_SECRET` (constant-time compare) |
| Demo | Routes absent unless `DEMO_MODE=true`; also require admin |
| Realtime | RLS enabled on all tables; only `events` selectable by `anon`; payloads contain no PII; the browser still fetches real data via authenticated API |
| Audit | Every transition writes an `events` row with `actor` |
| Headers | Add `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` middleware |

---

## 10. Frontend (`public/index.html`, one file, vanilla JS, Leaflet via CDN)

**Layout:** top bar (login: email/password → token kept in memory + `sessionStorage`), tabs by role. Admin sees all tabs plus the Ops Console.

**Real-time:** on load `GET /api/config`. If `supabase_url` present, load `supabase-js@2` from jsDelivr and subscribe:
```js
sb.channel('live').on('postgres_changes',
  { event: 'INSERT', schema: 'public', table: 'events' },
  p => onEvent(p.new)).subscribe();
```
Otherwise poll `/api/events?since=<lastId>` every 2 s. `onEvent` debounces (300 ms) and re-fetches the visible panel's data. Also poll the visible panel every 5 s as a safety net.

| Panel | Contents |
|---|---|
| **Donor** | Create-batch form (category, item, qty, ready in, expires in, cold-chain). Batch list: status badge, countdown to expiry, split table (rider, shelter, qty, status, slack), unfulfilled + reason chips. `INFEASIBLE` shown as a red banner with reasons. Buttons: Plan (dry run), Dispatch |
| **Rider** | Current assignment card: item, qty, destination, claim countdown, deliver-by countdown. Buttons Pickup / Depart / Deliver / Cancel. Auto-heartbeat every 15 s (browser geolocation optional; else last known). Online/Offline toggle. Banner when destination changes ("Re-routed to Shelter B") |
| **Shelter** | Demand table (required/received/in-transit/open) with edit; flag toggles (water/fuel/fridge); Open/Close; **Walk-in donation** form; incoming deliveries list |
| **Ops Console (admin)** | Leaflet map (donors, riders, shelters, lines rider→destination); KPI strip; live event feed (newest first, colored by type); tables for riders (status, heartbeat age) and shelters. **Demo bar:** Seed, Run scenario (main/tight/compat), Time +1/+5/+15 min, Silence/Unsilence rider, Accept-all riders, Close shelter, Walk-in at shelter, Rider online toggle |

Color rule for slack/countdown: > 20 min green, 5–20 amber, < 5 red. Escape all dynamic text (no `innerHTML` with unescaped data).

---

## 11. Synthetic Data (`data/`) and Seeder

Create these files exactly.

`data/donors.csv`
```
name,lat,lng
Banquet Hall,18.5204,73.8567
Hostel Mess,18.5100,73.8600
Medico Pharmacy,18.5250,73.8500
Far Pharmacy,18.6400,73.7600
```
`data/riders.csv`
```
name,capacity_units,speed_kmph,lat,lng,status
R1,40,25,18.5310,73.8440,AVAILABLE
R2,40,25,18.5150,73.8650,AVAILABLE
R3,40,25,18.5400,73.8700,AVAILABLE
R4,40,25,18.5000,73.8500,AVAILABLE
R5,40,25,18.5600,73.8300,OFFLINE
```
`data/shelters.csv`
```
name,lat,lng,status,has_clean_water,has_cooking_fuel,has_refrigeration
Shelter A,18.5290,73.8740,OPEN,true,true,false
Shelter B,18.5080,73.8450,OPEN,true,true,true
Shelter C,18.5400,73.8350,OPEN,true,false,false
Shelter D,18.4900,73.8600,OPEN,false,true,false
```
`data/shelter_demands.csv`
```
shelter_name,category,required
Shelter A,COOKED_FOOD,50
Shelter A,RAW_RATION,40
Shelter A,INFANT_FORMULA,20
Shelter B,COOKED_FOOD,60
Shelter B,RAW_RATION,40
Shelter C,COOKED_FOOD,70
Shelter C,INFANT_FORMULA,20
Shelter C,RAW_RATION,30
Shelter D,COOKED_FOOD,40
Shelter D,RAW_RATION,30
```
`data/batches.csv`
```
scenario,donor_name,category,item_name,cold_chain,quantity_total,ready_in_min,expires_in_min
main,Banquet Hall,COOKED_FOOD,Veg Biryani,false,180,0,75
tight,Far Pharmacy,MEDICAL,Insulin Kits,true,25,0,12
compat,Hostel Mess,RAW_RATION,Ration Kits,false,60,0,120
compat,Medico Pharmacy,INFANT_FORMULA,Infant Formula,false,30,0,90
```
(Shelter C lacks fuel and D lacks water, so `compat` must never plan rations to C or D, and formula must never go to D.)

**`scripts/seed.py` → `seed(db, now)`:**
1. Truncate `assignments, batches, shelter_demands, shelters, riders, donors, users, events, idempotency_keys` (`restart identity cascade`); set `clock_offset_s=0`.
2. Create `admin@demo.local` (role admin), and one user per donor/rider/shelter: email = slug(name)+`@demo.local` (lowercase, spaces→`.`), password = `DEMO_PASSWORD`.
3. Load CSVs with `csv.DictReader`; validate through the same Pydantic types; resolve names → ids.
4. Riders get `last_heartbeat_at = now`.
5. Batches are **not** created at seed; `POST /demo/batches/{scenario}` creates them through `create_batch` so the real feasibility path runs, using `expires_in_min`/`ready_in_min` offsets from `clock.now()`.
6. CLI: `python -m scripts.seed`.

---

## 12. Judge Demo Script (must work live from the Ops Console)

| Step | Action | Expected |
|---|---|---|
| 1 | Seed → run `main` (180 meals, 75 min) | 4 assignments × 40 = 160 across shelters; **20 unfulfilled `NO_RIDER`** (R5 offline). J2 |
| 2 | Silence R2 → Accept-all riders → Time +6 min | R2's claim expires (`CLAIM_EXPIRED`), R2 `UNRESPONSIVE`, batch unassigned = 60; R2 excluded from this batch. J3 |
| 3 | Set R5 online | Sweeper assigns R5 40 units; 20 remain `NO_RIDER` until a rider frees up |
| 4 | Walk-in 50 at the shelter a rider is heading to (or Close it) | That rider is **rerouted** to the best feasible shelter (event `REROUTED`, `reroute_count=1`). If none feasible → `STRANDED` alert. J4 |
| 5 | Seed → run `tight` | 422 `INFEASIBLE` (`TIME_INFEASIBLE`), red banner, best-case ETA shown. J1 |
| 6 | Seed → run `compat` | Rations only to A/B; formula only to A/C; never C or D for rations. J5 |
| 7 | Silence a rider **after** pickup (in transit) → Time +2 min | Assignment `LOST`, recovery batch created at the rider's last position and re-dispatched (or flagged infeasible) |

---

## 13. Tests (`pytest`, real Postgres via `TEST_DATABASE_URL`)

`conftest.py`: create schema once from `schema.sql`; truncate all tables before each test; factories `make_donor, make_rider, make_shelter, make_demand, make_batch`; tests pass an explicit `now` into engine functions (no sleeping).

| Test | Assert |
|---|---|
| `test_tight_expiry_infeasible` | tight batch → status `INFEASIBLE`, reasons include `TIME_INFEASIBLE`, no assignments, HTTP 422 |
| `test_split_sums_to_total` | 180 / riders 40 → Σ qty + unassigned == 180; every slice ≤ rider capacity and ≤ shelter open capacity |
| `test_urgent_batch_planned_first` | two batches, one rider → the earlier-expiring batch gets the rider |
| `test_claim_expiry_releases_and_replans` | after TTL: assignment `CLAIM_EXPIRED`, batch unassigned restored, shelter `in_transit` restored, new rider assigned, timed-out rider excluded |
| `test_claim_ttl_shrinks_for_tight_batches` | TTL clamps to [120 s, 300 s] and never exceeds `pickup_deadline` |
| `test_silent_rider_claimed_reassigned` | stale heartbeat on `CLAIMED` rider → expire + replan |
| `test_silent_rider_in_transit_creates_recovery` | `LOST`, `quantity_lost` set, recovery batch created at rider position |
| `test_shelter_close_reroutes_in_transit` | `shelter_id` changes, `reroute_count == 1`, old `in_transit` decreased, new increased |
| `test_walk_in_overflow_reroutes` | `received + in_transit > required` resolved by rerouting; invariant holds afterwards |
| `test_no_fallback_marks_stranded` | status `STRANDED`, `shelter_id is None`, old `in_transit` released, `STRANDED` event |
| `test_stranded_retried_when_shelter_opens` | opening/adding demand + sweep → back to `IN_TRANSIT` |
| `test_incompatible_never_planned` | rations never planned to no-fuel/no-water shelter; also blocked in reroute |
| `test_flag_change_triggers_reroute` | shelter loses `has_clean_water` while formula in transit → rerouted |
| `test_deliver_after_deadline_rejected` | 409 `SPOILED`, status `LOST`, `quantity_wasted` increased, never `DELIVERED` |
| `test_pickup_after_ttl_rejected` | 409 `CLAIM_EXPIRED` |
| `test_one_active_assignment_per_rider` | DB rejects a second active assignment for the same rider |
| `test_no_oversubscription_concurrent` | two threads dispatching different batches to one shelter never exceed `open_capacity` |
| `test_quantity_conservation` | `total == unassigned + wasted + lost + Σactive + Σdelivered` after every scenario |
| `test_sweep_idempotent` | second `run_sweep` with same `now` changes nothing |
| `test_invalid_transition_409` | e.g. `DELIVERED → PICKED_UP` |
| `test_rbac_and_ownership` | rider A → rider B's assignment = 404; donor cannot call shelter routes = 403; no token = 401 |
| `test_validation_bounds` | negative qty, bad category, lat 200 → 422 |
| `test_idempotency_key_replay` | same key twice → one batch |
| `test_demo_routes_absent_when_disabled` | `DEMO_MODE=false` → 404 |
| `test_time_travel_keeps_live_riders_fresh` | non-silenced riders stay plannable after +15 min |

---

## 14. Deployment (Vercel + Supabase)

1. Supabase: create project, run `schema.sql` then `supabase_extras.sql` in the SQL editor.
2. `DATABASE_URL` = Supabase **pooler** string (port 6543), format `postgresql+psycopg://postgres.<ref>:<password>@<host>:6543/postgres`.
3. `db.py` must use `NullPool` and `connect_args={"prepare_threshold": None}` (transaction pooler has no prepared statements):
```python
engine = create_engine(settings.database_url, poolclass=NullPool,
                       connect_args={"prepare_threshold": None})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
def get_db():
    db = SessionLocal()
    try: yield db
    except Exception: db.rollback(); raise
    finally: db.close()
```
4. `api/index.py`: `from app.main import app`.
5. `vercel.json`
```json
{
  "rewrites": [{ "source": "/api/(.*)", "destination": "/api/index.py" }],
  "crons": [{ "path": "/api/internal/sweep", "schedule": "* * * * *" }]
}
```
Vercel Hobby may restrict cron frequency; the **lazy sweep on every request** is the real guarantee and is already implemented.
6. Vercel env vars: `DATABASE_URL, JWT_SECRET, CRON_SECRET, ALLOWED_ORIGINS, DEMO_MODE=true (judging only), DEMO_PASSWORD, SUPABASE_URL, SUPABASE_ANON_KEY`.
7. After deploy: log in as `admin@demo.local`, press **Seed**, run the section 12 script.

---

## 15. `main.py` Checklist

- `app = FastAPI(title="Last-Mile Spoilage Trap")`
- Middleware: security headers, CORS (allowlist), slowapi
- Exception handlers: `ApiError` → `{code,message}` with its status; `RequestValidationError` → 422 `VALIDATION_ERROR`; generic `Exception` → 500 `{code:"INTERNAL"}` (log, no trace to client)
- Include routers with `prefix="/api"`; include `demo` router only if `settings.demo_mode`
- Last: `app.mount("/", StaticFiles(directory="public", html=True), name="ui")` (local use)

---

## 16. Build Order and Commits (120 min)

| Min | Milestone | Commit message |
|---|---|---|
| 0–8 | Scaffold: repo files, `.gitignore`, `requirements.txt`, docker-compose, `config.py`, `db.py`, `main.py` skeleton, `api/index.py`, `vercel.json` | `chore: scaffold fastapi project, config and deploy files` |
| 8–20 | `schema.sql`, `models.py`, `constants.py`, `clock.py`, `geo.py`, `rules.py`, `errors.py`, `init_db.py` | `feat: schema, models, clock, geo and compatibility rules` |
| 20–30 | `security.py`, `deps.py`, auth routes, rate limit, headers, `/me` | `feat: jwt auth, rbac and ownership guards` |
| 30–45 | `feasibility.py`, `planner.py`, `engine/batches.py`, batch routes + idempotency | `feat: time-aware planner with batch splitting and feasibility gate` |
| 45–55 | `lifecycle.py`, `events.py`, assignment routes (pickup/depart/deliver/cancel) | `feat: guarded state machine, audit events and rider flow` |
| 55–68 | `sweeper.py` (claims, heartbeat, spoilage, cutoff, auto-plan), lazy sweep, internal route | `feat: claim ttl, heartbeat detection and expiry sweeper` |
| 68–78 | `rerouter.py`, shelter routes (demand, walk-in, close/open, flags) | `feat: mid-transit rerouting and shelter change handling` |
| 78–85 | CSV data, `seed.py`, demo routes, `admin/overview`, `/events`, `/config` | `feat: csv seed data, demo controls and admin overview` |
| 85–100 | `public/index.html`: login, 4 panels, Realtime + polling fallback, Leaflet map | `feat: realtime dashboard with role panels and ops console` |
| 100–112 | Tests per section 13; fix failures | `test: judge scenarios, invariants and security tests` |
| 112–120 | README (run, architecture, demo script), Supabase + Vercel deploy, smoke test | `docs: readme, deployment and demo walkthrough` |

---

## 17. Definition of Done

- [ ] `pytest -q` passes all section 13 tests
- [ ] Section 12 demo script works end-to-end from the Ops Console, locally and on Vercel
- [ ] Browser updates arrive via Supabase Realtime (and via polling when env is empty)
- [ ] Time-travel + silence + accept-all work with `DEMO_MODE=true`; routes absent when false
- [ ] No quantity leaks (conservation test) and no double-booking (unique index + mutex)
- [ ] README documents setup, architecture, design decisions and the judge walkthrough
- [ ] 11+ milestone commits with clear messages
