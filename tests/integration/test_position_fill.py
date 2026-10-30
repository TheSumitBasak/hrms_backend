"""Employee occupancy through the existing job-change endpoint (JA §21.3, §18.2, §18.3)."""

from __future__ import annotations

from typing import Any

from app.core.clock import today_in
from app.core.errors import ErrorCode
from tests.job_architecture import if_match, setup_architecture


class TestFillAndVacancy:
    def test_fill_a_vacant_position(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-09: the assignment is active, occupancy derived, history and audit written."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        employee = arch.create_employee()

        response = arch.fill(employee, position)
        assert response.status_code == 200, response.text
        assignment = response.json()["position_assignment"]
        assert assignment["assignment_status"] == "ACTIVE"
        assert assignment["fte"] == "1.00"
        assert assignment["position_code"] is None or assignment["position_id"] == position["id"]

        occupancy = arch.headcount(position)["occupancy"]
        assert occupancy["occupied_fte"] == "1.00"
        assert occupancy["occupied_headcount"] == 1
        assert occupancy["vacant_fte"] == "0.00"
        assert occupancy["vacant_headcount"] == 0
        assert occupancy["is_fillable"] is False

        history = arch.client.get(f"{arch.base}/employees/{employee['id']}/job-history")
        assert history.status_code == 200, history.text
        assert history.json()[0]["position_id"] == position["id"]

        assignments = arch.client.get(
            f"{arch.base}/employees/{employee['id']}/position-assignments"
        )
        assert assignments.status_code == 200, assignments.text
        assert assignments.json()["items"][0]["position_code"] == position["position_code"]

        audit = arch.client.get(
            f"{arch.base}/audit-logs", params={"entity_type": "position_assignment"}
        )
        actions = {row["action"] for row in audit.json()}
        assert "EMPLOYEE_POSITION_ASSIGNED" in actions

    def test_a_job_change_without_position_change_is_unaffected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-20 / JA §20.7: the pre-existing behaviour is preserved exactly."""
        arch = setup_architecture(db_session, authenticate)
        employee = arch.create_employee()
        response = arch.client.post(
            f"{arch.base}/employees/{employee['id']}/job-changes",
            json={
                "effective_date": "2026-02-01",
                "change_type": "PROMOTION",
                "grade_id": arch.ids["grade_5_id"],
                "reason": "Annual promotion cycle",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["grade_id"] == arch.ids["grade_5_id"]
        assert response.json()["position_assignment"] is None

        assignments = arch.client.get(
            f"{arch.base}/employees/{employee['id']}/position-assignments"
        )
        assert assignments.json()["items"] == []

    def test_a_draft_position_cannot_be_filled(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.create_position()
        employee = arch.create_employee()
        response = arch.fill(employee, position)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.POSITION_NOT_ACTIVE.value

    def test_a_frozen_position_cannot_be_filled(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        frozen = arch.client.post(
            f"{arch.base}/positions/{position['id']}/freeze",
            json={"reason": "Hiring paused pending budget review"},
            headers=if_match(position),
        )
        assert frozen.status_code == 200, frozen.text
        employee = arch.create_employee()
        response = arch.fill(employee, position)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == ErrorCode.POSITION_NOT_ACTIVE.value

    def test_an_archived_position_cannot_be_filled(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.create_position()
        archived = arch.client.post(
            f"{arch.base}/positions/{position['id']}/archive",
            json={"reason": "Created in error during setup"},
            headers=if_match(position),
        )
        assert archived.status_code == 200, archived.text
        employee = arch.create_employee()
        response = arch.fill(employee, position)
        assert response.status_code in {404, 422}


class TestCapacityEnforcement:
    def test_capacity_cannot_be_exceeded(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-10: a second fill is refused at 1.00 FTE and at 0.25 FTE alike."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        first = arch.create_employee()
        second = arch.create_employee()
        assert arch.fill(first, position).status_code == 200

        for fte in ("1.00", "0.25"):
            response = arch.fill(second, position, fte=fte)
            assert response.status_code == 422, response.text
            body = response.json()["error"]
            assert body["code"] == ErrorCode.POSITION_HEADCOUNT_EXCEEDED.value
            # JA §20.9: the numbers must be in the message so the failure is explainable.
            assert "1.00" in body["details"][0]["message"]

        occupancy = arch.headcount(position)["occupancy"]
        assert occupancy["occupied_fte"] == "1.00"
        assert occupancy["occupied_headcount"] == 1

        assignments = arch.client.get(f"{arch.base}/employees/{second['id']}/position-assignments")
        assert assignments.json()["items"] == []

    def test_part_time_and_shared_positions(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-12: FTE and people caps are enforced independently."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position(authorized_fte="2.00", max_incumbents=3)
        first = arch.create_employee()
        second = arch.create_employee()
        third = arch.create_employee()
        fourth = arch.create_employee()

        assert arch.fill(first, position, fte="1.00").status_code == 200
        assert arch.fill(second, position, fte="0.50").status_code == 200
        assert arch.fill(third, position, fte="0.50").status_code == 200

        occupancy = arch.headcount(position)["occupancy"]
        assert occupancy["occupied_fte"] == "2.00"
        assert occupancy["occupied_headcount"] == 3
        assert occupancy["vacant_fte"] == "0.00"

        # The FTE cap is now full even though no further incumbent slot is needed.
        over = arch.fill(fourth, position, fte="0.25")
        assert over.status_code == 422
        assert over.json()["error"]["code"] == ErrorCode.POSITION_HEADCOUNT_EXCEEDED.value

    def test_people_cap_binds_before_the_fte_cap(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §8.2's worked distinction: four half-timers fit 2.00 FTE but not 3 incumbents."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position(authorized_fte="2.00", max_incumbents=3)
        employees = [arch.create_employee() for _ in range(4)]
        for employee in employees[:3]:
            assert arch.fill(employee, position, fte="0.50").status_code == 200

        occupancy = arch.headcount(position)["occupancy"]
        assert occupancy["occupied_fte"] == "1.50"
        assert occupancy["vacant_fte"] == "0.50"
        assert occupancy["vacant_headcount"] == 0

        blocked = arch.fill(employees[3], position, fte="0.50")
        assert blocked.status_code == 422
        detail = blocked.json()["error"]["details"][0]["message"]
        assert "incumbent" in detail

    def test_a_second_primary_assignment_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-12 / JA-A-06: one active ``PRIMARY`` assignment per employee."""
        arch = setup_architecture(db_session, authenticate)
        first_position = arch.open_new_position(position_code="PRIMARY-1")
        second_position = arch.open_new_position(position_code="PRIMARY-2")
        employee = arch.create_employee()
        assert arch.fill(employee, first_position).status_code == 200

        response = arch.fill(
            employee, second_position, effective_date="2026-03-01", end_current=False
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == ErrorCode.POSITION_ASSIGNMENT_OVERLAP.value

    def test_an_acting_assignment_may_coexist_and_counts(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-12: ``ACTING``/``SECONDARY`` are allowed alongside a primary and counted."""
        arch = setup_architecture(db_session, authenticate)
        primary = arch.open_new_position(position_code="MAIN")
        secondary = arch.open_new_position(position_code="EXTRA", authorized_fte="0.50")
        employee = arch.create_employee()
        assert arch.fill(employee, primary).status_code == 200

        response = arch.fill(
            employee,
            secondary,
            fte="0.50",
            assignment_type="SECONDARY",
            effective_date="2026-03-01",
            end_current=False,
        )
        assert response.status_code == 200, response.text
        assert response.json()["position_assignment"]["assignment_type"] == "SECONDARY"

        assert arch.headcount(secondary)["occupancy"]["occupied_fte"] == "0.50"
        # The primary assignment is untouched.
        assert arch.headcount(primary)["occupancy"]["occupied_headcount"] == 1

    def test_assignment_fte_above_one_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA-HC-008: an assignment is a fraction of one role, so FTE is 0.01-1.00."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position(authorized_fte="3.00", max_incumbents=3)
        employee = arch.create_employee()
        response = arch.fill(employee, position, fte="1.50")
        assert response.status_code == 422, response.text


class TestTransferAndEnd:
    def test_transfer_between_positions(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §18.3: the old assignment ends and the new one starts in one transaction."""
        arch = setup_architecture(db_session, authenticate)
        source = arch.open_new_position(position_code="FROM")
        target = arch.open_new_position(position_code="TO")
        employee = arch.create_employee()
        assert arch.fill(employee, source).status_code == 200

        moved = arch.fill(employee, target, effective_date="2026-04-01")
        assert moved.status_code == 200, moved.text
        assert moved.json()["position_assignment"]["position_id"] == target["id"]

        assert arch.headcount(source)["occupancy"]["occupied_fte"] == "0.00"
        assert arch.headcount(source)["occupancy"]["is_fillable"] is True
        assert arch.headcount(target)["occupancy"]["occupied_fte"] == "1.00"

        assignments = arch.client.get(
            f"{arch.base}/employees/{employee['id']}/position-assignments"
        ).json()["items"]
        by_position = {row["position_id"]: row for row in assignments}
        assert by_position[source["id"]]["assignment_status"] == "ENDED"
        assert by_position[source["id"]]["effective_to"] == "2026-04-01"
        assert by_position[target["id"]]["assignment_status"] == "ACTIVE"

    def test_ending_occupancy_without_a_new_position(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """A move out of a position with no replacement: ``position_id`` omitted."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        employee = arch.create_employee()
        assert arch.fill(employee, position).status_code == 200

        response = arch.client.post(
            f"{arch.base}/employees/{employee['id']}/job-changes",
            json={
                "effective_date": "2026-05-01",
                "change_type": "TRANSFER",
                "reason": "Moved out of the position pending reassignment",
                "position_change": {
                    "end_current_assignment": True,
                    "reason": "Role vacated",
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["position_assignment"] is None
        assert arch.headcount(position)["occupancy"]["occupied_fte"] == "0.00"

    def test_a_position_with_an_incumbent_cannot_be_closed(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-13: ``POSITION_IN_USE`` until occupancy is zero."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        employee = arch.create_employee()
        assert arch.fill(employee, position).status_code == 200

        current = arch.reload_position(position["id"])
        response = arch.client.post(
            f"{arch.base}/positions/{position['id']}/close",
            json={"reason": "Trying to close an occupied seat"},
            headers=if_match(current),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.POSITION_IN_USE.value

    def test_a_job_with_an_open_position_cannot_be_archived(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-13: ``JOB_IN_USE``."""
        arch = setup_architecture(db_session, authenticate)
        arch.open_new_position()
        job = arch.reload_job(arch.ids["job_id"])
        response = arch.client.post(
            f"{arch.base}/job-catalog/{job['id']}/archive",
            json={"reason": "Consolidating the job catalogue"},
            headers=if_match(job),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.JOB_IN_USE.value

    def test_separation_ends_occupancy_at_the_day_after_the_last_working_day(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-17: the assignment ends at ``employment_end_date + 1`` (JA §10.15)."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        employee = arch.create_employee()
        assert arch.fill(employee, position).status_code == 200

        notice = arch.client.post(
            f"{arch.base}/employees/{employee['id']}/status-transitions",
            json={
                "to_status": "ON_NOTICE",
                "status_effective_date": "2026-05-01",
                "employment_end_date": "2026-05-31",
                "status_reason": "RESIGNATION",
            },
        )
        assert notice.status_code == 200, notice.text

        separated = arch.client.post(
            f"{arch.base}/employees/{employee['id']}/status-transitions",
            json={
                "to_status": "RESIGNED",
                "status_effective_date": "2026-05-31",
                "employment_end_date": "2026-05-31",
                "status_reason": "RESIGNATION",
            },
        )
        assert separated.status_code == 200, separated.text

        assignments = arch.client.get(
            f"{arch.base}/employees/{employee['id']}/position-assignments"
        ).json()["items"]
        assert assignments[0]["assignment_status"] == "ENDED"
        assert assignments[0]["effective_to"] == "2026-06-01"
        assert assignments[0]["end_reason"]

        # The seat is free again, and now closable.
        assert arch.headcount(position)["occupancy"]["occupied_fte"] == "0.00"
        current = arch.reload_position(position["id"])
        closed = arch.client.post(
            f"{arch.base}/positions/{position['id']}/close",
            json={"reason": "Not refilling after the resignation"},
            headers=if_match(current),
        )
        assert closed.status_code == 200, closed.text


class TestJobAttributeConsistency:
    def test_the_position_department_and_grade_are_adopted(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §19.11: the employee's job attributes follow the position they move into."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position(grade_id=arch.ids["grade_5_id"])
        employee = arch.create_employee()
        assert employee["grade_id"] != arch.ids["grade_5_id"]

        response = arch.fill(employee, position)
        assert response.status_code == 200, response.text
        assert response.json()["grade_id"] == arch.ids["grade_5_id"]
        assert response.json()["department_id"] == arch.ids["department_id"]

    def test_a_contradicting_grade_is_rejected_rather_than_overwritten(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §19.11: a conflict is the client's to resolve, not ours to silently pick."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position(grade_id=arch.ids["grade_5_id"])
        employee = arch.create_employee()
        response = arch.fill(employee, position, grade_id=arch.ids["grade_1_id"])
        assert response.status_code == 422, response.text
        assert response.json()["error"]["details"][0]["field"] == "grade_id"

    def test_an_assignment_cannot_start_before_the_joining_date(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        employee = arch.create_employee()
        response = arch.fill(employee, position, effective_date="2025-01-01")
        assert response.status_code == 422, response.text


class TestFutureDatedAssignments:
    """JA-Q-08: the job-change endpoint keeps base Q-06; SCHEDULED rows come from a service.

    The specification is explicit that *employee job-history changes continue to respect base
    Q-06* (``effective_date <= today``), and that a future-dated assignment is allowed "only
    as a ``SCHEDULED`` row created by an approved onboarding/headcount path". No onboarding
    module exists in this repository (JA-Q-10), so the only producer of a ``SCHEDULED`` row is
    the assignment service itself, which these two tests cover from both sides.
    """

    def test_a_future_job_change_is_still_refused(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        employee = arch.create_employee()
        response = arch.fill(employee, position, effective_date="2099-01-01")
        assert response.status_code == 422, response.text
        detail = response.json()["error"]["details"][0]
        assert detail["field"] == "effective_date"
        assert detail["code"] == "JOB_CHANGE_DATE_INVALID"

    def test_a_scheduled_assignment_does_not_occupy_until_activated(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """The onboarding boundary of JA §18.2, exercised through the service it must use.

        A ``SCHEDULED`` row consumes no capacity (JA §8.3), still blocks a close (JA §7.8),
        and becomes ``ACTIVE`` only when the activation job promotes it (JA §15.3).
        """
        from datetime import timedelta

        from app.models.position import AssignmentSource
        from tests.job_architecture import assignment_service_for, org_context_for

        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        employee = arch.create_employee()
        # The organization clock, not the host clock: §1.4 defines "today" per tenant.
        start = today_in(None) + timedelta(days=30)

        service, session = assignment_service_for(arch)
        context = org_context_for(arch, session)
        employee_row = service.employee_row(employee["id"])
        assignment = service.apply_position_change(
            context,
            employee=employee_row,
            change={
                "position_id": position["id"],
                "assignment_type": "PRIMARY",
                "fte": "1.00",
                "end_current_assignment": True,
                "reason": "Onboarding commitment",
            },
            effective_date=start,
            job_change_values={},
            warnings=_collector(),
            source=AssignmentSource.ONBOARDING.value,
        )
        assert assignment is not None
        assert assignment.assignment_status == "SCHEDULED"
        session.commit()

        occupancy = arch.headcount(position)["occupancy"]
        assert occupancy["occupied_fte"] == "0.00"
        assert occupancy["occupied_headcount"] == 0

        current = arch.reload_position(position["id"])
        blocked = arch.client.post(
            f"{arch.base}/positions/{position['id']}/close",
            json={"reason": "Closing despite a scheduled fill"},
            headers=if_match(current),
        )
        assert blocked.status_code == 422, blocked.text
        assert blocked.json()["error"]["code"] == ErrorCode.POSITION_IN_USE.value

        # The activation job promotes it once the date arrives, re-checking capacity.
        service, session = assignment_service_for(arch)
        context = org_context_for(arch, session)
        activated = service.activate_due(context, as_of=start)
        session.commit()
        assert len(activated) == 1

        # Now ACTIVE, but §8.3 also requires `effective_from <= as_of`: the row counts on its
        # own start date, not before it.
        assert arch.headcount(position)["occupancy"]["occupied_fte"] == "0.00"
        on_the_day = arch.headcount(position, as_of=start.isoformat())["occupancy"]
        assert on_the_day["occupied_fte"] == "1.00"
        assert on_the_day["occupied_headcount"] == 1


def _collector() -> Any:
    from app.core.warnings import WarningCollector

    return WarningCollector([])
