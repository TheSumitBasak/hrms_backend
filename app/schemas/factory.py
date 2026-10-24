"""Pydantic schema factory.

Generates ``Create`` / ``Update`` / ``Read`` models directly from SQLAlchemy
models so that the 60-table data model stays the single source of truth.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model
from sqlalchemy import Column
from sqlalchemy.orm import DeclarativeBase

from app.core.types import JSONType

_SKIP_ON_CREATE = {"created_at", "updated_at"}


def _python_type(column: Column) -> Any:
    if isinstance(column.type, JSONType):
        return Any
    try:
        return column.type.python_type
    except (AttributeError, NotImplementedError):
        return Any


def _default_from_column(column: Column) -> Any:
    default = getattr(column, "default", None)
    if default is not None and getattr(default, "is_scalar", False):
        return default.arg
    if default is not None and getattr(default, "is_callable", False):
        return None
    return None


def build_read_schema(model: type[DeclarativeBase], name: str) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for column in model.__table__.columns:
        fields[column.name] = (_python_type(column) | None, None)
    return create_model(
        name,
        __config__=ConfigDict(from_attributes=True),
        **fields,
    )


def build_create_schema(
    model: type[DeclarativeBase],
    name: str,
    *,
    exclude: set[str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> type[BaseModel]:
    exclude = exclude or set()
    overrides = overrides or {}
    fields: dict[str, Any] = {}
    for column in model.__table__.columns:
        if column.primary_key or column.name in exclude:
            continue
        py_type = _python_type(column)
        if column.name in overrides:
            fields[column.name] = overrides[column.name]
        elif column.nullable:
            fields[column.name] = (py_type | None, None)
        elif column.default is not None or column.server_default is not None:
            default = _default_from_column(column)
            fields[column.name] = (py_type, default)
        else:
            fields[column.name] = (py_type, ...)
    for field_name, spec in overrides.items():
        if field_name not in fields:
            fields[field_name] = spec
    return create_model(name, **fields)


def build_update_schema(
    model: type[DeclarativeBase],
    name: str,
    *,
    exclude: set[str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> type[BaseModel]:
    exclude = exclude or set()
    overrides = overrides or {}
    fields: dict[str, Any] = {}
    for column in model.__table__.columns:
        if column.primary_key or column.name in exclude:
            continue
        fields[column.name] = (_python_type(column) | None, None)
    fields.update(overrides)
    return create_model(name, **fields)


__all__ = [
    "build_create_schema",
    "build_read_schema",
    "build_update_schema",
    "uuid",
    "date",
    "datetime",
    "time",
    "Decimal",
]
