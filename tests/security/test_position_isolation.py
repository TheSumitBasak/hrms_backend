"""Tenant isolation and the permission matrix for the job architecture (JA §21.4).

Two distinct questions, kept apart because they have different answers:

* **Can a caller reach another organization's data?** No — a path with no grant is
  ``403 ORG_ACCESS_DENIED``, and an id from another organization inside a path the caller
  *does* hold is ``404 RESOURCE_NOT_FOUND``. Neither discloses existence (§24.3).
* **Can a caller do more than their role allows?** No — ``position.view`` does not imply
  ``headcount.view``, ``position.manage`` does not imply ``employee.edit``, and
  ``headcount.manage`` does not imply ``headcount.approve`` (JA §12.4).
"""

from __future__ import annotations

from typing import Any

from app.api.deps import reset_permission_cache
from app.core.errors import ErrorCode
from app.core.permissions import Permission, Role
from tests.factories import create_user, enable_configurable_permission
from tests.job_architecture import idempotent, if_match, setup_architecture


class TestCrossOrganizationIsolation:
    def test_a_path_without_a_grant_is_refused(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-02: no grant for the organization in the path means 403, not 404."""
        first = setup_architecture(db_session, authenticate)
        second = setup_architecture(db_session, authenticate)

        for path in (
            f"/api/v1/orgs/{second.organization.id}/positions",
            f"/api/v1/orgs/{second.organization.id}/job-catalog",
            f"/api/v1/orgs/{second.organization.id}/job-families",
            f"/api/v1/orgs/{second.organization.id}/competencies",
            f"/api/v1/orgs/{second.organization.id}/headcount/summary",
            f"/api/v1/orgs/{second.organization.id}/headcount-plans",
            f"/api/v1/orgs/{second.organization.id}/headcount-change-requests",
        ):
            response = first.client.get(path)
            assert response.status_code == 403, path
            assert response.json()["error"]["code"] == ErrorCode.ORG_ACCESS_DENIED.value

    def test_another_organizations_position_id_is_not_found(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-02: a foreign id in a path the caller holds reads as absent, not forbidden."""
        first = setup_architecture(db_session, authenticate)
        second = setup_architecture(db_session, authenticate)
        foreign = second.open_new_position()

        response = first.client.get(f"{first.base}/positions/{foreign['id']}")
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND.value

    def test_a_foreign_job_cannot_carry_a_local_position(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-02: a cross-organization reference in a body is 422, not 404."""
        first = setup_architecture(db_session, authenticate)
        second = setup_architecture(db_session, authenticate)

        response = first.client.post(
            f"{first.base}/positions",
            json={
                "position_code": "FOREIGN-JOB",
                "job_id": second.ids["job_id"],
                "department_id": first.ids["department_id"],
            },
            headers=idempotent(),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.INVALID_ORGANIZATION_REFERENCE.value

    def test_a_foreign_grade_or_family_cannot_be_referenced(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        first = setup_architecture(db_session, authenticate)
        second = setup_architecture(db_session, authenticate)

        foreign_grade = first.client.post(
            f"{first.base}/job-catalog",
            json={
                "job_code": "X-GRADE",
                "title": "Engineer",
                "job_family_id": first.ids["family_id"],
                "default_grade_id": second.ids["grade_3_id"],
            },
        )
        assert foreign_grade.status_code == 422, foreign_grade.text
        assert (
            foreign_grade.json()["error"]["code"] == ErrorCode.INVALID_ORGANIZATION_REFERENCE.value
        )

        foreign_family = first.client.post(
            f"{first.base}/job-catalog",
            json={
                "job_code": "X-FAMILY",
                "title": "Engineer",
                "job_family_id": second.ids["family_id"],
            },
        )
        assert foreign_family.status_code == 404, foreign_family.text

    def test_a_foreign_reporting_position_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §19.1: the position hierarchy never crosses a tenant boundary."""
        first = setup_architecture(db_session, authenticate)
        second = setup_architecture(db_session, authenticate)
        local = first.create_position()
        foreign = second.create_position()

        response = first.client.patch(
            f"{first.base}/positions/{local['id']}",
            json={"reports_to_position_id": foreign["id"]},
            headers=if_match(local),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.INVALID_ORGANIZATION_REFERENCE.value

    def test_an_employee_cannot_be_assigned_to_a_foreign_position(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        first = setup_architecture(db_session, authenticate)
        second = setup_architecture(db_session, authenticate)
        employee = first.create_employee()
        foreign = second.open_new_position()

        response = first.fill(employee, foreign)
        assert response.status_code in {404, 422}, response.text

    def test_a_foreign_plan_scope_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        first = setup_architecture(db_session, authenticate)
        second = setup_architecture(db_session, authenticate)
        response = first.client.post(
            f"{first.base}/headcount-plans",
            json={
                "plan_code": "X-SCOPE",
                "name": "Plan for another organization's department",
                "scope_type": "DEPARTMENT",
                "department_id": second.ids["department_id"],
                "authorized_fte": "1.00",
                "authorized_headcount": 1,
                "effective_from": "2026-01-01",
            },
            headers=idempotent(),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.INVALID_ORGANIZATION_REFERENCE.value

    def test_a_foreign_report_filter_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """A filter on another tenant's department must not quietly return an empty report.

        An empty result would itself disclose that the department exists and is unstaffed.
        """
        first = setup_architecture(db_session, authenticate)
        second = setup_architecture(db_session, authenticate)
        response = first.client.get(
            f"{first.base}/headcount/summary",
            params={"group_by": "DEPARTMENT", "department_id": second.ids["department_id"]},
        )
        assert response.status_code in {404, 422}, response.text

    def test_each_organization_may_reuse_the_same_codes(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §11.3: codes are unique per organization, not globally."""
        first = setup_architecture(db_session, authenticate)
        second = setup_architecture(db_session, authenticate)
        first.create_position(position_code="SHARED")
        reused = second.client.post(
            f"{second.base}/positions",
            json={
                "position_code": "SHARED",
                "job_id": second.ids["job_id"],
                "department_id": second.ids["department_id"],
            },
            headers=idempotent(),
        )
        assert reused.status_code == 201, reused.text

    def test_lists_never_leak_another_organizations_rows(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        first = setup_architecture(db_session, authenticate)
        second = setup_architecture(db_session, authenticate)
        first.create_position(position_code="MINE")
        second.create_position(position_code="THEIRS")

        listed = first.client.get(f"{first.base}/positions")
        codes = {row["position_code"] for row in listed.json()["items"]}
        assert "THEIRS" not in codes
        assert "MINE" in codes


class TestPermissionMatrix:
    def test_hr_executive_may_read_but_not_manage_positions(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-18: HRE holds ``position.view`` and may fill, but not ``position.manage``."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        employee = arch.create_employee()

        hre = create_user(db_session, grants=[(arch.organization.id, Role.HR_EXECUTIVE)])
        db_session.commit()
        client = authenticate(hre)

        readable = client.get(f"{arch.base}/positions/{position['id']}")
        assert readable.status_code == 200, readable.text

        create = client.post(
            f"{arch.base}/positions",
            json={
                "position_code": "HRE-1",
                "job_id": arch.ids["job_id"],
                "department_id": arch.ids["department_id"],
            },
            headers=idempotent(),
        )
        assert create.status_code == 403, create.text
        assert create.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED.value

        # HRE holds employee.edit and position.view, which is what filling requires.
        filled = client.post(
            f"{arch.base}/employees/{employee['id']}/job-changes",
            json={
                "effective_date": "2026-02-01",
                "change_type": "TRANSFER",
                "reason": "Assigning the new joiner to their seat",
                "position_change": {"position_id": position["id"], "fte": "1.00"},
            },
        )
        assert filled.status_code == 200, filled.text

    def test_hr_executive_cannot_read_headcount(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §12.2: headcount is confidential workforce planning; HRE holds none of it."""
        arch = setup_architecture(db_session, authenticate)
        hre = create_user(db_session, grants=[(arch.organization.id, Role.HR_EXECUTIVE)])
        db_session.commit()
        client = authenticate(hre)

        for path in ("headcount/summary", "headcount-plans", "headcount-change-requests"):
            response = client.get(f"{arch.base}/{path}")
            assert response.status_code == 403, path
            assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED.value

    def test_department_manager_and_viewers_cannot_read_positions(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §12.2: DM, RV and RDR receive neither ``position.view`` nor ``headcount.view``."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()

        for role in (
            Role.DEPARTMENT_MANAGER,
            Role.READ_ONLY_VIEWER,
            Role.RESTRICTED_DOCUMENT_REVIEWER,
        ):
            user = create_user(db_session, grants=[(arch.organization.id, role)])
            db_session.commit()
            client = authenticate(user)

            positions = client.get(f"{arch.base}/positions")
            assert positions.status_code == 403, role
            single = client.get(f"{arch.base}/positions/{position['id']}")
            assert single.status_code == 403, role
            headcount = client.get(f"{arch.base}/headcount/summary")
            assert headcount.status_code == 403, role

            # The job catalogue is reference data and *is* readable: it exposes no headcount,
            # no incumbent and no salary (JA §12.2 note 6).
            catalogue = client.get(f"{arch.base}/job-catalog")
            assert catalogue.status_code == 200, role

    def test_hr_admin_cannot_manage_positions_until_the_cell_is_enabled(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §12.2: ``position.manage`` is a ``C`` cell for HRA, off until an OA enables it."""
        arch = setup_architecture(db_session, authenticate)
        hra = create_user(db_session, grants=[(arch.organization.id, Role.HR_ADMIN)])
        db_session.commit()
        client = authenticate(hra)

        body = {
            "position_code": "HRA-1",
            "job_id": arch.ids["job_id"],
            "department_id": arch.ids["department_id"],
        }
        before = client.post(f"{arch.base}/positions", json=body, headers=idempotent())
        assert before.status_code == 403, before.text

        enable_configurable_permission(
            db_session,
            organization_id=arch.organization.id,
            role=Role.HR_ADMIN,
            permission_code=Permission.POSITION_MANAGE.value,
        )
        db_session.commit()
        # The §5.5 cache is invalidated by AccessService when a cell is enabled through the
        # API. This test writes the override row directly, so it flushes the cache itself
        # rather than waiting out the TTL.
        reset_permission_cache()
        client = authenticate(hra)
        after = client.post(f"{arch.base}/positions", json=body, headers=idempotent())
        assert after.status_code == 201, after.text

    def test_managing_headcount_does_not_imply_approving_it(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §12.4: maker-checker must be separable, so the two grants are distinct."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        hra = create_user(db_session, grants=[(arch.organization.id, Role.HR_ADMIN)])
        db_session.commit()
        enable_configurable_permission(
            db_session,
            organization_id=arch.organization.id,
            role=Role.HR_ADMIN,
            permission_code=Permission.HEADCOUNT_MANAGE.value,
        )
        db_session.commit()
        reset_permission_cache()
        client = authenticate(hra)

        created = client.post(
            f"{arch.base}/headcount-change-requests",
            json={
                "position_id": position["id"],
                "change_type": "INCREASE",
                "requested_fte": "2.00",
                "requested_headcount": 2,
                "reason": "Additional capacity for the platform team",
            },
            headers=idempotent(),
        )
        assert created.status_code == 201, created.text
        submitted = client.post(
            f"{arch.base}/headcount-change-requests/{created.json()['id']}/submit",
            headers=if_match(created.json()),
        )
        assert submitted.status_code == 200, submitted.text

        approve = client.post(
            f"{arch.base}/headcount-change-requests/{created.json()['id']}/approve",
            json={"note": "Approving without the approve permission"},
            headers=if_match(submitted.json()),
        )
        assert approve.status_code == 403, approve.text
        assert approve.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED.value

    def test_system_super_admin_holds_no_position_data(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §12.2 note 1: SSA keeps no organization data access; break-glass is the path."""
        arch = setup_architecture(db_session, authenticate)
        ssa = create_user(db_session, grants=[(arch.organization.id, Role.SYSTEM_SUPER_ADMIN)])
        db_session.commit()
        client = authenticate(ssa)

        for path in ("positions", "headcount/summary", "job-catalog"):
            response = client.get(f"{arch.base}/{path}")
            assert response.status_code == 403, path


class TestAuditDisclosure:
    def test_headcount_values_are_redacted_without_headcount_view(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §16.4 / JA-Q-12: the event is visible; the establishment numbers are not."""
        arch = setup_architecture(db_session, authenticate)
        arch.open_new_position(authorized_fte="3.00", max_incumbents=3)

        with_view = arch.client.get(
            f"{arch.base}/audit-logs", params={"action": ["POSITION_CREATED"]}
        )
        assert with_view.status_code == 200, with_view.text
        assert with_view.json(), "the position create must be audited"
        assert with_view.json()[0]["after_values"]["authorized_fte"] == "3.00"

        # An HR Admin holds audit.view and headcount.view by default, so take the view away
        # by using a role that has audit.view without it... HRE has neither, so grant an
        # explicit HRA and check the positive case, then assert the redaction path directly
        # for a caller who lacks headcount.view.
        from app.api.v1.endpoints.audit import REDACTED, _apply_headcount_gating
        from app.core.context import OrgContext, RequestMetadata
        from app.core.permissions import PermissionSet
        from app.schemas.audit import AuditLogResponse

        rows = [AuditLogResponse.model_validate(row) for row in with_view.json()]
        context = OrgContext(
            user_id=arch.organization.id,
            email="viewer@example.test",
            display_name="Auditor",
            metadata=RequestMetadata(),
            organization_id=arch.organization.id,
            permissions=PermissionSet(
                roles=frozenset(),
                permissions=frozenset({Permission.AUDIT_VIEW}),
            ),
        )
        gated = _apply_headcount_gating(context, rows)
        assert gated[0].after_values is not None
        assert set(gated[0].after_values.values()) == {REDACTED}
        # The field names survive, so an auditor still sees *what* changed (JA §16.4).
        assert "authorized_fte" in gated[0].after_values

    def test_headcount_reports_contain_no_personal_data(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-18: a caller with ``headcount.view`` sees counts, not incumbents."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        employee = arch.create_employee()
        assert arch.fill(employee, position).status_code == 200

        for path, params in (
            ("headcount/summary", {"group_by": "POSITION"}),
            ("headcount/reconciliation", {}),
        ):
            response = arch.client.get(f"{arch.base}/{path}", params=params)
            assert response.status_code == 200, path
            body = response.text
            assert employee["first_name"] not in body
            assert employee["last_name"] not in body
            assert str(employee["id"]) not in body
