"""The ``/api/v1`` router.

Routers are mounted here and nowhere else, so the API surface is readable in one file. That
matters for AC-24: the test that asserts no out-of-scope endpoint exists reads the generated
OpenAPI document, and this is the list that determines it.

Mounting order follows the §24.4 sections. Domain routers are added as each lands; see
``docs/IMPLEMENTATION_CHECKLIST.md`` for current coverage.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    attendance,
    audit,
    auth,
    bank,
    catalog,
    documents,
    employees,
    headcount,
    id_rules,
    identifiers,
    job_catalog,
    leave,
    meta,
    onboarding,
    organizations,
    payroll,
    positions,
    privacy,
    profile,
    salary,
    schedule,
    statutory_rules,
    transfers,
    users,
)

api_router = APIRouter()

# §24.4.8 — authentication, users, access grants, break-glass, role catalogue.
api_router.include_router(auth.router)
api_router.include_router(meta.router)
api_router.include_router(users.router)

# §24.4.1 — organizations, settings, registrations, identifier and nomination policies.
api_router.include_router(organizations.router)
api_router.include_router(organizations.group_router)

# §24.4.2 — branches, masters, lookups, custom statuses.
api_router.include_router(catalog.router)

# §24.4.7 — employee ID rules, sequences, code registry.
api_router.include_router(id_rules.router)

# §24.4.3 — employees, status transitions, job changes, code reassignment.
api_router.include_router(employees.router)
api_router.include_router(audit.router)

# §24.4.4 — identifiers, duplicate check and audited reveal.
api_router.include_router(identifiers.router)

# §24.4.4 — bank accounts, primary invariant and audited reveal.
api_router.include_router(bank.router)
api_router.include_router(profile.router)
api_router.include_router(documents.router)
api_router.include_router(documents.storage_router)
api_router.include_router(salary.router)

# Payroll addendum §15.2 — settings, pay groups, period calendar.
api_router.include_router(payroll.router)
api_router.include_router(statutory_rules.router)
api_router.include_router(privacy.router)
api_router.include_router(transfers.router)

# JA — job catalogue, competencies, positions, headcount.
api_router.include_router(job_catalog.router)
api_router.include_router(positions.router)
api_router.include_router(headcount.router)

# Addendum §5.2 — weekly offs, holiday calendars, branch calendar assignments.
api_router.include_router(schedule.router)

# Addendum §6 — leave types, policies, ledger, applications and self-service.
api_router.include_router(leave.router)

# Addendum §5 — shifts, policies, assignments, punches, records, corrections.
api_router.include_router(attendance.router)

# Addendum §4 — onboarding templates, instances, tasks, submissions and self-service.
api_router.include_router(onboarding.router)

# Pending routers, in the dependency order of docs/IMPLEMENTATION_CHECKLIST.md:
#   §24.4.3 employees and their lifecycle
#   §24.4.4 identifiers, education, previous employment, family, nominees, bank accounts
#   §24.4.5 document categories and employee documents
#   §24.4.6 salary components, templates, assignments, revisions
#   §24.4.8 audit log reads

__all__ = ["api_router"]
