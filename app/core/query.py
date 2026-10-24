"""Query helpers: filtering, searching and sorting for generic list endpoints."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Select, and_, or_
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import ColumnElement

_OPERATORS = {
    "eq": operators.eq,
    "ne": operators.ne,
    "gt": operators.gt,
    "gte": operators.ge,
    "lt": operators.lt,
    "lte": operators.le,
    "like": operators.ilike_op,
    "in": None,
    "isnull": None,
}


def _column(model: Any, field: str) -> InstrumentedAttribute | None:
    return getattr(model, field, None)


def coerce(model: Any, field: str, value: Any) -> Any:
    """Coerce a raw string value to the Python type of the target column."""
    column = _column(model, field)
    if column is None:
        return value
    try:
        py_type = column.property.columns[0].type.python_type
    except (AttributeError, NotImplementedError):
        return value
    if py_type is uuid.UUID and isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return value
    if py_type is bool and isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if py_type is int and isinstance(value, str):
        return int(value)
    if py_type is Decimal and isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return value
    if py_type is date and isinstance(value, str) and "/" not in value:
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return value
    if py_type is datetime and isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def apply_filters(stmt: Select, model: Any, filters: dict[str, Any]) -> Select:
    conditions: list[ColumnElement] = []
    for raw_key, raw_value in filters.items():
        field, _, op = raw_key.partition("__")
        op = op or "eq"
        column = _column(model, field)
        if column is None:
            continue
        if op == "in":
            values = raw_value if isinstance(raw_value, (list, tuple)) else str(raw_value).split(",")
            conditions.append(column.in_([coerce(model, field, v) for v in values]))
        elif op == "isnull":
            is_null = str(raw_value).strip().lower() in {"1", "true", "yes"}
            conditions.append(column.is_(None) if is_null else column.is_not(None))
        elif op in _OPERATORS and _OPERATORS[op] is not None:
            conditions.append(_OPERATORS[op](column, coerce(model, field, raw_value)))
        else:
            conditions.append(column == coerce(model, field, raw_value))
    if conditions:
        stmt = stmt.where(and_(*conditions))
    return stmt


def apply_search(stmt: Select, model: Any, term: str | None, search_fields: list[str]) -> Select:
    if not term or not search_fields:
        return stmt
    clauses = []
    for field in search_fields:
        column = _column(model, field)
        if column is not None:
            clauses.append(column.ilike(f"%{term}%"))
    if clauses:
        stmt = stmt.where(or_(*clauses))
    return stmt


def apply_sort(
    stmt: Select, model: Any, sort: str | None, default: list[str] | None = None
) -> Select:
    specs = sort.split(",") if sort else (default or [])
    for spec in specs:
        spec = spec.strip()
        if not spec:
            continue
        if ":" in spec:
            field, _, direction = spec.partition(":")
        elif spec.startswith("-"):
            field, direction = spec[1:], "desc"
        else:
            field, direction = spec, "asc"
        column = _column(model, field)
        if column is None:
            continue
        stmt = stmt.order_by(column.desc() if direction.lower() == "desc" else column.asc())
    return stmt
