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

