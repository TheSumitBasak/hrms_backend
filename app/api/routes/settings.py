"""Organisation settings, currency configuration, integrations and feature flags."""
from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import client_ip, get_current_user, require_permission, write_audit
from app.core.errors import APIError, NotFoundError
from app.crud.base import get_or_404, jsonable
from app.models.auth import User
from app.models.currency import CurrencyRate
from app.models.integration import Integration
from app.models.organization import LegalEntity as LegalEntityModel
from app.models.organization import Organization
from app.models.user_pref import TenantSetting, UserPreference

router = APIRouter(tags=["Settings"])

DEFAULT_CURRENCY_SETTINGS: dict = {
    "org_default_display_currency": None,
    "enabled_currencies": [],
    "allow_user_override": True,
    "user_selectable_currencies": [],
    "scoping": [],
    "rate_source": "manual",
    "rate_type": "monthly_fixed",
    "display_mode": "dual",
    "rounding": {},
    "display_surfaces": {
        "payslip_annexure": True,
        "salary_breakup": True,
        "expense_claims": True,
        "loans": True,
        "reports": True,
        "dashboards": True,
    },
    "disclaimer": "Converted amounts are indicative and for display only.",
}

DEFAULT_FEATURES = [
    {"code": "multi_currency", "enabled": True, "plan_tier": "growth"},
    {"code": "advanced_analytics", "enabled": True, "plan_tier": "growth"},
    {"code": "loans", "enabled": True, "plan_tier": "strength"},
    {"code": "expenses", "enabled": True, "plan_tier": "foundation"},
    {"code": "helpdesk", "enabled": True, "plan_tier": "strength"},
    {"code": "e_signature", "enabled": False, "plan_tier": "growth"},
    {"code": "ats", "enabled": False, "plan_tier": "addon"},
    {"code": "us_payroll", "enabled": False, "plan_tier": "addon"},
]


def _active_org(db: Session, user: User | None) -> Organization | None:
    if user is not None:
        prefs = db.execute(
            select(UserPreference).where(UserPreference.user_id == user.id)
        ).scalar_one_or_none()
        if prefs is not None and prefs.active_org_id:
            org = db.get(Organization, prefs.active_org_id)
            if org is not None:
                return org
    return db.execute(select(Organization).order_by(Organization.created_at)).scalars().first()


def _get_setting(db: Session, org_id, key: str) -> dict | None:
    row = db.execute(
        select(TenantSetting).where(TenantSetting.org_id == org_id, TenantSetting.key == key)
    ).scalar_one_or_none()
    return row.value if row is not None else None


def _put_setting(db: Session, org_id, key: str, value: dict) -> None:
    row = db.execute(
        select(TenantSetting).where(TenantSetting.org_id == org_id, TenantSetting.key == key)
    ).scalar_one_or_none()
    if row is None:
        row = TenantSetting(org_id=org_id, key=key, value=value)
        db.add(row)
    else:
        row.value = value
        db.add(row)


