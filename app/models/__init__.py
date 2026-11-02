"""SQLAlchemy ORM models.

Model modules are imported through :func:`import_all_models` rather than at package import
time. Several tables carry mutual references — ``users.employee_id`` points at ``employees``
while every business table's ``created_by`` points back at ``users`` — so eager imports at
package level would create an import cycle. Deferring the imports into one function keeps the
registration order explicit and gives Alembic a single call to populate
``Base.metadata``.

Every module listed here must be present for autogenerate to be correct: a module that is
not imported is a table Alembic cannot see, and it would generate a migration that drops it.
"""

from __future__ import annotations

from importlib import import_module

MODEL_MODULES: tuple[str, ...] = (
    "reference",  # states and GST state codes
    "platform",  # users, roles, permissions, refresh tokens, idempotency keys
    "organization",  # groups, organizations, settings, organization files
    "access",  # organization access grants, per-organization permission overrides
    "audit",  # append-only audit log, integrity findings
    "branch",  # branches
    "master",  # nine masters, lookup values, custom statuses
    "employee",  # employees, addresses, job history, status history
    "identifier",  # encrypted identifiers with blind indexes
    "profile",  # education, previous employment, family, nominees, bank accounts
    "document",  # document categories and employee documents
    "salary",  # components, templates, assignments, revision history
    "payroll",  # payroll settings, pay groups, period calendar
    "statutory_rules",  # platform rule sets, versions, parameters, slabs (payroll addendum §4.6)
    "org_config",  # registrations, identifier and nomination policies, notice versions
    "employee_code",  # ID rules, sequence counters, code registry
    "privacy",  # notices, change requests, erasure requests, purge batches
    "position",  # job architecture, positions, assignments, headcount
    "attendance",  # work schedules, shifts, punches, daily records (addendum §5)
    "leave",  # leave types, policies, ledger, balances, applications; job runs (addendum §6, §14)
    "onboarding",  # templates, instances, tasks, submissions (addendum §4)
)
"""Module names under ``app.models``, in dependency order.

Populated as each domain module lands; see ``docs/IMPLEMENTATION_CHECKLIST.md``.
"""


def import_all_models() -> None:
    """Import every model module so ``Base.metadata`` is complete.

    Idempotent: repeated calls are cheap because :func:`importlib.import_module` returns the
    cached module.
    """
    for module_name in MODEL_MODULES:
        import_module(f"app.models.{module_name}")


__all__ = ["MODEL_MODULES", "import_all_models"]
