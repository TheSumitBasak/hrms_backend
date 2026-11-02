"""Statutory rule versions: browse, draft, publish (payroll addendum §4.6, PCFG-20 to PCFG-23)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.audit.actions import AuditAction
from app.audit.events import AuditEvent, build_change_set
from app.audit.writer import AuditWriter
from app.core.clock import utc_now
from app.core.context import ActorContext
from app.core.errors import RuleSelfPublishNotAllowed, ValidationFailed
from app.db.types import uuid7
from app.models.statutory_rules import (
    StatutoryRuleListValue,
    StatutoryRuleParameter,
    StatutoryRuleSet,
    StatutoryRuleSlab,
    StatutoryRuleVersion,
)
from app.payroll.rules import resolve, schema_matches_family, slab_problems
from app.repositories.statutory_rules import StatutoryRuleRepository, VersionContents

_NUMERIC_TYPES = frozenset({"DECIMAL", "MONEY", "RATE_PCT", "INTEGER"})


class StatutoryRuleService:
    def __init__(self, *, rules: StatutoryRuleRepository, audit: AuditWriter) -> None:
        self._rules = rules
        self._audit = audit

    # -------------------------------------------------------------------- browse

    def browse(
        self,
        *,
        family: str | None,
        jurisdiction_code: str | None,
        as_of: date | None,
    ) -> list[tuple[StatutoryRuleSet, list[tuple[StatutoryRuleVersion, VersionContents]]]]:
        """PUBLISHED versions per rule set; with ``as_of``, only the one in force (PCFG-20)."""
        sets = self._rules.list_sets(family=family, jurisdiction_code=jurisdiction_code)
        published = self._rules.published_versions([row.id for row in sets])
        by_set: dict[uuid.UUID, list[StatutoryRuleVersion]] = {row.id: [] for row in sets}
        for version in published:
            by_set[version.rule_set_id].append(version)
        if as_of is not None:
            for set_id, versions in by_set.items():
                match = resolve(versions, as_of)
                by_set[set_id] = [match] if match else []
        contents = self._rules.contents([v.id for versions in by_set.values() for v in versions])
        return [
            (row, [(version, contents[version.id]) for version in by_set[row.id]]) for row in sets
        ]

    # --------------------------------------------------------------------- draft

    def draft(
        self, actor: ActorContext, set_id: uuid.UUID, values: dict[str, Any]
    ) -> tuple[StatutoryRuleVersion, VersionContents]:
        rule_set = self._rules.require_set(set_id, lock=True)
        if not schema_matches_family(values["parameter_schema_version"], rule_set.rule_family):
            raise ValidationFailed(
                f"parameter_schema_version must be {rule_set.rule_family}.v<n>."
            ).with_detail(field="parameter_schema_version", code="INVALID", message="Wrong family.")
        self._require_unique(values)

        version = StatutoryRuleVersion(
            id=uuid7(),
            rule_set_id=set_id,
            version_number=self._rules.next_version_number(set_id),
            effective_from=values["effective_from"],
            effective_to=values["effective_to"],
            status="DRAFT",
            parameter_schema_version=values["parameter_schema_version"],
            verification_status="UNVERIFIED",
            legal_review_notes=values["legal_review_notes"],
            prepared_by=actor.user_id,
            created_by=actor.user_id,
        )
        contents = VersionContents(
            parameters=[
                _parameter(version.id, item, actor.user_id) for item in values["parameters"]
            ],
            slabs=[
                StatutoryRuleSlab(
                    id=uuid7(), rule_version_id=version.id, **item, created_by=actor.user_id
                )
                for item in values["slabs"]
            ],
            list_values=[
                StatutoryRuleListValue(
                    id=uuid7(), rule_version_id=version.id, **item, created_by=actor.user_id
                )
                for item in values["list_values"]
            ],
        )
        # The version row must exist before children reference it; flush it first.
        self._rules.add(version)
        self._rules.flush()
        self._rules.add(*contents.parameters, *contents.slabs, *contents.list_values)
        self._rules.flush()
        self._record(
            actor,
            AuditAction.STATUTORY_RULE_VERSION_DRAFTED,
            version,
            after={
                "rule_set_id": set_id,
                "version_number": version.version_number,
                "effective_from": version.effective_from,
            },
        )
        return version, contents

    # ------------------------------------------------------------------- publish

    def publish(
        self,
        actor: ActorContext,
        version_id: uuid.UUID,
        *,
        verification_status: str,
        legal_review_notes: str | None,
    ) -> tuple[StatutoryRuleVersion, VersionContents]:
        """PCFG-21 and PCFG-23: maker-checker, and the predecessor's end date follows."""
        version = self._rules.require_version(version_id)
        self._rules.require_set(version.rule_set_id, lock=True)
        # Re-read under the lock so two publishers cannot both see the same predecessor.
        self._rules.session.refresh(version)
        if version.status != "DRAFT":
            raise ValidationFailed("Only a DRAFT version can be published.")
        if version.prepared_by == actor.user_id:
            raise RuleSelfPublishNotAllowed()

        contents = self._rules.contents([version.id])[version.id]
        problems = slab_problems(contents.slabs)
        if problems:
            raise ValidationFailed("Slabs must be contiguous and non-overlapping.").with_detail(
                field="slabs", code="SLAB_INVALID", message="; ".join(problems)
            )

        published = self._rules.published_versions([version.rule_set_id])
        if any(other.effective_from >= version.effective_from for other in published):
            raise ValidationFailed(
                "A version must start after every published version of the rule set."
            ).with_detail(
                field="effective_from", code="RULE_VERSION_OVERLAP", message="Overlaps history."
            )
        for other in published:
            if other.effective_to is None or other.effective_to > version.effective_from:
                other.effective_to = version.effective_from
                other.updated_by = actor.user_id

        version.status = "PUBLISHED"
        version.verification_status = verification_status
        if legal_review_notes is not None:
            version.legal_review_notes = legal_review_notes
        version.published_by = actor.user_id
        version.published_at = utc_now()
        version.updated_by = actor.user_id
        self._rules.flush()
        self._record(
            actor,
            AuditAction.STATUTORY_RULE_VERSION_PUBLISHED,
            version,
            after={
                "version_number": version.version_number,
                "verification_status": verification_status,
            },
        )
        return version, contents

    # ----------------------------------------------------------------- internals

    @staticmethod
    def _require_unique(values: dict[str, Any]) -> None:
        keys = {
            "parameters": [item["parameter_code"] for item in values["parameters"]],
            "slabs": [
                (i["slab_table_code"], i["condition_code"], i["sequence_no"])
                for i in values["slabs"]
            ],
            "list_values": [(i["list_code"], i["value_code"]) for i in values["list_values"]],
        }
        for field, found in keys.items():
            if len(found) != len(set(found)):
                raise ValidationFailed(f"{field} contains a duplicate entry.").with_detail(
                    field=field, code="DUPLICATE", message="Entries must be unique."
                )

    def _record(
        self,
        actor: ActorContext,
        action: AuditAction,
        version: StatutoryRuleVersion,
        *,
        after: dict[str, object],
    ) -> None:
        self._audit.record(
            AuditEvent(
                action=action,
                entity_type="statutory_rule_version",
                entity_id=version.id,
                actor_user_id=actor.user_id,
                actor_role_codes="SSA",
                changes=build_change_set(None, after),
                request_id=actor.metadata.request_id,
                ip_address=actor.metadata.ip_address,
                user_agent=actor.metadata.user_agent,
            )
        )


