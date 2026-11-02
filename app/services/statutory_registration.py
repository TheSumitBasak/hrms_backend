"""Payroll attributes of statutory registrations (payroll addendum §4.5, PCFG-10)."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Sequence
from typing import Any

from app.audit.actions import AuditAction
from app.audit.writer import AuditWriter
from app.core.context import OrgContext
from app.core.crypto import FieldCipher, FieldContext
from app.core.errors import ModuleNotEnabled, ResourceNotFound, ValidationFailed
from app.core.masking import last_four_of
from app.core.permissions import Permission
from app.db.types import uuid7
from app.models.payroll import StatutoryRegistrationBranch, StatutoryRegistrationSetting
from app.repositories.branch import BranchRepository
from app.repositories.organization import (
    OrganizationRegistrationRepository,
    OrganizationSettingsRepository,
)
from app.repositories.payroll import (
    StatutoryRegistrationBranchRepository,
    StatutoryRegistrationSettingRepository,
)
from app.services.organizations import assert_etag

_PF = ("pf_contribution_rate_category", "pf_exempted_trust", "pf_trust_name")
_ESI = ("esi_implemented_area",)
_TAN = (
    "deductor_category",
    "responsible_person_name",
    "responsible_person_designation",
    "responsible_person_pan",
)
_DEFAULTS: dict[str, Any] = {
    "pf_contribution_rate_category": None,
    "pf_exempted_trust": False,
    "pf_trust_name": None,
    "esi_implemented_area": True,
    "deductor_category": None,
    "responsible_person_name": None,
    "responsible_person_designation": None,
    "responsible_person_pan": None,
}
_APPLICABLE: dict[str, tuple[str, ...]] = {
    "PF_ESTABLISHMENT": _PF,
    "ESIC_EMPLOYER": _ESI,
    "ESIC_SUB_CODE": _ESI,
    "TAN": _TAN,
    "PT_REGISTRATION": (),
    "PT_ENROLMENT": (),
    "LWF": (),
}
_BRANCH_TYPES = frozenset({"PF_ESTABLISHMENT", "ESIC_EMPLOYER", "ESIC_SUB_CODE"})
"""PCFG-10 resolves only PF and ESI by branch; PT and LWF resolve by state, TDS by TAN."""

_AUDITED = tuple(field for field in _DEFAULTS if field != "responsible_person_pan")


class StatutoryRegistrationService:
    def __init__(
        self,
        *,
        settings: StatutoryRegistrationSettingRepository,
        setting_branches: StatutoryRegistrationBranchRepository,
        registrations: OrganizationRegistrationRepository,
        branches: BranchRepository,
        org_settings: OrganizationSettingsRepository,
        cipher: FieldCipher,
        audit: AuditWriter,
    ) -> None:
        self._settings = settings
        self._setting_branches = setting_branches
        self._registrations = registrations
        self._branches = branches
        self._org_settings = org_settings
        self._cipher = cipher
        self._audit = audit

    def get(
        self, context: OrgContext, registration_id: uuid.UUID
    ) -> tuple[StatutoryRegistrationSetting, Sequence[StatutoryRegistrationBranch]]:
        context.require(Permission.STATUTORY_CONFIG_MANAGE)
        self._registrations.require(registration_id, resource="registration")
        row = self._settings.for_registration(registration_id)
        if row is None:
            raise ResourceNotFound("statutory registration settings")
        return row, self._setting_branches.for_setting(row.id)

    def replace(
        self,
        context: OrgContext,
        registration_id: uuid.UUID,
        values: dict[str, Any],
        *,
        if_match: int | None,
    ) -> tuple[StatutoryRegistrationSetting, Sequence[StatutoryRegistrationBranch]]:
        """Create on first call, replace afterwards; the branch list is replaced with it."""
        context.require(Permission.STATUTORY_CONFIG_MANAGE)
        if not self._org_settings.require(context.organization_id).enable_payroll_module:
            raise ModuleNotEnabled()
        registration = self._registrations.require(registration_id, resource="registration")
        branch_rows: list[dict[str, Any]] = values.pop("branches")
        self._check_applicable(registration.registration_type, values, branch_rows)
        self._check_branches(branch_rows)

        row = self._settings.for_registration(registration_id)
        fields = {field: values[field] for field in _AUDITED}
        if row is None:
            row = StatutoryRegistrationSetting(
                id=uuid7(),
                organization_id=context.organization_id,
                registration_id=registration_id,
                created_by=context.user_id,
                **fields,
            )
            self._settings.add(row)
            before: dict[str, Any] = {}
        else:
            assert_etag(row.row_version, if_match)
            before = {field: getattr(row, field) for field in fields}
            for field, value in fields.items():
                setattr(row, field, value)
            row.updated_by = context.user_id
        self._seal_pan(context, row, values["responsible_person_pan"])
        self._settings.flush()

        for old in self._setting_branches.for_setting(row.id):
            self._setting_branches.delete(old)
        # Unit of work runs INSERTs before DELETEs; flush so a re-sent row cannot collide
        # with the unique (setting, branch, effective_from) key.
        self._setting_branches.flush()
        created = [
            StatutoryRegistrationBranch(
                id=uuid7(),
                organization_id=context.organization_id,
                registration_setting_id=row.id,
                created_by=context.user_id,
                **item,
            )
            for item in branch_rows
        ]
        self._setting_branches.add_all(created)
        self._setting_branches.flush()

        self._audit.record_change(
            context,
            action=AuditAction.STATUTORY_REGISTRATION_SETTING_UPDATED,
            entity_type="statutory_registration_setting",
            entity_id=row.id,
            before=before,
            after={**fields, "branch_count": len(created)},
            fields=[*fields, "branch_count"],
        )
        return row, created

    # ------------------------------------------------------------------ internals

    def _check_applicable(
        self, registration_type: str, values: dict[str, Any], branch_rows: list[dict[str, Any]]
    ) -> None:
        if registration_type not in _APPLICABLE:
            raise ValidationFailed(
                f"{registration_type} registrations have no payroll settings."
            ).with_detail(
                field="registration_id", code="NOT_APPLICABLE", message="Unsupported type."
            )
        allowed = _APPLICABLE[registration_type]
        for field, default in _DEFAULTS.items():
            if field not in allowed and values[field] != default:
                raise ValidationFailed(
                    f"{field} does not apply to a {registration_type} registration."
                ).with_detail(field=field, code="NOT_APPLICABLE", message="Not applicable.")
        if branch_rows and registration_type not in _BRANCH_TYPES:
            raise ValidationFailed(
                f"Branches do not apply to a {registration_type} registration."
            ).with_detail(field="branches", code="NOT_APPLICABLE", message="Not applicable.")

    def _check_branches(self, branch_rows: list[dict[str, Any]]) -> None:
        if not branch_rows:
            return
        known = {branch.id for branch in self._branches.list_branches()}
        for item in branch_rows:
            if item["branch_id"] not in known:
                raise ResourceNotFound("branch")
        ordered = sorted(branch_rows, key=lambda item: (item["branch_id"], item["effective_from"]))
        for earlier, later in itertools.pairwise(ordered):
            if earlier["branch_id"] != later["branch_id"]:
                continue
            end = earlier["effective_to"]
            if end is None or end > later["effective_from"]:
                raise ValidationFailed("A branch cannot have overlapping periods.").with_detail(
                    field="branches", code="OVERLAP", message="Periods must not overlap."
                )

    def _seal_pan(
        self, context: OrgContext, row: StatutoryRegistrationSetting, pan: str | None
    ) -> None:
        if pan is None:
            row.responsible_person_pan_encrypted = None
            row.responsible_person_pan_key_version = None
            row.responsible_person_pan_last_four = None
            return
        sealed, version = self._cipher.encrypt(
            pan,
            FieldContext(
                organization_id=context.organization_id,
                entity_type="statutory_registration_setting",
                entity_id=row.id,
                field="responsible_person_pan",
            ),
        )
        row.responsible_person_pan_encrypted = sealed
        row.responsible_person_pan_key_version = version
        row.responsible_person_pan_last_four = last_four_of(pan)


__all__ = ["StatutoryRegistrationService"]
