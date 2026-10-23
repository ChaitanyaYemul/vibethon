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
