"""Aggregate API router: custom workflow routers first, then generic resources."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.registry import build_all_resource_routers
from app.api.routes import (
    access,
    analytics,
    auth,
    currency,
    documents,
    employees,
    finance,
    hr,
    lookups,
    me,
    payroll,
    settings as settings_routes,
    workflow,
)

api_router = APIRouter()

# Custom routes are registered before the generic resource routers so that
# literal paths such as /approvals/pending win over /{item_id} matchers.
for custom_router in (
    auth.router,
    access.router,
    lookups.router,
    me.router,
    settings_routes.router,
    analytics.router,
    currency.router,
    documents.router,
    employees.router,
    payroll.router,
    finance.router,
    hr.router,
    workflow.router,
):
    api_router.include_router(custom_router)

for resource_router in build_all_resource_routers():
    api_router.include_router(resource_router)
