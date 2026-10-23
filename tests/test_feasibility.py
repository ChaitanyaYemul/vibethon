from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from app.engine.feasibility import eta_for
from app.engine.planner import claim_ttl


def test_wait_load_and_deadline():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rider = SimpleNamespace(lat=0, lng=0, speed_kmph=25)
    shelter = SimpleNamespace(lat=0, lng=0)
    batch = SimpleNamespace(lat=0, lng=0, ready_at=now + timedelta(minutes=5), expires_at=now + timedelta(minutes=10), safety_buffer_min=3)
    eta = eta_for(now, rider, batch, shelter)
    assert eta.arrival == now + timedelta(minutes=8)
    assert not eta.feasible
    assert claim_ttl(now, now + timedelta(minutes=1)).total_seconds() == 120
    assert claim_ttl(now, now + timedelta(hours=1)).total_seconds() == 300
