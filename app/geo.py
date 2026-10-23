from math import radians, sin, cos, asin, sqrt
from app.constants import ROAD_FACTOR
def haversine_km(lat1, lng1, lat2, lng2):
    p1, p2 = radians(lat1), radians(lat2)
    dphi, dl = p2 - p1, radians(lng2 - lng1)
    a = sin(dphi/2)**2 + cos(p1)*cos(p2)*sin(dl/2)**2
    return 2 * 6371.0 * asin(sqrt(min(1.0, a)))
def travel_minutes(km, speed_kmph):
    return km * ROAD_FACTOR / speed_kmph * 60
