import re
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import AuditLog, Tournament

def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "torneio"

def unique_slug(db: Session, name: str) -> str:
    base = slugify(name)
    slug = base
    index = 2
    while db.scalar(select(Tournament).where(Tournament.slug == slug)):
        slug = f"{base}-{index}"
        index += 1
    return slug

def audit(db: Session, action: str, entity: str, entity_id: int | None = None, details: str = ""):
    db.add(AuditLog(action=action, entity=entity, entity_id=entity_id, details=details))
    db.commit()
