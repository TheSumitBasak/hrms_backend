"""Third-party integrations."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.types import JSONType
from app.models.base import IDMixin


class Integration(IDMixin, Base):
    __tablename__ = "integrations"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="disconnected")
    config: Mapped[dict | None] = mapped_column(JSONType)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
