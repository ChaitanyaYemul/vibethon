from types import SimpleNamespace
from app.rules import missing
from app.geo import haversine_km, travel_minutes


def test_food_safety_requirements():
    shelter = SimpleNamespace(has_clean_water=False, has_cooking_fuel=False, has_refrigeration=False)
    assert missing("RAW_RATION", False, shelter) == ["has_cooking_fuel", "has_clean_water"]
    assert missing("INFANT_FORMULA", True, shelter) == ["has_clean_water", "has_refrigeration"]
    assert missing("COOKED_FOOD", False, shelter) == []


def test_travel_units():
    assert haversine_km(18.52, 73.85, 18.52, 73.85) == 0
    assert 110 < haversine_km(0, 0, 1, 0) < 112
    assert travel_minutes(25, 25) == 78
