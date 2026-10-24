"""Current-user preferences: UI prefs and dashboard layout."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.auth import User
from app.models.user_pref import UserPreference
from app.schemas.common import DashboardLayoutIn, PreferencesIn

router = APIRouter(prefix="/me", tags=["Preferences"])


def _prefs(db: Session, user_id) -> UserPreference:
    row = db.execute(
        select(UserPreference).where(UserPreference.user_id == user_id)
    ).scalar_one_or_none()
    if row is None:
        row = UserPreference(user_id=user_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.get("/preferences", summary="Get UI preferences")
def get_preferences(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    row = _prefs(db, user.id)
    return {
        "data": {
            "ui_prefs": row.ui_prefs or {},
            "locale": row.locale,
            "theme": row.theme,
            "active_org_id": str(row.active_org_id) if row.active_org_id else None,
        },
        "meta": None,
        "errors": None,
    }


@router.put("/preferences", summary="Update UI preferences")
def set_preferences(
    payload: PreferencesIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    row = _prefs(db, user.id)
    data = payload.model_dump(exclude_unset=True)
    if "ui_prefs" in data:
        row.ui_prefs = data["ui_prefs"]
    if "locale" in data:
        row.locale = data["locale"]
    if "theme" in data:
        row.theme = data["theme"]
    db.add(row)
    db.commit()
    return {"data": {"ui_prefs": row.ui_prefs or {}, "locale": row.locale, "theme": row.theme}, "meta": None, "errors": None}


@router.get("/dashboard-layout", summary="Get dashboard widget layout")
def get_dashboard_layout(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    row = _prefs(db, user.id)
    return {"data": {"widgets": row.dashboard_layout or []}, "meta": None, "errors": None}


@router.put("/dashboard-layout", summary="Save dashboard widget order/visibility")
def set_dashboard_layout(
    payload: DashboardLayoutIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    row = _prefs(db, user.id)
    row.dashboard_layout = payload.widgets
    db.add(row)
    db.commit()
    return {"data": {"widgets": row.dashboard_layout}, "meta": None, "errors": None}
