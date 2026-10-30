"""Platform reference data: Indian states and GST state codes (§23.2).

Seeded rather than hardcoded in validation conditionals (ADR-006), so §7.1's "if country = IN:
must match ``states`` reference" and the ``state_code`` derivation in §7.1/§8.1/§11.4 stay
configuration. Not organization-scoped: the list of Indian states is the same for every tenant.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import status_constraint

STATE_CODE_LENGTH = 2
"""GST state codes are two digits, stored as characters to preserve a leading zero."""


class State(Base):
    """An Indian state or union territory with its GST state code.

    ``code`` is the primary key rather than a UUID: it is a stable, externally defined
    identifier (the GST state code), it is what other tables store, and §23.2 specifies
    ``code CHAR(2) PK``.
    """

    __tablename__ = "states"
    __table_args__ = (
        CheckConstraint("code REGEXP '^[0-9]{2}$'", name="ck_states_code_numeric"),
        status_constraint("states"),
    )

    code: Mapped[str] = mapped_column(String(STATE_CODE_LENGTH), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")

    def __repr__(self) -> str:
        return f"<State {self.code}>"
