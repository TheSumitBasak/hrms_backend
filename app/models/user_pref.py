"""Per-user preferences, scheduled reports and arbitrary tenant settings."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.types import JSONType
from app.models.base import IDMixin, TimestampMixin, utcnow


class UserPreference(IDMixin, Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=False, unique=True
    )
    active_org_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("organizations.id"))
    dashboard_layout: Mapped[list | None] = mapped_column(JSONType)
    ui_prefs: Mapped[dict | None] = mapped_column(JSONType)
    locale: Mapped[str | None] = mapped_column(String(10))
    theme: Mapped[str | None] = mapped_column(String(20))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ReportSchedule(IDMixin, TimestampMixin, Base):
    __tablename__ = "report_schedules"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    report_code: Mapped[str] = mapped_column(String(100), nullable=False)
    frequency: Mapped[str] = mapped_column(String(20), nullable=False, default="monthly")
    format: Mapped[str] = mapped_column(String(10), nullable=False, default="csv")
    recipients: Mapped[list | None] = mapped_column(JSONType)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))


class TenantSetting(IDMixin, TimestampMixin, Base):
    """Generic per-tenant key/value store (currency settings, features, ...)."""

    __tablename__ = "tenant_settings"
    __table_args__ = (UniqueConstraint("org_id", "key", name="uq_tenant_setting_key"),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[dict | None] = mapped_column(JSONType)
