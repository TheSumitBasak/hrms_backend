"""Position definition, lifecycle and reporting hierarchy (JA §21.3)."""

from __future__ import annotations

from typing import Any

from app.core.errors import ErrorCode
from tests.job_architecture import idempotent, if_match, setup_architecture


class TestPositionCreation:
    def test_title_and_grade_default_from_the_job(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-06: a position created without a title or grade inherits the job's."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.create_position()
        assert position["position_title"] == "Software Engineer"
        assert position["grade_id"] == arch.ids["grade_3_id"]
        assert position["position_status"] == "DRAFT"
        assert position["occupancy"]["authorized_fte"] == "1.00"
        # JA §8.4: VacantFTE is authorized - occupied - reserved, with no status term, so a
        # DRAFT position reports its full authorization as vacant. Fillability is the
        # separate predicate, and that *is* status-dependent.
        assert position["occupancy"]["vacant_fte"] == "1.00"
        assert position["occupancy"]["occupied_fte"] == "0.00"
        assert position["occupancy"]["is_fillable"] is False

    def test_an_explicit_title_is_kept(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.create_position(position_title="Staff Engineer, Payments")
        assert position["position_title"] == "Staff Engineer, Payments"

    def test_duplicate_code_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        arch.create_position(position_code="POS-1")
        duplicate = arch.client.post(
            f"{arch.base}/positions",
            json={
                "position_code": "pos-1",
                "job_id": arch.ids["job_id"],
                "department_id": arch.ids["department_id"],
            },
            headers=idempotent(),
        )
        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.json()["error"]["code"] == ErrorCode.DUPLICATE_RESOURCE.value

    def test_idempotency_key_is_required_and_replays(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §13.11: a retry must not authorize a second seat."""
        arch = setup_architecture(db_session, authenticate)
        body = {
            "position_code": "IDEM-1",
            "job_id": arch.ids["job_id"],
            "department_id": arch.ids["department_id"],
        }
        missing = arch.client.post(f"{arch.base}/positions", json=body)
        assert missing.status_code == 422, missing.text

        headers = idempotent()
        first = arch.client.post(f"{arch.base}/positions", json=body, headers=headers)
        assert first.status_code == 201, first.text
        replay = arch.client.post(f"{arch.base}/positions", json=body, headers=headers)
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == first.json()["id"]

        listed = arch.client.get(f"{arch.base}/positions", params={"q": "IDEM-1"})
        assert len(listed.json()["items"]) == 1

    def test_same_key_with_a_different_payload_is_a_conflict(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        headers = idempotent()
        first = arch.client.post(
            f"{arch.base}/positions",
            json={
                "position_code": "IDEM-2",
                "job_id": arch.ids["job_id"],
                "department_id": arch.ids["department_id"],
            },
            headers=headers,
        )
        assert first.status_code == 201
        reused = arch.client.post(
            f"{arch.base}/positions",
            json={
                "position_code": "IDEM-3",
                "job_id": arch.ids["job_id"],
                "department_id": arch.ids["department_id"],
            },
            headers=headers,
        )
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == ErrorCode.IDEMPOTENCY_KEY_REUSED.value

    def test_grade_outside_the_job_range_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-04: the position grade must sit inside the job's range."""
        arch = setup_architecture(db_session, authenticate, with_job=False)
        job = arch.create_job(
            job_code="RANGED",
            default_grade_id=arch.ids["grade_3_id"],
            min_grade_id=arch.ids["grade_3_id"],
            max_grade_id=arch.ids["grade_3_id"],
        )
        outside = arch.client.post(
            f"{arch.base}/positions",
            json={
                "position_code": "OUT-1",
                "job_id": job["id"],
                "department_id": arch.ids["department_id"],
                "grade_id": arch.ids["grade_5_id"],
            },
            headers=idempotent(),
        )
        assert outside.status_code == 422, outside.text
        assert outside.json()["error"]["code"] == ErrorCode.POSITION_GRADE_OUT_OF_RANGE.value

        inside = arch.client.post(
            f"{arch.base}/positions",
            json={
                "position_code": "IN-1",
                "job_id": job["id"],
                "department_id": arch.ids["department_id"],
                "grade_id": arch.ids["grade_3_id"],
            },
            headers=idempotent(),
        )
        assert inside.status_code == 201, inside.text

    def test_capacity_cannot_be_patched(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §8.7: a direct budget edit is refused with ``HEADCOUNT_CHANGE_REQUIRED``."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.create_position()
        response = arch.client.patch(
            f"{arch.base}/positions/{position['id']}",
            json={"authorized_fte": "5.00"},
            headers=if_match(position),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.HEADCOUNT_CHANGE_REQUIRED.value

    def test_sub_department_must_belong_to_the_department(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """§6.3: the department / sub-department pair is constrained."""
        arch = setup_architecture(db_session, authenticate)
        other_department = arch.client.post(
            f"{arch.base}/masters/departments", json={"code": "SALES", "name": "Sales"}
        ).json()
        response = arch.client.post(
            f"{arch.base}/positions",
            json={
                "position_code": "MIX-1",
                "job_id": arch.ids["job_id"],
                "department_id": other_department["id"],
                "sub_department_id": arch.ids["sub_department_id"],
            },
            headers=idempotent(),
        )
        assert response.status_code == 422, response.text

    def test_an_inactive_job_cannot_carry_a_new_position(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        job = arch.reload_job(arch.ids["job_id"])
        arch.client.post(
            f"{arch.base}/job-catalog/{job['id']}/deactivate",
            json={"reason": "No longer staffed"},
            headers=if_match(job),
        )
        response = arch.client.post(
            f"{arch.base}/positions",
            json={
                "position_code": "DEADJOB",
                "job_id": job["id"],
                "department_id": arch.ids["department_id"],
            },
            headers=idempotent(),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == ErrorCode.MASTER_INACTIVE.value


class TestPositionLifecycle:
    def test_full_lifecycle_and_history(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-08: open, freeze, reopen, close, archive — each with a history row."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.create_position()

        opened = arch.open_position(position)
        assert opened["position_status"] == "OPEN"
        assert opened["opened_at"] is not None
        assert opened["occupancy"]["is_fillable"] is True

        frozen = arch.client.post(
            f"{arch.base}/positions/{position['id']}/freeze",
            json={"reason": "Hiring paused for the quarter"},
            headers=if_match(opened),
        )
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["position_status"] == "FROZEN"
        assert frozen.json()["occupancy"]["is_fillable"] is False

        reopened = arch.client.post(
            f"{arch.base}/positions/{position['id']}/reopen",
            json={"reason": "Hiring resumed after the review"},
            headers=if_match(frozen.json()),
        )
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["position_status"] == "OPEN"

        closed = arch.client.post(
            f"{arch.base}/positions/{position['id']}/close",
            json={"reason": "Role no longer part of the structure"},
            headers=if_match(reopened.json()),
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["position_status"] == "CLOSED"
        assert closed.json()["closed_at"] is not None

        history = arch.client.get(f"{arch.base}/positions/{position['id']}/history")
        assert history.status_code == 200, history.text
        statuses = [row["to_status"] for row in history.json()["items"] if row["kind"] == "STATUS"]
        assert statuses == ["DRAFT", "OPEN", "FROZEN", "OPEN", "CLOSED"]

        archived = arch.client.post(
            f"{arch.base}/positions/{position['id']}/archive",
            json={"reason": "Closed and removed from the plan"},
            headers=if_match(closed.json()),
        )
        assert archived.status_code == 200, archived.text
        assert archived.json()["archived_at"] is not None

    def test_draft_cannot_be_closed_directly(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-08 / JA §15.7."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.create_position()
        response = arch.client.post(
            f"{arch.base}/positions/{position['id']}/close",
            json={"reason": "Created by mistake this morning"},
            headers=if_match(position),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.INVALID_STATUS_TRANSITION.value

    def test_freeze_requires_a_reason(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        response = arch.client.post(
            f"{arch.base}/positions/{position['id']}/freeze",
            json={},
            headers=if_match(position),
        )
        assert response.status_code == 422, response.text

    def test_lifecycle_requires_if_match(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """§24.1: a state-changing POST on an existing resource needs the ETag."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.create_position()
        response = arch.client.post(
            f"{arch.base}/positions/{position['id']}/open",
            json={"reason": "Authorized by the workforce plan"},
        )
        assert response.status_code == 412, response.text

    def test_a_draft_never_opened_can_be_archived(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §7.8: a position created in error is archived, not closed."""
        arch = setup_architecture(db_session, authenticate)
        position = arch.create_position()
        response = arch.client.post(
            f"{arch.base}/positions/{position['id']}/archive",
            json={"reason": "Duplicate of an existing position"},
            headers=if_match(position),
        )
        assert response.status_code == 200, response.text

    def test_an_open_position_cannot_be_archived(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.open_new_position()
        response = arch.client.post(
            f"{arch.base}/positions/{position['id']}/archive",
            json={"reason": "Trying to skip the closure step"},
            headers=if_match(position),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.POSITION_IN_USE.value


class TestReportingHierarchy:
    def test_cycle_is_rejected(self, api_client: Any, db_session: Any, authenticate: Any) -> None:
        """AC-JA-07: P2->P1, P3->P2, then P1->P3 must be refused."""
        arch = setup_architecture(db_session, authenticate)
        first = arch.create_position(position_code="P1")
        second = arch.create_position(position_code="P2")
        third = arch.create_position(position_code="P3")

        step_one = arch.client.patch(
            f"{arch.base}/positions/{second['id']}",
            json={"reports_to_position_id": first["id"]},
            headers=if_match(second),
        )
        assert step_one.status_code == 200, step_one.text
        step_two = arch.client.patch(
            f"{arch.base}/positions/{third['id']}",
            json={"reports_to_position_id": second["id"]},
            headers=if_match(third),
        )
        assert step_two.status_code == 200, step_two.text

        cycle = arch.client.patch(
            f"{arch.base}/positions/{first['id']}",
            json={"reports_to_position_id": third["id"]},
            headers=if_match(first),
        )
        assert cycle.status_code == 422, cycle.text
        assert cycle.json()["error"]["code"] == ErrorCode.POSITION_REPORTING_CYCLE.value

    def test_self_reference_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.create_position()
        response = arch.client.patch(
            f"{arch.base}/positions/{position['id']}",
            json={"reports_to_position_id": position["id"]},
            headers=if_match(position),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == ErrorCode.POSITION_REPORTING_CYCLE.value

    def test_filtering_by_parent_direct_and_indirect(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """§17.4 ``reports_to_position_id`` with and without ``include_indirect``."""
        arch = setup_architecture(db_session, authenticate)
        root = arch.create_position(position_code="ROOT")
        child = arch.create_position(position_code="CHILD")
        grandchild = arch.create_position(position_code="GRAND")
        arch.client.patch(
            f"{arch.base}/positions/{child['id']}",
            json={"reports_to_position_id": root["id"]},
            headers=if_match(child),
        )
        arch.client.patch(
            f"{arch.base}/positions/{grandchild['id']}",
            json={"reports_to_position_id": child["id"]},
            headers=if_match(grandchild),
        )

        direct = arch.client.get(
            f"{arch.base}/positions", params={"reports_to_position_id": root["id"]}
        )
        assert {row["position_code"] for row in direct.json()["items"]} == {"CHILD"}

        subtree = arch.client.get(
            f"{arch.base}/positions",
            params={"reports_to_position_id": root["id"], "include_indirect": True},
        )
        assert {row["position_code"] for row in subtree.json()["items"]} == {"CHILD", "GRAND"}

    def test_a_closed_position_cannot_be_a_parent(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        parent = arch.open_new_position(position_code="PARENT")
        closed = arch.client.post(
            f"{arch.base}/positions/{parent['id']}/close",
            json={"reason": "Removed from the structure entirely"},
            headers=if_match(parent),
        )
        assert closed.status_code == 200, closed.text

        child = arch.create_position(position_code="ORPHAN")
        response = arch.client.patch(
            f"{arch.base}/positions/{child['id']}",
            json={"reports_to_position_id": parent["id"]},
            headers=if_match(child),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.POSITION_NOT_ACTIVE.value


class TestPositionCompetencies:
    def test_a_position_may_add_but_not_lower_a_job_requirement(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-05: lowering a job requirement is ``COMPETENCY_REQUIREMENT_CONFLICT``."""
        arch = setup_architecture(db_session, authenticate)
        required = arch.create_competency(competency_code="C1", name="Core skill")
        extra = arch.create_competency(competency_code="C2", name="Extra skill")
        job = arch.reload_job(arch.ids["job_id"])
        arch.client.put(
            f"{arch.base}/job-catalog/{job['id']}/competencies",
            json={
                "mappings": [
                    {
                        "competency_id": required["id"],
                        "requirement_type": "REQUIRED",
                        "min_proficiency": 3,
                    }
                ]
            },
            headers=if_match(job),
        )
        position = arch.create_position()

        lowered = arch.client.put(
            f"{arch.base}/positions/{position['id']}/competencies",
            json={"mappings": [{"competency_id": required["id"], "min_proficiency": 1}]},
            headers=if_match(position),
        )
        assert lowered.status_code == 422, lowered.text
        assert lowered.json()["error"]["code"] == ErrorCode.COMPETENCY_REQUIREMENT_CONFLICT.value

        added = arch.client.put(
            f"{arch.base}/positions/{position['id']}/competencies",
            json={
                "mappings": [
                    {"competency_id": extra["id"], "min_proficiency": 2},
                    {"competency_id": required["id"], "min_proficiency": 5},
                ]
            },
            headers=if_match(position),
        )
        assert added.status_code == 200, added.text
        effective = {row["competency_id"]: row for row in added.json()}
        # The union takes the higher minimum for the shared competency (JA §9.3).
        assert effective[required["id"]]["min_proficiency"] == 5
        assert effective[extra["id"]]["min_proficiency"] == 2
        assert effective[extra["id"]]["source"] == "POSITION"

    def test_position_read_shows_the_union(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        competency = arch.create_competency()
        job = arch.reload_job(arch.ids["job_id"])
        arch.client.put(
            f"{arch.base}/job-catalog/{job['id']}/competencies",
            json={"mappings": [{"competency_id": competency["id"], "min_proficiency": 3}]},
            headers=if_match(job),
        )
        position = arch.create_position()
        response = arch.client.get(f"{arch.base}/positions/{position['id']}/competencies")
        assert response.status_code == 200, response.text
        assert [row["source"] for row in response.json()] == ["JOB"]
        assert response.json()[0]["competency_code"] == competency["competency_code"]


class TestPositionSearch:
    def test_filters_sort_and_cursor_pagination(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-19: filters are correct, pages are stable, unknown sort is a 422."""
        arch = setup_architecture(db_session, authenticate)
        for index in range(5):
            arch.create_position(position_code=f"SEQ-{index}")
        opened = arch.open_new_position(position_code="OPENED")

        first_page = arch.client.get(f"{arch.base}/positions", params={"limit": 2})
        assert first_page.status_code == 200, first_page.text
        body = first_page.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"]

        second_page = arch.client.get(
            f"{arch.base}/positions", params={"limit": 2, "cursor": body["next_cursor"]}
        )
        assert second_page.status_code == 200
        first_codes = {row["position_code"] for row in body["items"]}
        second_codes = {row["position_code"] for row in second_page.json()["items"]}
        assert not first_codes & second_codes

        by_status = arch.client.get(f"{arch.base}/positions", params={"position_status": ["OPEN"]})
        assert {row["position_code"] for row in by_status.json()["items"]} == {"OPENED"}

        vacant = arch.client.get(f"{arch.base}/positions", params={"has_vacancy": True})
        assert {row["position_code"] for row in vacant.json()["items"]} == {opened["position_code"]}

        unknown = arch.client.get(f"{arch.base}/positions", params={"sort": "salary"})
        assert unknown.status_code == 422

    def test_archived_positions_are_excluded_by_default(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        position = arch.create_position(position_code="GONE")
        archived = arch.client.post(
            f"{arch.base}/positions/{position['id']}/archive",
            json={"reason": "Created in error during setup"},
            headers=if_match(position),
        )
        assert archived.status_code == 200, archived.text

        default = arch.client.get(f"{arch.base}/positions")
        assert "GONE" not in {row["position_code"] for row in default.json()["items"]}

        included = arch.client.get(f"{arch.base}/positions", params={"include_archived": True})
        assert "GONE" in {row["position_code"] for row in included.json()["items"]}
