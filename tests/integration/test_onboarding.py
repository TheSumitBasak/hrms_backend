"""Onboarding: templates, initiation, tasks, activation rules (addendum 4, AC-29..AC-34)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.core.errors import ErrorCode
from app.core.permissions import Role
from tests.job_architecture import if_match
from tests.onboarding_support import CHECKLIST, make_world


def _code(response: Any) -> str:
    return str(response.json()["error"]["code"])


class TestTemplates:
    def test_publish_rules_conflicts_and_selection_order(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        general = w.template("GEN")
        assert general["versions"][0]["status"] == "DRAFT"  # the create response is the draft

        # An identical applicability tuple and priority overlapping in time conflicts (409).
        other = w.template("OTHER", publish=False)
        draft = other["versions"][0]
        conflict = w.hr.post(
            f"{w.base}/onboarding-templates/{other['id']}/versions/{draft['id']}/publish",
            headers=if_match(draft),
        )
        assert conflict.status_code == 409
        assert _code(conflict) == ErrorCode.ONBOARDING_TEMPLATE_CONFLICT.value

        specific = w.template(
            "DEPT", applies_department_id=w.arch.ids["department_id"], priority=100
        )
        employee = w.draft()
        resolved = w.hr.post(
            f"{w.base}/onboarding-templates/resolve", json={"employee_id": employee["id"]}
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["selected"]["template_id"] == specific["id"]
        assert len(resolved.json()["candidates"]) == 2  # the general one matches too

    def test_invalid_definitions_are_refused(self, db_session: Any, authenticate: Any) -> None:
        w = make_world(db_session, authenticate)
        cyclic = w.hr.post(
            f"{w.base}/onboarding-templates",
            json={
                "template_code": "CYC",
                "template_name": "Cyclic",
                "version": {"effective_from": "2026-01-01"},
                "tasks": [
                    {**CHECKLIST, "task_code": "A", "title": "Task A"},
                    {**CHECKLIST, "task_code": "B", "title": "Task B"},
                ],
                "dependencies": [
                    {"task_code": "A", "prerequisite_task_code": "B"},
                    {"task_code": "B", "prerequisite_task_code": "A"},
                ],
            },
        )
        assert cyclic.status_code == 422
        assert _code(cyclic) == ErrorCode.ONBOARDING_TEMPLATE_INVALID.value

        blocking_optional = w.hr.post(
            f"{w.base}/onboarding-templates",
            json={
                "template_code": "BAD",
                "template_name": "Bad",
                "version": {"effective_from": "2026-01-01"},
                "tasks": [
                    {
                        **CHECKLIST,
                        "task_code": "A",
                        "title": "Task A",
                        "is_mandatory": False,
                        "blocks_activation": True,
                    }
                ],
            },
        )
        assert _code(blocking_optional) == ErrorCode.ONBOARDING_TEMPLATE_INVALID.value

    def test_ac34_template_changes_never_rewrite_running_instances(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        template = w.template("STD")
        first = w.initiate(w.draft()).json()
        assert first["template_version_number"] == 1

        # v2: a renamed task and one task removed, effective after v1 started.
        v2 = w.hr.post(f"{w.base}/onboarding-templates/{template['id']}/versions", json={})
        assert v2.status_code == 201, v2.text
        draft = v2.json()
        replaced = w.hr.put(
            f"{w.base}/onboarding-templates/{template['id']}/versions/{draft['id']}/tasks",
            json={
                "tasks": [
                    {**CHECKLIST, "task_code": "SETUP", "title": "Prepare the laptop and badge"}
                ],
                "dependencies": [],
            },
        )
        assert replaced.status_code == 200, replaced.text
        patched = w.hr.patch(
            f"{w.base}/onboarding-templates/{template['id']}/versions/{draft['id']}",
            json={"effective_from": "2026-02-01", "change_note": "Simplified the checklist"},
            headers=if_match(replaced.json()),
        )
        assert patched.status_code == 200, patched.text
        published = w.hr.post(
            f"{w.base}/onboarding-templates/{template['id']}/versions/{draft['id']}/publish",
            headers=if_match(patched.json()),
        )
        assert published.status_code == 200, published.text

        unchanged = w.reload(first)
        assert unchanged["template_version_number"] == 1
        assert {t["task_code"] for t in unchanged["tasks"]} >= {"KYC_CHECK", "WELCOME_KIT"}
        assert w.task(unchanged, "SETUP")["title"] == "Prepare the workstation"

        second = w.initiate(w.draft(joining_date="2026-03-02"))
        assert second.status_code == 201, second.text
        assert second.json()["template_version_number"] == 2
        assert [t["task_code"] for t in second.json()["tasks"]] == ["SETUP"]


class TestInitiation:
    def test_ac29_snapshot_due_dates_events_and_org_isolation(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        w.template("STD")
        employee = w.draft(joining_date="2026-06-01")
        response = w.initiate(employee)
        assert response.status_code == 201, response.text
        instance = response.json()
        assert instance["status"] == "IN_PROGRESS" and instance["selection_mode"] == "AUTOMATIC"
        assert len(instance["tasks"]) == 5 and instance["mandatory_total"] == 4
        assert w.task(instance, "KYC_CHECK")["due_date"] == "2026-05-29"  # joining - 3
        assert w.task(instance, "SETUP")["due_date"] == "2026-06-02"
        full = w.reload(instance)
        assert [e["event_type"] for e in full["events"]] == ["INITIATED"]
        assert instance["progress_percent"] == 0

        foreign = make_world(db_session, authenticate)
        theirs = foreign.template("THEIRS")
        mine = w.draft()
        wrong = w.initiate(mine, template_id=theirs["id"])
        assert wrong.status_code == 422  # a template of another organization does not exist here
        assert w.hr.get(f"{foreign.base}/onboarding/{instance['id']}").status_code == 403
        assert foreign.hr.get(f"{foreign.base}/onboarding/{instance['id']}").status_code == 404

    def test_eligibility_and_missing_template(self, db_session: Any, authenticate: Any) -> None:
        w = make_world(db_session, authenticate)
        employee = w.draft()
        none = w.initiate(employee)
        assert (
            none.status_code == 422
            and _code(none) == ErrorCode.NO_APPLICABLE_ONBOARDING_TEMPLATE.value
        )
        w.template("STD")
        assert w.initiate(employee).status_code == 201
        again = w.initiate(employee)
        assert (
            again.status_code == 409 and _code(again) == ErrorCode.ONBOARDING_ALREADY_EXISTS.value
        )

        disabled = make_world(db_session, authenticate, enable_onboarding_module=False)
        refused = disabled.initiate(disabled.draft())
        assert refused.status_code == 403 and _code(refused) == ErrorCode.MODULE_NOT_ENABLED.value

    def test_initiation_needs_the_permission_and_replays_by_key(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        w.template("STD")
        employee = w.draft()
        viewer = w.client_for(Role.READ_ONLY_VIEWER)
        denied = viewer.post(
            f"{w.base}/employees/{employee['id']}/onboarding",
            json={},
            headers={"Idempotency-Key": "7b0c1d3e-0000-4000-8000-0000000000a1"},
        )
        assert denied.status_code == 403
        key = {"Idempotency-Key": "7b0c1d3e-0000-4000-8000-0000000000a2"}
        url = f"{w.base}/employees/{employee['id']}/onboarding"
        first, replay = w.hr.post(url, json={}, headers=key), w.hr.post(url, json={}, headers=key)
        assert first.status_code == 201 and replay.status_code == 201
        assert first.json()["id"] == replay.json()["id"]
        assert w.hr.post(url, json={}).status_code == 422  # the key is mandatory (4.13)

    def test_ac30_concurrent_initiations_create_exactly_one_instance(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        w.template("STD")
        employee = w.draft()

        def initiate(_: int) -> int:
            return int(w.initiate(employee).status_code)

        with ThreadPoolExecutor(max_workers=3) as pool:
            statuses = list(pool.map(initiate, range(3)))
        assert statuses.count(201) == 1 and statuses.count(409) == 2, statuses
        listing = w.hr.get(f"{w.base}/employees/{employee['id']}/onboarding").json()
        assert len(listing["items"]) == 1


class TestCompletionAndActivation:
    def test_ac31_ac32_mandatory_tasks_block_and_optional_ones_are_cancelled(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        w.template(
            "TWO",
            tasks=[
                {**CHECKLIST, "task_code": "A", "title": "First mandatory task"},
                {**CHECKLIST, "task_code": "B", "title": "Second mandatory task"},
                {**CHECKLIST, "task_code": "OPT", "title": "Optional task", "is_mandatory": False},
            ],
            dependencies=[],
        )
        instance = w.initiate(w.active()).json()

        blocked = w.hr.post(
            f"{w.base}/onboarding/{instance['id']}/complete", headers=if_match(instance)
        )
        assert blocked.status_code == 422
        assert _code(blocked) == ErrorCode.ONBOARDING_MANDATORY_TASKS_INCOMPLETE.value
        listed = str(blocked.json()["error"]["details"])
        assert "A" in listed and "B" in listed

        assert w.act(w.hr, instance, "A", "complete").status_code == 200
        assert w.act(w.hr, instance, "B", "complete").status_code == 200
        assert w.act(w.hr, instance, "OPT", "start").status_code == 200
        current = w.reload(instance)
        assert current["progress_percent"] == 100
        done = w.hr.post(
            f"{w.base}/onboarding/{instance['id']}/complete", headers=if_match(current)
        )
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "COMPLETED"
        assert w.task(done.json(), "OPT")["status"] == "CANCELLED"
        events = [e["event_type"] for e in w.reload(instance)["events"]]
        assert events == ["INITIATED", "COMPLETED"]

    def test_ac33_blocking_tasks_block_activation_until_waived(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        w.template("STD")
        employee = w.draft()
        instance = w.initiate(employee).json()

        blocked = w.hr.post(f"{w.base}/employees/{employee['id']}/activate")
        assert blocked.status_code == 422
        assert _code(blocked) == ErrorCode.ONBOARDING_ACTIVATION_BLOCKED.value
        assert [d["field"] for d in blocked.json()["error"]["details"]] == ["KYC_CHECK"]

        waived = w.act(
            w.hr,
            instance,
            "KYC_CHECK",
            "waive",
            {"waiver_reason": "Verified offline at the branch"},
        )
        assert waived.status_code == 200, waived.text
        activated = w.hr.post(f"{w.base}/employees/{employee['id']}/activate")
        assert activated.status_code == 200, (
            activated.text
        )  # non-blocking mandatory tasks never block

    def test_required_for_activation_setting(self, db_session: Any, authenticate: Any) -> None:
        w = make_world(db_session, authenticate, onboarding_required_for_activation=True)
        w.template("STD")
        employee = w.draft()
        refused = w.hr.post(f"{w.base}/employees/{employee['id']}/activate")
        assert refused.status_code == 422
        assert _code(refused) == ErrorCode.ONBOARDING_REQUIRED_FOR_ACTIVATION.value

    def test_auto_complete_after_the_last_mandatory_task_when_active(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate, onboarding_auto_complete=True)
        w.template(
            "ONE",
            tasks=[
                {**CHECKLIST, "task_code": "ONLY", "title": "The only task"},
                {**CHECKLIST, "task_code": "EXTRA", "title": "Optional", "is_mandatory": False},
            ],
            dependencies=[],
        )
        employee = w.active()
        instance = w.initiate(employee).json()
        assert w.act(w.hr, instance, "ONLY", "complete").status_code == 200
        done = w.reload(instance)
        assert done["status"] == "COMPLETED" and done["completion_mode"] == "AUTOMATIC"
        assert w.task(done, "EXTRA")["status"] == "CANCELLED"
        assert [e["event_type"] for e in done["events"]][-1] == "AUTO_COMPLETED"

    def test_cancel_and_reopen_rules(self, db_session: Any, authenticate: Any) -> None:
        w = make_world(db_session, authenticate)
        w.template(
            "ONE",
            tasks=[{**CHECKLIST, "task_code": "ONLY", "title": "The only task"}],
            dependencies=[],
        )
        employee = w.active()
        instance = w.initiate(employee).json()
        assert w.act(w.hr, instance, "ONLY", "complete").status_code == 200
        current = w.reload(instance)
        done = w.hr.post(
            f"{w.base}/onboarding/{instance['id']}/complete", headers=if_match(current)
        )
        assert done.status_code == 200 and done.json()["status"] == "COMPLETED"
        reopened = w.hr.post(
            f"{w.base}/onboarding/{instance['id']}/reopen",
            json={"reason": "A missing document was found after completion"},
            headers=if_match(done.json()),
        )
        assert reopened.status_code == 200 and reopened.json()["reopened_count"] == 1

        second = w.active()
        other = w.initiate(second).json()
        cancelled = w.hr.post(
            f"{w.base}/onboarding/{other['id']}/cancel",
            json={"reason": "Created for the wrong employee record"},
            headers=if_match(other),
        )
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
        assert all(t["status"] == "CANCELLED" for t in cancelled.json()["tasks"])
        again = w.hr.post(
            f"{w.base}/onboarding/{other['id']}/complete", headers=if_match(cancelled.json())
        )
        assert again.status_code == 422
        assert _code(again) == ErrorCode.INVALID_ONBOARDING_TRANSITION.value
        assert w.initiate(second).status_code == 201  # a cancelled instance frees the slot


class TestTasks:
    def test_dependency_blocking_stale_etag_and_waiver_rules(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        w.template("STD")
        instance = w.initiate(w.draft()).json()
        blocked = w.act(w.hr, instance, "SETUP", "start")
        assert (
            blocked.status_code == 422 and _code(blocked) == ErrorCode.ONBOARDING_TASK_BLOCKED.value
        )
        assert w.task(w.reload(instance), "SETUP")["is_blocked"] is True

        task = w.refresh_task(instance, "KYC_CHECK")
        stale = w.hr.post(
            f"{w.base}/onboarding/{instance['id']}/tasks/{task['id']}/start",
            json={},
            headers={"If-Match": str(task["row_version"] + 5)},
        )
        assert stale.status_code == 412
        assert w.act(w.hr, instance, "KYC_CHECK", "start").status_code == 200
        assert w.act(w.hr, instance, "KYC_CHECK", "complete").status_code == 200
        assert w.act(w.hr, instance, "SETUP", "start").status_code == 200
        assert w.task(w.reload(instance), "SETUP")["is_blocked"] is False

        short = w.act(w.hr, instance, "WELCOME_KIT", "waive", {"waiver_reason": "too short"})
        assert short.status_code == 422
        ok = w.act(
            w.hr,
            instance,
            "WELCOME_KIT",
            "waive",
            {"waiver_reason": "Not needed for this contractor"},
        )
        assert ok.status_code == 200 and ok.json()["status"] == "WAIVED"

    def test_reopen_reassign_due_date_and_adhoc_tasks(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_world(db_session, authenticate)
        w.template("STD")
        employee = w.active()
        instance = w.initiate(employee).json()
        assert w.act(w.hr, instance, "KYC_CHECK", "complete").status_code == 200

        warned = w.act(w.hr, instance, "KYC_CHECK", "reopen", {"reason": "The scan was unreadable"})
        assert (
            warned.status_code == 422
        )  # reopening a blocking task after activation needs the warning
        assert _code(warned) == ErrorCode.WARNINGS_REQUIRE_ACKNOWLEDGEMENT.value
        reopened = w.act(
            w.hr,
            instance,
            "KYC_CHECK",
            "reopen",
            {
                "reason": "The scan was unreadable",
                "acknowledged_warnings": ["W-ONB-REOPEN-AFTER-ACTIVATION"],
            },
        )
        assert reopened.status_code == 200 and reopened.json()["status"] == "IN_PROGRESS"

        moved = w.act(
            w.hr,
            instance,
            "SETUP",
            "due-date",
            {"due_date": "2026-09-30", "reason": "Equipment delayed"},
        )
        assert moved.status_code == 200 and moved.json()["due_date"] == "2026-09-30"
        assert moved.json()["due_date_source"] == "MANUAL"

        adhoc = w.hr.post(
            f"{w.base}/onboarding/{instance['id']}/tasks",
            json={
                **CHECKLIST,
                "task_code": "BADGE",
                "title": "Issue the access badge",
                "due_offset_days": 3,
            },
            headers={"Idempotency-Key": "7b0c1d3e-0000-4000-8000-0000000000b1"},
        )
        assert adhoc.status_code == 201, adhoc.text
        removed = w.hr.post(
            f"{w.base}/onboarding/{instance['id']}/tasks/{adhoc.json()['id']}/cancel",
            json={"reason": "Badges are issued by the front desk"},
            headers=if_match(adhoc.json()),
        )
        assert removed.status_code == 200 and removed.json()["status"] == "CANCELLED"
        template_task = w.refresh_task(instance, "SETUP")
        refused = w.hr.post(
            f"{w.base}/onboarding/{instance['id']}/tasks/{template_task['id']}/cancel",
            json={"reason": "Trying to remove a template task"},
            headers=if_match(template_task),
        )
        assert refused.status_code == 422  # only ad-hoc tasks can be removed

    def test_only_an_assignee_or_manager_may_act(self, db_session: Any, authenticate: Any) -> None:
        w = make_world(db_session, authenticate)
        w.template("STD")
        instance = w.initiate(w.draft()).json()
        stranger = w.client_for(Role.DEPARTMENT_MANAGER)
        denied = w.act(stranger, instance, "KYC_CHECK", "start")
        assert denied.status_code == 403
        viewer = w.client_for(Role.READ_ONLY_VIEWER)
        assert w.act(viewer, instance, "KYC_CHECK", "start").status_code == 403