def _parameter(version_id: uuid.UUID, item: dict[str, Any], user_id: uuid.UUID) -> Any:
    """Map the typed ``value`` onto exactly one value column (the table's CHECK)."""
    kind, raw = item["value_type"], item["value"]
    columns: dict[str, Any] = {}
    if kind == "BOOLEAN":
        if not isinstance(raw, bool):
            raise _bad_value(item, "must be true or false")
        columns["value_boolean"] = raw
    elif isinstance(raw, bool):
        raise _bad_value(item, "must be a string")
    elif kind in _NUMERIC_TYPES:
        try:
            number = Decimal(raw)
        except InvalidOperation:
            raise _bad_value(item, "must be a decimal string") from None
        if (
            not number.is_finite()
            or abs(number) >= 10**13
            or (kind == "INTEGER" and number != number.to_integral_value())
        ):
            raise _bad_value(item, "must be a finite number of the declared type")
        columns["value_decimal"] = number
    elif kind == "DATE":
        try:
            columns["value_date"] = date.fromisoformat(raw)
        except ValueError:
            raise _bad_value(item, "must be an ISO date") from None
    else:
        columns["value_text"] = raw
    return StatutoryRuleParameter(
        id=uuid7(),
        rule_version_id=version_id,
        parameter_code=item["parameter_code"],
        value_type=kind,
        created_by=user_id,
        **columns,
    )


def _bad_value(item: dict[str, Any], reason: str) -> ValidationFailed:
    return ValidationFailed(f"Parameter {item['parameter_code']}: value {reason}.").with_detail(
        field="parameters", code="INVALID", message=reason
    )


__all__ = ["StatutoryRuleService"]
