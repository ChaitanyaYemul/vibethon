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
