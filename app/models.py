from datetime import datetime
from uuid import UUID
from sqlalchemy import BigInteger, Boolean, DateTime, Double, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base

TZ = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=text("now()"))


class Profile:
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    lat: Mapped[float] = mapped_column(Double)
    lng: Mapped[float] = mapped_column(Double)


class Donor(Profile, Base):
    __tablename__ = "donors"


class Rider(Profile, Base):
    __tablename__ = "riders"
    capacity_units: Mapped[int] = mapped_column(Integer)
    speed_kmph: Mapped[float] = mapped_column(Double, default=25, server_default="25")
    status: Mapped[str] = mapped_column(Text, default="AVAILABLE", server_default="AVAILABLE")
    last_heartbeat_at: Mapped[datetime] = mapped_column(TZ, server_default=text("now()"))
    demo_silenced: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class Shelter(Profile, Base):
    __tablename__ = "shelters"
    status: Mapped[str] = mapped_column(Text, default="OPEN", server_default="OPEN")
    has_clean_water: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    has_cooking_fuel: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    has_refrigeration: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class ShelterDemand(Base):
    __tablename__ = "shelter_demands"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shelter_id: Mapped[int] = mapped_column(ForeignKey("shelters.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(Text)
    required: Mapped[int] = mapped_column(Integer)
    received: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    in_transit: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    @property
    def open_capacity(self):
        return max(0, self.required - self.received - self.in_transit)


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    donor_id: Mapped[int] = mapped_column(ForeignKey("donors.id", ondelete="CASCADE"))
    parent_batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    is_recovery: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    category: Mapped[str] = mapped_column(Text)
    item_name: Mapped[str] = mapped_column(Text)
    cold_chain: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    quantity_total: Mapped[int] = mapped_column(Integer)
    quantity_unassigned: Mapped[int] = mapped_column(Integer)
    quantity_wasted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    quantity_lost: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ready_at: Mapped[datetime] = mapped_column(TZ)
    expires_at: Mapped[datetime] = mapped_column(TZ)
    safety_buffer_min: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    lat: Mapped[float] = mapped_column(Double)
    lng: Mapped[float] = mapped_column(Double)
    status: Mapped[str] = mapped_column(Text, default="AVAILABLE", server_default="AVAILABLE")
    unfulfilled_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=text("now()"))


class Assignment(Base):
    __tablename__ = "assignments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"))
    rider_id: Mapped[int] = mapped_column(ForeignKey("riders.id"))
    shelter_id: Mapped[int | None] = mapped_column(ForeignKey("shelters.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    claim_expires_at: Mapped[datetime] = mapped_column(TZ)
    pickup_deadline: Mapped[datetime] = mapped_column(TZ)
    deliver_by: Mapped[datetime] = mapped_column(TZ)
    picked_up_at: Mapped[datetime | None] = mapped_column(TZ)
    delivered_at: Mapped[datetime | None] = mapped_column(TZ)
    reroute_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=text("now()"))


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(TZ)
    actor: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    response: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=text("now()"))


class AppState(Base):
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