# --------------------------------------------------------------------- Org current
@router.get("/organizations/current", summary="Active organisation with settings")
def current_organization(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    org = _active_org(db, user)
    if org is None:
        raise NotFoundError("Organization")
    entities = db.execute(
        select(LegalEntityModel).where(LegalEntityModel.org_id == org.id)
    ).scalars().all()
    return {
        "data": {
            "organization": {
                "id": str(org.id),
                "name": org.name,
                "legal_name": org.legal_name,
                "country_code": org.country_code,
                "default_currency": org.default_currency,
                "timezone": org.timezone,
                "is_active": org.is_active,
            },
            "legal_entities": [
                {
                    "id": str(e.id),
                    "name": e.name,
                    "code": e.code,
                    "base_currency": e.base_currency,
                    "country_code": e.country_code,
                }
                for e in entities
            ],
            "settings": _get_setting(db, org.id, "org") or {},
        },
        "meta": None,
        "errors": None,
    }


# ------------------------------------------------------------------ Currency settings
@router.get("/settings/currency", summary="Organisation currency settings")
def get_currency_settings(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    org = _active_org(db, user)
    if org is None:
        raise NotFoundError("Organization")
    stored = _get_setting(db, org.id, "currency") or {}
    merged = {**DEFAULT_CURRENCY_SETTINGS, **stored}
    if not merged.get("org_default_display_currency"):
        merged["org_default_display_currency"] = org.default_currency
    if not merged.get("enabled_currencies"):
        merged["enabled_currencies"] = [org.default_currency]
    entities = db.execute(
        select(LegalEntityModel).where(LegalEntityModel.org_id == org.id)
    ).scalars().all()
    merged["legal_entity_base_currencies"] = [
        {"legal_entity_id": str(e.id), "name": e.name, "base_currency": e.base_currency}
        for e in entities
    ]
    return {"data": merged, "meta": None, "errors": None}


class CurrencySettingsIn(BaseModel):
    org_default_display_currency: str | None = None
    enabled_currencies: list[str] | None = None
    allow_user_override: bool | None = None
    user_selectable_currencies: list[str] | None = None
    scoping: list[dict] | None = None
    rate_source: str | None = None
    rate_type: str | None = None
    display_mode: str | None = None
    rounding: dict | None = None
    display_surfaces: dict | None = None
    disclaimer: str | None = None


@router.put("/settings/currency", summary="Update organisation currency settings")
def update_currency_settings(
    payload: CurrencySettingsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("currency.edit")),
) -> dict:
    org = _active_org(db, user)
    if org is None:
        raise NotFoundError("Organization")
    stored = _get_setting(db, org.id, "currency") or {}
    stored.update(payload.model_dump(exclude_unset=True))
    _put_setting(db, org.id, "currency", stored)
    write_audit(
        db,
        user=user,
        entity_type="tenant_settings",
        entity_id=org.id,
        action="update",
        new_values=payload.model_dump(exclude_unset=True),
        ip_address=client_ip(request),
        org_id=org.id,
    )
    db.commit()
    return {"data": {**DEFAULT_CURRENCY_SETTINGS, **stored}, "meta": None, "errors": None}


@router.post("/currency-rates/import", summary="Import currency rates from CSV")
def import_currency_rates(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("currency.edit")),
) -> dict:
    org = _active_org(db, user)
    if org is None:
        raise NotFoundError("Organization")
    content = file.file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    imported = 0
    errors: list[dict] = []
    for index, row in enumerate(reader, start=2):
        try:
            base = (row.get("base_currency") or "").strip().upper()
            quote = (row.get("quote_currency") or "").strip().upper()
            if not base or not quote:
                raise ValueError("base_currency and quote_currency are required")
            rate = Decimal(str(row.get("rate")).strip())
            effective = row.get("effective_date") or date.today().isoformat()
            db.add(
                CurrencyRate(
                    org_id=org.id,
                    base_currency=base,
                    quote_currency=quote,
                    rate=rate,
                    effective_date=datetime.fromisoformat(effective).date(),
                    source=(row.get("source") or "import").strip(),
                )
            )
            imported += 1
        except (ValueError, InvalidOperation, TypeError) as exc:
            errors.append({"row": index, "message": str(exc)})
    db.commit()
    return {
        "data": {"imported": imported, "failed": len(errors)},
        "meta": None,
        "errors": errors or None,
    }


# ----------------------------------------------------------------------- Features
@router.get("/tenant/features", summary="Plan-tier feature flags for the tenant")
def tenant_features(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    org = _active_org(db, user)
    overrides = (_get_setting(db, org.id, "features") if org else None) or {}
    features = []
    for feature in DEFAULT_FEATURES:
        item = dict(feature)
        if feature["code"] in overrides:
            item.update(overrides[feature["code"]])
        features.append(item)
    return {"data": features, "meta": {"total": len(features)}, "errors": None}


# -------------------------------------------------------------------- Integrations
class ConnectIn(BaseModel):
    config: dict | None = None


@router.post("/integrations/{integration_id}/connect", summary="Connect an integration")
def connect_integration(
    integration_id: uuid.UUID,
    payload: ConnectIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("integration.edit")),
) -> dict:
    integration = get_or_404(db, Integration, integration_id, "Integration")
    integration.status = "connected"
    integration.connected_at = datetime.now(timezone.utc)
    if payload and payload.config:
        integration.config = {**(integration.config or {}), **payload.config}
    write_audit(
        db,
        user=user,
        entity_type="integrations",
        entity_id=integration.id,
        action="update",
        new_values={"status": "connected"},
        ip_address=client_ip(request) if request else None,
    )
    db.commit()
    return {
        "data": {"id": str(integration.id), "status": integration.status, "connected_at": jsonable(integration.connected_at)},
        "meta": None,
        "errors": None,
    }


class OAuthCallbackIn(BaseModel):
    code: str
    state: str | None = None
    config: dict | None = None


@router.post("/integrations/{integration_id}/callback", summary="OAuth callback for an integration")
def integration_callback(
    integration_id: uuid.UUID,
    payload: OAuthCallbackIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("integration.edit")),
) -> dict:
    integration = get_or_404(db, Integration, integration_id, "Integration")
    if not payload.code:
        raise APIError(400, "Authorization code is required", "VALIDATION")
    integration.status = "connected"
    integration.connected_at = datetime.now(timezone.utc)
    integration.config = {
        **(integration.config or {}),
        "oauth_code": payload.code,
        "oauth_state": payload.state,
        **(payload.config or {}),
    }
    db.commit()
    return {"data": {"id": str(integration.id), "status": integration.status}, "meta": None, "errors": None}
