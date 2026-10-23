from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal["COOKED_FOOD", "RAW_RATION", "INFANT_FORMULA", "MEDICAL", "KIT"]
Name = Annotated[str, Field(min_length=1, max_length=120)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Position(Body):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class Register(Position):
    name: Name
    email: Name
    password: str = Field(min_length=8, max_length=72)
    role: Literal["donor", "rider", "shelter"]
    capacity_units: int = Field(default=40, gt=0, le=5000)
    speed_kmph: float = Field(default=25, gt=0, le=200)
    has_clean_water: bool = True
    has_cooking_fuel: bool = True
    has_refrigeration: bool = False

    @model_validator(mode="after")
    def valid_credentials(self):
        self.email = self.email.lower()
        if "@" not in self.email or len(self.password.encode()) > 72:
            raise ValueError("Invalid email or password exceeds 72 bytes")
        return self


class Login(Body):
    email: Name
    password: str = Field(min_length=1, max_length=72)


class BatchCreate(Body):
    donor_id: int | None = Field(default=None, gt=0)
    category: Category
    item_name: Name
    cold_chain: bool = False
    quantity_total: int = Field(ge=1, le=5000)
    ready_in_min: int = Field(default=0, ge=0, le=720)
    expires_in_min: int = Field(ge=1, le=1440)
    safety_buffer_min: int = Field(default=10, ge=0, le=60)
    auto_dispatch: bool = True

    @model_validator(mode="after")
    def valid_window(self):
        if self.expires_in_min <= self.ready_in_min:
            raise ValueError("Expiry must be after readiness")
        return self


class Online(Body):
    online: bool


class Demand(Body):
    category: Category
    required: int = Field(ge=0)


class WalkIn(Body):
    category: Category
    quantity: int = Field(ge=1, le=5000)


class Flags(Body):
    has_clean_water: bool | None = None
    has_cooking_fuel: bool | None = None
    has_refrigeration: bool | None = None

    @model_validator(mode="after")
    def no_null_flags(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Flags must be booleans")
        return self


class TimeTravel(Body):
    minutes: int = Field(ge=1, le=120)


class Silence(Body):
    on: bool
