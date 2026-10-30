"""Job families, the job catalogue and competencies through the API (JA §21.3)."""

from __future__ import annotations

from typing import Any

from app.core.errors import ErrorCode
from tests.job_architecture import if_match, setup_architecture


class TestJobFamilies:
    def test_create_read_update_and_reparent(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA-FAM-001/002."""
        arch = setup_architecture(db_session, authenticate, with_job=False)
        family = arch.create_family(family_code="PRODUCT", name="Product")
        subfamily = arch.create_family(
            family_code="PLAT",
            name="Platform",
            node_level="SUBFAMILY",
            parent_job_family_id=family["id"],
        )
        assert subfamily["node_level"] == "SUBFAMILY"
        assert subfamily["parent"]["family_code"] == "PRODUCT"

        other_family = arch.create_family(family_code="DATA", name="Data")
        moved = arch.client.patch(
            f"{arch.base}/job-families/{subfamily['id']}",
            json={"parent_job_family_id": other_family["id"], "name": "Data Platform"},
            headers=if_match(subfamily),
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["parent"]["family_code"] == "DATA"

    def test_a_third_level_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-03: the hierarchy is at most ``FAMILY -> SUBFAMILY``."""
        arch = setup_architecture(db_session, authenticate, with_job=False)
        family = arch.create_family(family_code="ENGG2", name="Engineering")
        subfamily = arch.create_family(
            family_code="PLAT2",
            name="Platform",
            node_level="SUBFAMILY",
            parent_job_family_id=family["id"],
        )
        third = arch.client.post(
            f"{arch.base}/job-families",
            json={
                "family_code": "DEEP",
                "name": "Too deep",
                "node_level": "SUBFAMILY",
                "parent_job_family_id": subfamily["id"],
            },
        )
        assert third.status_code == 422, third.text
        assert third.json()["error"]["code"] == ErrorCode.JOB_FAMILY_CYCLE.value

    def test_a_family_cannot_become_a_subfamily(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-03: demoting a top-level family would create a cycle risk, so it is refused."""
        arch = setup_architecture(db_session, authenticate, with_job=False)
        first = arch.create_family(family_code="F1", name="First")
        second = arch.create_family(family_code="F2", name="Second")
        response = arch.client.patch(
            f"{arch.base}/job-families/{first['id']}",
            json={"parent_job_family_id": second["id"]},
            headers=if_match(first),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.JOB_FAMILY_CYCLE.value

    def test_self_parenting_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate, with_job=False)
        family = arch.create_family(family_code="SELF", name="Self")
        response = arch.client.patch(
            f"{arch.base}/job-families/{family['id']}",
            json={"parent_job_family_id": family["id"]},
            headers=if_match(family),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == ErrorCode.JOB_FAMILY_CYCLE.value

    def test_subfamily_without_a_parent_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate, with_job=False)
        response = arch.client.post(
            f"{arch.base}/job-families",
            json={"family_code": "ORPHAN", "name": "Orphan", "node_level": "SUBFAMILY"},
        )
        assert response.status_code == 422

    def test_duplicate_code_is_case_insensitive(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA-FAM-005: codes are unique per organization, case-insensitively."""
        arch = setup_architecture(db_session, authenticate, with_job=False)
        arch.create_family(family_code="UNIQ", name="Unique")
        duplicate = arch.client.post(
            f"{arch.base}/job-families",
            json={"family_code": "uniq", "name": "Another", "node_level": "FAMILY"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == ErrorCode.DUPLICATE_RESOURCE.value

    def test_archive_is_blocked_by_an_active_job(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA-FAM-006 / AC-JA-13."""
        arch = setup_architecture(db_session, authenticate)
        family = arch.reload_family(arch.ids["family_id"])
        response = arch.client.post(
            f"{arch.base}/job-families/{family['id']}/archive",
            json={"reason": "Reorganising the whole catalogue"},
            headers=if_match(family),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.JOB_FAMILY_IN_USE.value

    def test_archive_is_blocked_by_an_active_subfamily(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate, with_job=False)
        family = arch.create_family(family_code="PARENT", name="Parent")
        arch.create_family(
            family_code="CHILD",
            name="Child",
            node_level="SUBFAMILY",
            parent_job_family_id=family["id"],
        )
        response = arch.client.post(
            f"{arch.base}/job-families/{family['id']}/archive",
            json={"reason": "Removing this branch of the tree"},
            headers=if_match(family),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == ErrorCode.JOB_FAMILY_IN_USE.value

    def test_deactivate_archive_and_restore(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate, with_job=False)
        family = arch.create_family(family_code="TEMP", name="Temporary")

        deactivated = arch.client.post(
            f"{arch.base}/job-families/{family['id']}/deactivate",
            json={"reason": "No longer used for new jobs"},
            headers=if_match(family),
        )
        assert deactivated.status_code == 200, deactivated.text
        assert deactivated.json()["status"] == "INACTIVE"

        archived = arch.client.post(
            f"{arch.base}/job-families/{family['id']}/archive",
            json={"reason": "Superseded by the new taxonomy"},
            headers=if_match(deactivated.json()),
        )
        assert archived.status_code == 200, archived.text
        assert archived.json()["archived_at"] is not None

        restored = arch.client.post(
            f"{arch.base}/job-families/{family['id']}/restore",
            json={"reason": "Reinstating the previous taxonomy"},
            headers=if_match(archived.json()),
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["archived_at"] is None
        # JA §15.1: restore returns a catalogue row to INACTIVE, never straight to ACTIVE.
        assert restored.json()["status"] == "INACTIVE"

    def test_stale_etag_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate, with_job=False)
        family = arch.create_family(family_code="ETAG", name="Etag")
        first = arch.client.patch(
            f"{arch.base}/job-families/{family['id']}",
            json={"name": "Renamed once"},
            headers=if_match(family),
        )
        assert first.status_code == 200
        stale = arch.client.patch(
            f"{arch.base}/job-families/{family['id']}",
            json={"name": "Renamed twice"},
            headers=if_match(family),
        )
        assert stale.status_code == 412
        assert stale.json()["error"]["code"] == ErrorCode.PRECONDITION_FAILED.value


class TestJobCatalogue:
    def test_create_read_update_archive_restore(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-01: CRUD plus case-insensitive code uniqueness and a reserved code."""
        arch = setup_architecture(db_session, authenticate, with_job=False)
        job = arch.create_job(job_code="ENG-001", title="Engineer")
        assert job["job_code"] == "ENG-001"
        assert job["effective_from"] is not None

        duplicate = arch.client.post(
            f"{arch.base}/job-catalog",
            json={
                "job_code": "eng-001",
                "title": "Engineer II",
                "job_family_id": arch.ids["family_id"],
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == ErrorCode.DUPLICATE_RESOURCE.value

        read = arch.client.get(f"{arch.base}/job-catalog/{job['id']}")
        assert read.status_code == 200
        assert read.headers["etag"]

        updated = arch.client.patch(
            f"{arch.base}/job-catalog/{job['id']}",
            json={"title": "Senior Engineer", "min_experience_years": 5},
            headers=if_match(read.json()),
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["title"] == "Senior Engineer"
        assert updated.json()["min_experience_years"] == 5

        archived = arch.client.post(
            f"{arch.base}/job-catalog/{job['id']}/archive",
            json={"reason": "Replaced by the new job family structure"},
            headers=if_match(updated.json()),
        )
        assert archived.status_code == 200, archived.text

        # JA §4.5: an archived code stays reserved and is never reused.
        reused = arch.client.post(
            f"{arch.base}/job-catalog",
            json={
                "job_code": "ENG-001",
                "title": "Engineer again",
                "job_family_id": arch.ids["family_id"],
            },
        )
        assert reused.status_code == 409

        restored = arch.client.post(
            f"{arch.base}/job-catalog/{job['id']}/restore",
            json={"reason": "Archived in error during the migration"},
            headers=if_match(archived.json()),
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["status"] == "INACTIVE"

    def test_archive_requires_a_reason_of_ten_characters(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        job = arch.client.get(f"{arch.base}/job-catalog/{arch.ids['job_id']}").json()
        response = arch.client.post(
            f"{arch.base}/job-catalog/{job['id']}/archive",
            json={"reason": "short"},
            headers=if_match(job),
        )
        assert response.status_code == 422

    def test_grade_range_is_validated_by_rank(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-04: an inverted range and an out-of-range default are both rejected."""
        arch = setup_architecture(db_session, authenticate, with_job=False)
        inverted = arch.client.post(
            f"{arch.base}/job-catalog",
            json={
                "job_code": "BAD-RANGE",
                "title": "Engineer",
                "job_family_id": arch.ids["family_id"],
                "min_grade_id": arch.ids["grade_5_id"],
                "max_grade_id": arch.ids["grade_1_id"],
            },
        )
        assert inverted.status_code == 422, inverted.text
        assert inverted.json()["error"]["code"] == ErrorCode.INVALID_GRADE_RANGE.value

        outside = arch.client.post(
            f"{arch.base}/job-catalog",
            json={
                "job_code": "BAD-DEFAULT",
                "title": "Engineer",
                "job_family_id": arch.ids["family_id"],
                "default_grade_id": arch.ids["grade_1_id"],
                "min_grade_id": arch.ids["grade_3_id"],
                "max_grade_id": arch.ids["grade_5_id"],
            },
        )
        assert outside.status_code == 422
        assert outside.json()["error"]["code"] == ErrorCode.INVALID_GRADE_RANGE.value

        valid = arch.create_job(
            job_code="GOOD-RANGE",
            default_grade_id=arch.ids["grade_3_id"],
            min_grade_id=arch.ids["grade_1_id"],
            max_grade_id=arch.ids["grade_5_id"],
        )
        assert valid["min_grade"]["rank"] == 1
        assert valid["max_grade"]["rank"] == 5
        assert valid["default_grade"]["rank"] == 3

    def test_duplicate_title_in_a_family_warns_and_can_be_acknowledged(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §14.5 ``W-JOB-TITLE-DUPLICATE`` follows the §21.3 acknowledgement protocol."""
        arch = setup_architecture(db_session, authenticate, with_job=False)
        arch.create_job(job_code="DUP-1", title="Engineer")
        blocked = arch.client.post(
            f"{arch.base}/job-catalog",
            json={
                "job_code": "DUP-2",
                "title": "Engineer",
                "job_family_id": arch.ids["family_id"],
            },
        )
        assert blocked.status_code == 422, blocked.text
        body = blocked.json()["error"]
        assert body["code"] == ErrorCode.WARNINGS_REQUIRE_ACKNOWLEDGEMENT.value
        assert [warning["code"] for warning in body["warnings"]] == ["W-JOB-TITLE-DUPLICATE"]

        acknowledged = arch.client.post(
            f"{arch.base}/job-catalog",
            json={
                "job_code": "DUP-2",
                "title": "Engineer",
                "job_family_id": arch.ids["family_id"],
                "acknowledged_warnings": ["W-JOB-TITLE-DUPLICATE"],
            },
        )
        assert acknowledged.status_code == 201, acknowledged.text

    def test_the_same_title_in_another_family_does_not_warn(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §14.5 says so explicitly: only a duplicate *within one family* warns."""
        arch = setup_architecture(db_session, authenticate, with_job=False)
        arch.create_job(job_code="T-1", title="Engineer")
        other = arch.create_family(family_code="OTHERFAM", name="Other")
        response = arch.client.post(
            f"{arch.base}/job-catalog",
            json={"job_code": "T-2", "title": "Engineer", "job_family_id": other["id"]},
        )
        assert response.status_code == 201, response.text

    def test_filtering_by_family_includes_subfamilies(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA-FAM-004 / §17.2."""
        arch = setup_architecture(db_session, authenticate, with_job=False)
        subfamily = arch.create_family(
            family_code="SUBF",
            name="Sub",
            node_level="SUBFAMILY",
            parent_job_family_id=arch.ids["family_id"],
        )
        arch.create_job(job_code="IN-FAMILY", title="In family")
        arch.create_job(
            job_code="IN-SUBFAMILY", title="In subfamily", job_family_id=subfamily["id"]
        )

        with_subfamilies = arch.client.get(
            f"{arch.base}/job-catalog",
            params={"job_family_id": arch.ids["family_id"]},
        )
        codes = {row["job_code"] for row in with_subfamilies.json()["items"]}
        assert codes == {"IN-FAMILY", "IN-SUBFAMILY"}

        without = arch.client.get(
            f"{arch.base}/job-catalog",
            params={"job_family_id": arch.ids["family_id"], "include_subfamilies": False},
        )
        assert {row["job_code"] for row in without.json()["items"]} == {"IN-FAMILY"}

    def test_inactive_family_cannot_classify_a_new_job(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate, with_job=False)
        family = arch.create_family(family_code="DEAD", name="Deactivated")
        arch.client.post(
            f"{arch.base}/job-families/{family['id']}/deactivate",
            json={"reason": "Not accepting new jobs"},
            headers=if_match(family),
        )
        response = arch.client.post(
            f"{arch.base}/job-catalog",
            json={"job_code": "NEW-1", "title": "Engineer", "job_family_id": family["id"]},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == ErrorCode.MASTER_INACTIVE.value

    def test_unknown_sort_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """§26.4 / §17.1: the sort allowlist refuses anything it does not name."""
        arch = setup_architecture(db_session, authenticate)
        response = arch.client.get(f"{arch.base}/job-catalog", params={"sort": "salary"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value

    def test_system_fields_cannot_be_supplied(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """§19.4: ``extra="forbid"`` makes a mass-assignment attempt a 422."""
        arch = setup_architecture(db_session, authenticate, with_job=False)
        response = arch.client.post(
            f"{arch.base}/job-catalog",
            json={
                "job_code": "SYS-1",
                "title": "Engineer",
                "job_family_id": arch.ids["family_id"],
                "organization_id": str(arch.organization.id),
                "row_version": 9,
            },
        )
        assert response.status_code == 422


class TestCompetencies:
    def test_catalogue_crud_and_archive_protection(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """AC-JA-05 and AC-JA-13 for competencies."""
        arch = setup_architecture(db_session, authenticate)
        competency = arch.create_competency(competency_code="DIST-SYS", name="Distributed systems")
        assert competency["category_code"] == "TECHNICAL"

        mapped = arch.client.put(
            f"{arch.base}/job-catalog/{arch.ids['job_id']}/competencies",
            json={
                "mappings": [
                    {
                        "competency_id": competency["id"],
                        "requirement_type": "REQUIRED",
                        "min_proficiency": 3,
                    }
                ]
            },
            headers=if_match(arch.ids["job"]),
        )
        assert mapped.status_code == 200, mapped.text
        assert mapped.json()[0]["min_proficiency"] == 3

        blocked = arch.client.post(
            f"{arch.base}/competencies/{competency['id']}/archive",
            json={"reason": "Consolidating the competency catalogue"},
            headers=if_match(competency),
        )
        assert blocked.status_code == 422, blocked.text
        assert blocked.json()["error"]["code"] == ErrorCode.COMPETENCY_IN_USE.value

    def test_unknown_category_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §9.5: the category must be a configured ``COMPETENCY_CATEGORY`` lookup value."""
        arch = setup_architecture(db_session, authenticate, with_job=False)
        response = arch.client.post(
            f"{arch.base}/competencies",
            json={
                "competency_code": "BAD-CAT",
                "name": "Unknown category",
                "category_code": "NOT_A_CATEGORY",
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED.value

    def test_seeded_categories_are_available(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §9.5: an organization can create its first competency without configuring a list."""
        arch = setup_architecture(db_session, authenticate, with_job=False)
        values = arch.client.get(f"{arch.base}/lookups/COMPETENCY_CATEGORY")
        assert values.status_code == 200, values.text
        codes = {row["value_code"] for row in values.json()}
        assert {"TECHNICAL", "BEHAVIOURAL", "LEADERSHIP"} <= codes

    def test_a_duplicate_competency_in_one_payload_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §9.4: a competency may appear once in a replacement payload."""
        arch = setup_architecture(db_session, authenticate)
        competency = arch.create_competency()
        response = arch.client.put(
            f"{arch.base}/job-catalog/{arch.ids['job_id']}/competencies",
            json={
                "mappings": [
                    {"competency_id": competency["id"], "min_proficiency": 2},
                    {"competency_id": competency["id"], "min_proficiency": 4},
                ]
            },
            headers=if_match(arch.ids["job"]),
        )
        assert response.status_code == 422, response.text

    def test_proficiency_outside_one_to_five_is_rejected(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        competency = arch.create_competency()
        for proficiency in (0, 6):
            response = arch.client.put(
                f"{arch.base}/job-catalog/{arch.ids['job_id']}/competencies",
                json={
                    "mappings": [
                        {"competency_id": competency["id"], "min_proficiency": proficiency}
                    ]
                },
                headers=if_match(arch.ids["job"]),
            )
            assert response.status_code == 422, proficiency

    def test_replacing_the_set_end_dates_what_left(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        """JA §9.4: a mapping change end-dates the old row; history is not deleted."""
        arch = setup_architecture(db_session, authenticate)
        first = arch.create_competency(competency_code="C-ONE", name="One")
        second = arch.create_competency(competency_code="C-TWO", name="Two")
        job = arch.client.get(f"{arch.base}/job-catalog/{arch.ids['job_id']}").json()

        arch.client.put(
            f"{arch.base}/job-catalog/{job['id']}/competencies",
            json={"mappings": [{"competency_id": first["id"], "min_proficiency": 3}]},
            headers=if_match(job),
        )
        job = arch.client.get(f"{arch.base}/job-catalog/{job['id']}").json()
        replaced = arch.client.put(
            f"{arch.base}/job-catalog/{job['id']}/competencies",
            json={"mappings": [{"competency_id": second["id"], "min_proficiency": 2}]},
            headers=if_match(job),
        )
        assert replaced.status_code == 200, replaced.text
        active = {row["competency_id"] for row in replaced.json()}
        assert active == {second["id"]}

        # The competency that left is no longer required, so it can now be archived.
        archived = arch.client.post(
            f"{arch.base}/competencies/{first['id']}/archive",
            json={"reason": "No longer part of any job requirement"},
            headers=if_match(first),
        )
        assert archived.status_code == 200, archived.text

    def test_inactive_competency_cannot_be_newly_required(
        self, api_client: Any, db_session: Any, authenticate: Any
    ) -> None:
        arch = setup_architecture(db_session, authenticate)
        competency = arch.create_competency()
        arch.client.post(
            f"{arch.base}/competencies/{competency['id']}/deactivate",
            json={"reason": "Retired from the catalogue"},
            headers=if_match(competency),
        )
        response = arch.client.put(
            f"{arch.base}/job-catalog/{arch.ids['job_id']}/competencies",
            json={"mappings": [{"competency_id": competency["id"], "min_proficiency": 3}]},
            headers=if_match(arch.ids["job"]),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == ErrorCode.MASTER_INACTIVE.value
