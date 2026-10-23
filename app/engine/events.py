from app.models import Event

def emit(db, now, actor, entity_type, entity_id, event_type, payload=None):
    db.add(Event(ts=now, actor=actor, entity_type=entity_type, entity_id=entity_id,
                 event_type=event_type, payload=payload or {}))
