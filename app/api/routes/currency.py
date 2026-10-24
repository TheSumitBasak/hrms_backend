"""Multi-currency conversion and per-user display-currency preferences."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_permission, write_audit
from app.core.errors import BusinessRuleError
from app.crud.base import get_or_404, jsonable
from app.models.auth import User
from app.models.currency import CurrencyRate, DisplayCurrencyPreference
from app.models.employee import Employee
from app.models.organization import LegalEntity, Organization

router = APIRouter(tags=["Currency"])


def _round(value: Decimal, dp: int = 2) -> Decimal:
    quant = Decimal(1).scaleb(-dp)
    return value.quantize(quant, rounding=ROUND_HALF_UP)


def resolve_rate(
    db: Session, base: str, quote: str, on_date: date | None = None
) -> tuple[Decimal, date, bool] | None:
    """Return (rate, effective_date, is_stale) or None when unavailable."""
    if base.upper() == quote.upper():
        return Decimal(1), on_date or date.today(), False
    stmt = select(CurrencyRate).where(
        CurrencyRate.base_currency == base.upper(),
        CurrencyRate.quote_currency == quote.upper(),
    )
    if on_date:
        stmt = stmt.where(CurrencyRate.effective_date <= on_date)
    rate = db.execute(stmt.order_by(CurrencyRate.effective_date.desc())).scalars().first()
    if rate is not None:
        return Decimal(rate.rate), rate.effective_date, rate.is_stale
    inverse_stmt = select(CurrencyRate).where(
        CurrencyRate.base_currency == quote.upper(),
        CurrencyRate.quote_currency == base.upper(),
    )
    if on_date:
        inverse_stmt = inverse_stmt.where(CurrencyRate.effective_date <= on_date)
    inverse = db.execute(
        inverse_stmt.order_by(CurrencyRate.effective_date.desc())
    ).scalars().first()
    if inverse is not None and Decimal(inverse.rate) != 0:
        return (Decimal(1) / Decimal(inverse.rate)), inverse.effective_date, True
    return None


@router.get("/currency/convert", summary="Convert an amount between currencies")
def convert(
    amount: Decimal = Query(...),
    base: str = Query(..., min_length=3, max_length=3),
    quote: str = Query(..., min_length=3, max_length=3),
    on_date: date | None = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    resolved = resolve_rate(db, base, quote, on_date)
    if resolved is None:
        raise BusinessRuleError(
            f"No exchange rate available for {base.upper()}->{quote.upper()}", "RATE_UNAVAILABLE"
        )
    rate, effective, stale = resolved
    converted = _round(Decimal(amount) * rate)
    return {
        "data": {
            "amount": str(_round(Decimal(amount))),
            "base_currency": base.upper(),
            "quote_currency": quote.upper(),
            "rate": str(rate),
            "rate_date": effective.isoformat(),
            "converted_amount": str(converted),
            "is_stale": stale,
        },
        "meta": None,
        "errors": None,
    }


def _user_org(db: Session, user: User) -> Organization | None:
    if user.employee_id:
        employee = db.get(Employee, user.employee_id)
        if employee is not None:
            return db.get(Organization, employee.org_id)
    return db.execute(select(Organization).order_by(Organization.created_at)).scalars().first()


@router.get("/me/preferences/currency", summary="Resolve the current user's display currency")
def my_display_currency(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    pref = db.execute(
        select(DisplayCurrencyPreference).where(DisplayCurrencyPreference.user_id == user.id)
    ).scalar_one_or_none()
    org = _user_org(db, user)
    entity_base = "INR"
    if user.employee_id:
        employee = db.get(Employee, user.employee_id)
        if employee is not None:
            entity = db.get(LegalEntity, employee.legal_entity_id)
            if entity is not None:
                entity_base = entity.base_currency
    org_default = org.default_currency if org else entity_base
    resolved = org_default
    source = "org_default"
    if pref is not None and not pref.use_org_default and pref.display_currency:
        resolved = pref.display_currency
        source = "user_preference"
    return {
        "data": {
            "display_currency": resolved,
            "source": source,
            "org_default": org_default,
            "entity_base_currency": entity_base,
            "use_org_default": pref.use_org_default if pref else True,
            "preference": jsonable(pref.display_currency) if pref else None,
        },
        "meta": None,
        "errors": None,
    }


class DisplayPreferenceIn(BaseModel):
    use_org_default: bool = True
    display_currency: str | None = None


@router.put("/me/preferences/currency", summary="Set the current user's display currency")
def set_display_currency(
    payload: DisplayPreferenceIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    pref = db.execute(
        select(DisplayCurrencyPreference).where(DisplayCurrencyPreference.user_id == user.id)
    ).scalar_one_or_none()
    if pref is None:
        pref = DisplayCurrencyPreference(user_id=user.id)
        db.add(pref)
    pref.use_org_default = payload.use_org_default
    pref.display_currency = None if payload.use_org_default else payload.display_currency
    write_audit(
        db,
        user=user,
        entity_type="display_currency_preferences",
        entity_id=user.id,
        action="update",
        new_values=payload.model_dump(),
    )
    db.commit()
    return {
        "data": {
            "use_org_default": pref.use_org_default,
            "display_currency": pref.display_currency,
        },
        "meta": None,
        "errors": None,
    }


@router.post("/currency/bulk-reset", summary="Reset every user to the organisation default")
def bulk_reset(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("currency.edit")),
) -> dict:
    rows = db.execute(select(DisplayCurrencyPreference)).scalars().all()
    for row in rows:
        row.use_org_default = True
        row.display_currency = None
    write_audit(
        db,
        user=user,
        entity_type="display_currency_preferences",
        entity_id=user.id,
        action="update",
        new_values={"bulk_reset": len(rows)},
    )
    db.commit()
    return {"data": {"reset": len(rows)}, "meta": None, "errors": None}
