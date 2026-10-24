"""Generic persistence helpers shared by every resource router."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.core.deps import Pagination
from app.core.errors import NotFoundError
from app.core.query import apply_filters, apply_search, apply_sort


def jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def to_dict(obj: Any) -> dict:
    return {column.name: jsonable(getattr(obj, column.name)) for column in obj.__table__.columns}


def get_or_404(db: Session, model: Any, entity_id: Any, entity_name: str | None = None) -> Any:
    obj = db.get(model, entity_id)
    if obj is None:
        raise NotFoundError(entity_name or model.__name__, entity_id)
    return obj


def build_query(
    model: Any,
    pagination: Pagination,
    *,
    search_fields: Sequence[str] = (),
    default_sort: Sequence[str] = (),
    extra_conditions: Sequence[Any] = (),
) -> Select:
    stmt = select(model)
    for condition in extra_conditions:
        stmt = stmt.where(condition)
    stmt = apply_filters(stmt, model, pagination.filters)
    stmt = apply_search(stmt, model, pagination.search, list(search_fields))
    stmt = apply_sort(stmt, model, pagination.sort, list(default_sort))
    return stmt


def paginate(db: Session, stmt: Select, pagination: Pagination) -> tuple[list[Any], int]:
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = db.execute(count_stmt).scalar_one()
    rows = db.execute(stmt.offset(pagination.offset).limit(pagination.page_size)).scalars().all()
    return list(rows), int(total)


def create(db: Session, model: Any, data: dict) -> Any:
    obj = model(**data)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def bulk_create(db: Session, model: Any, items: list[dict]) -> list[Any]:
    objects = [model(**item) for item in items]
    db.add_all(objects)
    db.commit()
    for obj in objects:
        db.refresh(obj)
    return objects


def update(db: Session, obj: Any, data: dict) -> Any:
    for key, value in data.items():
        setattr(obj, key, value)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def delete(db: Session, obj: Any) -> None:
    db.delete(obj)
    db.commit()
