"""Attendance: configuration, punches, records, finalization, corrections (addendum 5)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.core.errors import ErrorCode
from app.core.permissions import Role
from tests.attendance_support import ist, make_attendance_world, new_uuid, past_working_day
from tests.job_architecture import if_match
from tests.leave_support import next_weekday


def _code(response: Any) -> str:
    return str(response.json()["error"]["code"])


class TestShiftsAndPolicies:
    def test_shift_window_rule_and_derived_columns(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        assert w.shift["duration_minutes"] == 540 and w.shift["is_overnight"] is False
        night = w.make_shift(
            "NIGHT",
            start_time="22:00:00",
            end_time="06:00:00",
            break_minutes=30,
            full_day_min_work_minutes=450,
            half_day_min_work_minutes=225,
        )
        assert night["duration_minutes"] == 480 and night["is_overnight"] is True

        too_wide = w.hr.post(
            f"{w.base}/shifts",
            json={
                "shift_code": "WIDE",
                "shift_name": "Too wide",
                "start_time": "09:00:00",
                "end_time": "18:00:00",
                "full_day_min_work_minutes": 480,
                "half_day_min_work_minutes": 240,
                "window_before_minutes": 600,
                "window_after_minutes": 600,
            },
        )
        assert too_wide.status_code == 422
        assert _code(too_wide) == ErrorCode.SHIFT_WINDOW_INVALID.value
        duplicate = w.hr.post(
            f"{w.base}/shifts",
            json={
                "shift_code": "day",
                "shift_name": "Again",
                "start_time": "09:00:00",
                "end_time": "17:00:00",
                "full_day_min_work_minutes": 400,
                "half_day_min_work_minutes": 200,
            },
        )
        assert duplicate.status_code in (409, 422)

    def test_shift_timings_lock_once_a_record_uses_them(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        w.day(employee, past_working_day(), ((9, 0), (18, 0)))
        current = w.hr.get(f"{w.base}/shifts/{w.shift['id']}").json()
        locked = w.hr.patch(
            f"{w.base}/shifts/{w.shift['id']}",
            json={"start_time": "08:00:00"},
            headers=if_match(current),
        )
        assert locked.status_code == 422
        assert _code(locked) == ErrorCode.SHIFT_IN_USE_IMMUTABLE.value
        renamed = w.hr.patch(
            f"{w.base}/shifts/{w.shift['id']}",
            json={"shift_name": "Day shift (HQ)"},
            headers=if_match(current),
        )
        assert renamed.status_code == 200

    def test_policy_publish_rules(self, db_session: Any, authenticate: Any) -> None:
        w = make_attendance_world(db_session, authenticate)
        other = w.make_policy("OTHER", publish=False, default_shift_id=w.shift["id"])
        draft = other["versions"][0]
        conflict = w.hr.post(
            f"{w.base}/attendance-policies/{other['id']}/versions/{draft['id']}/publish",
            headers=if_match(draft),
        )
        assert conflict.status_code == 409
        assert _code(conflict) == ErrorCode.ATTENDANCE_POLICY_CONFLICT.value
        second = w.hr.post(f"{w.base}/attendance-policies/{other['id']}/versions", json={})
        assert _code(second) == ErrorCode.ATTENDANCE_POLICY_DRAFT_EXISTS.value

        flexible = w.make_policy("FLEX", publish=False, priority=50, effective_from="2030-01-01")
        incomplete = flexible["versions"][0]
        refused = w.hr.post(
            f"{w.base}/attendance-policies/{flexible['id']}/versions/{incomplete['id']}/publish",
            headers=if_match(incomplete),
        )
        assert refused.status_code == 422  # no default shift and no flexible thresholds

    def test_a_shift_assignment_pattern_drives_both_attendance_and_leave(
        self, db_session: Any, authenticate: Any
    ) -> None:
        """5.6 step 4 is shared: leave must agree with attendance about weekly offs."""
        w = make_attendance_world(db_session, authenticate)
        w.leave_type_row = w.leave_type("CL")
        w.policy(w.leave_type_row)
        employee = w.employee()
        wednesday_off = w.hr.post(
            f"{w.base}/weekly-off-patterns",
            json={
                "pattern_code": "WED",
                "pattern_name": "Wednesday off",
                "days": [{"day_of_week": 3, "occurrence": "ALL"}],
            },
        ).json()
        wednesday = next_weekday(3, after=14)
        created = w.hr.post(
            f"{w.base}/employees/{employee['id']}/shift-assignments",
            json={
                "weekly_off_pattern_id": wednesday_off["id"],
                "shift_id": w.shift["id"],
                "effective_from": (wednesday - timedelta(days=-0)).isoformat(),
            },
        )
        assert created.status_code == 201, created.text
        resolved = w.hr.post(
            f"{w.base}/attendance-policies/resolve",
            json={"employee_id": employee["id"], "date": wednesday.isoformat()},
        )
        assert resolved.status_code == 200
        assert resolved.json()["day_type"] == "WEEKLY_OFF"
        saturday = wednesday + timedelta(days=3)  # after the assignment starts
        assert (
            w.hr.post(
                f"{w.base}/attendance-policies/resolve",
                json={"employee_id": employee["id"], "date": saturday.isoformat()},
            ).json()["day_type"]
            == "WORKING_DAY"
        )  # the assignment replaces the Sat/Sun pattern

        w.opening(employee, w.leave_type_row, "5.00")
        client = w.self_client(employee)
        preview = client.post(
            f"{w.base}/leave-applications/preview",
            json={
                "leave_type_id": w.leave_type_row["id"],
                "start_date": wednesday.isoformat(),
                "end_date": wednesday.isoformat(),
            },
        )
        assert _code(preview) == ErrorCode.LEAVE_BOUNDARY_ON_NON_WORKING_DAY.value

    def test_assignment_truncates_the_current_one_with_a_warning(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        night = w.make_shift(
            "NIGHT",
            start_time="22:00:00",
            end_time="06:00:00",
            break_minutes=30,
            full_day_min_work_minutes=450,
            half_day_min_work_minutes=225,
        )
        first_day = next_weekday(1, after=5)
        url = f"{w.base}/employees/{employee['id']}/shift-assignments"
        first = w.hr.post(
            url, json={"shift_id": w.shift["id"], "effective_from": first_day.isoformat()}
        )
        assert first.status_code == 201, first.text
        later = (first_day + timedelta(days=14)).isoformat()
        refused = w.hr.post(url, json={"shift_id": night["id"], "effective_from": later})
        assert _code(refused) == ErrorCode.WARNINGS_REQUIRE_ACKNOWLEDGEMENT.value
        accepted = w.hr.post(
            url,
            json={
                "shift_id": night["id"],
                "effective_from": later,
                "acknowledged_warnings": ["W-SHIFT-ASSIGNMENT-TRUNCATES"],
            },
        )
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["boundary_adjustment"]["effective_to_after"] == later
        overlap = w.hr.post(
            url, json={"shift_id": w.shift["id"], "effective_from": first_day.isoformat()}
        )
        assert _code(overlap) == ErrorCode.SHIFT_ASSIGNMENT_OVERLAP.value


class TestPunchesAndEvaluation:
    def test_full_day_is_present_with_lateness_and_break(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        day = past_working_day()
        w.day(employee, day, ((9, 25), (18, 30)))
        record = w.record(employee, day).json()
        assert (record["first_half_status"], record["second_half_status"]) == ("PRESENT", "PRESENT")
        assert (
            record["gross_minutes"],
            record["break_deducted_minutes"],
            record["worked_minutes"],
        ) == (545, 60, 485)
        assert (record["late_by_minutes"], record["is_late"], record["early_by_minutes"]) == (
            25,
            True,
            0,
        )
        assert record["day_status"] == "PRESENT" and record["record_state"] == "OPEN"
        assert [p["punch_status"] for p in record["punches"]] == ["VALID", "VALID"]

    def test_early_exit_is_a_split_day(self, db_session: Any, authenticate: Any) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        day = past_working_day()
        w.day(
            employee, day, ((9, 0), (14, 30))
        )  # gross 330 - 60 = 270: between half (240) and full (480)
        record = w.record(employee, day).json()
        assert (record["first_half_status"], record["second_half_status"]) == ("PRESENT", "ABSENT")
        assert record["day_status"] == "SPLIT" and record["is_early_departure"] is True

    def test_missing_out_keeps_the_day_incomplete(self, db_session: Any, authenticate: Any) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        day = past_working_day()
        assert w.punch(employee, ist(day, 9, 0), "IN").status_code == 200
        record = w.record(employee, day).json()
        assert record["has_missing_punch"] is True and record["day_status"] == "INCOMPLETE"
        assert record["worked_minutes"] is None

    def test_weekly_off_record_and_work_on_it(self, db_session: Any, authenticate: Any) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        saturday = past_working_day(20)
        while saturday.isoweekday() != 6:
            saturday -= timedelta(days=1)
        w.day(employee, saturday, ((10, 0), (14, 0)))
        record = w.record(employee, saturday).json()
        assert record["day_type"] == "WEEKLY_OFF" and record["day_status"] == "WEEKLY_OFF"
        assert record["worked_on_non_working_day"] is True

    def test_duplicate_punches_are_stored_but_not_evaluated(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        day = past_working_day()
        assert w.punch(employee, ist(day, 9, 0), "IN").json()["outcome"] == "CREATED"
        again = w.punch(employee, ist(day, 9, 0).replace(second=30), "IN")
        assert again.status_code == 200
        assert again.json()["outcome"] == "DUPLICATE" and again.json()["duplicate_of"]
        punches = w.record(employee, day).json()["punches"]
        assert sorted(p["punch_status"] for p in punches) == ["DUPLICATE", "VALID"]

    def test_future_manual_punch_is_refused(self, db_session: Any, authenticate: Any) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        tomorrow = past_working_day(-3)
        response = w.punch(employee, ist(tomorrow, 9, 0), "IN")
        assert response.status_code == 422
        assert _code(response) == ErrorCode.PUNCH_TIME_INVALID.value

    def test_ineligible_dates_are_refused(self, db_session: Any, authenticate: Any) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()  # joined 2026-01-05
        before_joining = ist(past_working_day(300), 9, 0)
        response = w.punch(employee, before_joining, "IN")
        assert response.status_code == 422
        assert _code(response) in (
            ErrorCode.EMPLOYEE_NOT_ELIGIBLE_FOR_ATTENDANCE.value,
            ErrorCode.NO_APPLICABLE_ATTENDANCE_POLICY.value,
        )

    def test_manual_punch_needs_remarks_and_permission(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        day = past_working_day()
        short = w.punch(employee, ist(day, 9, 0), "IN", remarks="short")
        assert short.status_code == 422
        manager = w.manager_client(w.employee(first_name="Mira"))
        denied = w.punch(employee, ist(day, 9, 0), "IN", client=manager)
        assert denied.status_code == 403

    def test_self_punch_uses_server_time_and_the_linked_employee(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        client = w.self_client(employee)
        rejected = client.post(
            f"{w.base}/me/attendance/punches",
            json={"punch_type": "IN", "punch_at": "2020-01-01T00:00:00Z"},
        )
        assert rejected.status_code == 422  # client time is not accepted
        response = client.post(
            f"{w.base}/me/attendance/punches",
            json={"punch_type": "IN"},
            headers={"Idempotency-Key": new_uuid()},
        )
        assert response.status_code == 200, response.text
        assert response.json()["punch"]["employee_id"] == employee["id"]
        assert response.json()["punch"]["source"] == "SELF_SERVICE"

    def test_self_punch_is_gated_by_flags_and_policy(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        client = w.self_client(employee)
        w._settings(enable_attendance_self_service=False)
        off = client.post(f"{w.base}/me/attendance/punches", json={"punch_type": "IN"})
        assert off.status_code == 403
        assert _code(off) == ErrorCode.SELF_SERVICE_NOT_ENABLED.value

        w2 = make_attendance_world(db_session, authenticate, self_punch_enabled=False)
        client2 = w2.self_client(w2.employee())
        disabled = client2.post(f"{w2.base}/me/attendance/punches", json={"punch_type": "IN"})
        assert disabled.status_code == 422
        assert _code(disabled) == ErrorCode.ATTENDANCE_SOURCE_DISABLED.value

    def test_employee_cannot_read_a_colleagues_attendance(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        day = past_working_day()
        w.day(employee, day, ((9, 0), (18, 0)))
        stranger = w.self_client(w.employee(first_name="Sam"))
        assert w.record(employee, day, client=stranger).status_code == 403  # no attendance.view
        own = w.self_client(employee)
        mine = own.get(f"{w.base}/me/attendance/{day}")
        assert mine.status_code == 200 and mine.json()["remarks"] is None

    def test_cross_organization_access_is_hidden(self, db_session: Any, authenticate: Any) -> None:
        w1 = make_attendance_world(db_session, authenticate)
        w2 = make_attendance_world(db_session, authenticate)
        employee = w1.employee()
        day = past_working_day()
        w1.day(employee, day, ((9, 0), (18, 0)))
        assert (
            w2.hr.get(f"{w1.base}/employees/{employee['id']}/attendance/{day}").status_code == 403
        )
        assert w2.record(employee, day).status_code == 404


class TestIntegrationImport:
    def test_import_creates_replays_conflicts_and_isolates_failures(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        w.add_source()
        employee = w.employee()
        client, _ = w.client_for(None, Role.ATTENDANCE_INTEGRATION_CLIENT)
        day = past_working_day()

        def item(event: str, hour: int, source: str = "ACCESS_CTRL_1") -> dict[str, Any]:
            return {
                "employee_id": employee["id"],
                "punch_at": ist(day, hour).isoformat(),
                "punch_type": "IN" if hour < 12 else "OUT",
                "integration_source_code": source,
                "external_event_id": event,
            }

        first = client.post(
            f"{w.base}/attendance/punch-imports", json={"items": [item("e1", 9), item("e2", 18)]}
        )
        assert first.status_code == 200, first.text
        assert [r["outcome"] for r in first.json()["results"]] == ["CREATED", "CREATED"]

        mixed = client.post(
            f"{w.base}/attendance/punch-imports",
            json={"items": [item("e1", 9), item("e2", 17), item("e3", 10, source="UNKNOWN")]},
        )
        outcomes = mixed.json()["results"]
        assert outcomes[0]["outcome"] == "REPLAYED"
        assert outcomes[1]["outcome"] == "REJECTED"
        assert outcomes[1]["error_code"] == ErrorCode.CONFLICTING_ATTENDANCE_EVENT.value
        assert outcomes[2]["error_code"] == ErrorCode.ATTENDANCE_SOURCE_DISABLED.value
        punches = w.record(employee, day).json()["punches"]
        assert len(punches) == 2  # the replay and the rejections wrote nothing

    def test_only_the_integration_role_may_import(self, db_session: Any, authenticate: Any) -> None:
        w = make_attendance_world(db_session, authenticate)
        w.add_source()
        employee = w.employee()
        body = {
            "items": [
                {
                    "employee_id": employee["id"],
                    "punch_at": ist(past_working_day(), 9).isoformat(),
                    "punch_type": "IN",
                    "integration_source_code": "ACCESS_CTRL_1",
                    "external_event_id": "x1",
                }
            ]
        }
        denied = w.hr.post(f"{w.base}/attendance/punch-imports", json=body)
        assert denied.status_code == 403


class TestFinalizeAndCorrect:
    def test_finalized_records_protect_themselves_ac42(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        w.add_source()
        employee = w.employee()
        day = past_working_day()
        w.day(employee, day, ((9, 0), (18, 0)))
        done = w.finalize(employee, day)
        assert done.status_code == 200 and done.json()["finalized_count"] == 1, done.text

        manual = w.punch(employee, ist(day, 19, 0), "OUT")
        assert manual.status_code == 422
        assert _code(manual) == ErrorCode.ATTENDANCE_RECORD_FINALIZED.value

        client, _ = w.client_for(None, Role.ATTENDANCE_INTEGRATION_CLIENT)
        held = client.post(
            f"{w.base}/attendance/punch-imports",
            json={
                "items": [
                    {
                        "employee_id": employee["id"],
                        "punch_at": ist(day, 19, 30).isoformat(),
                        "punch_type": "OUT",
                        "integration_source_code": "ACCESS_CTRL_1",
                        "external_event_id": "late-1",
                    }
                ]
            },
        )
        assert held.json()["results"][0]["outcome"] == "HELD"
        record = w.record(employee, day).json()
        assert record["has_held_punch"] is True and record["record_state"] == "FINALIZED"

        hre, _ = w.client_for(None, Role.HR_EXECUTIVE)
        denied = w.correct(
            employee,
            day,
            [{"set_override": {"half": "SECOND_HALF", "status": "ABSENT"}}],
            client=hre,
        )
        assert denied.status_code == 403

        corrected = w.correct(
            employee, day, [{"set_override": {"half": "SECOND_HALF", "status": "ABSENT"}}]
        )
        assert corrected.status_code == 200, corrected.text
        data = corrected.json()
        assert data["record_state"] == "FINALIZED" and data["post_finalization_change"] is True
        assert data["second_half_status"] == "ABSENT" and data["day_status"] == "SPLIT"
        revisions = w.hr.get(
            f"{w.base}/employees/{employee['id']}/attendance/{day}/revisions"
        ).json()["items"]
        assert [r["change_source"] for r in revisions] == [
            "MANUAL_PUNCH",
            "MANUAL_PUNCH",
            "FINALIZED",
            "ADMIN_CORRECTION",
        ]
        assert revisions[3]["before_snapshot"]["second_half_status"] == "PRESENT"
        assert revisions[3]["after_snapshot"]["second_half_status"] == "ABSENT"

    def test_correction_voids_punches_and_applies_held_ones(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        day = past_working_day()
        w.day(employee, day, ((9, 0), (14, 0)))
        record = w.record(employee, day).json()
        wrong_out = next(p for p in record["punches"] if p["punch_type"] == "OUT")
        fixed = w.correct(
            employee,
            day,
            [
                {"void_punch_id": wrong_out["id"]},
                {"add_punch": {"punch_at": ist(day, 18, 0).isoformat(), "punch_type": "OUT"}},
            ],
        )
        assert fixed.status_code == 200, fixed.text
        detail = w.record(employee, day).json()
        assert detail["day_status"] == "PRESENT"
        assert sorted(p["punch_status"] for p in detail["punches"]) == ["VALID", "VALID", "VOIDED"]

    def test_finalize_skips_incomplete_days_and_reopen_works(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        day = past_working_day()
        w.punch(employee, ist(day, 9, 0), "IN")
        skipped = w.finalize(employee, day)
        assert skipped.json()["finalized_count"] == 0
        assert skipped.json()["skipped"][0]["reason"] == "INCOMPLETE"

        other = past_working_day(14)
        w.day(employee, other, ((9, 0), (18, 0)))
        assert w.finalize(employee, other).json()["finalized_count"] == 1
        record = w.record(employee, other).json()
        reopened = w.hr.post(
            f"{w.base}/employees/{employee['id']}/attendance/{other}/reopen",
            json={"reason": "Reopened to add the evening punch"},
            headers=if_match(record),
        )
        assert reopened.status_code == 200 and reopened.json()["record_state"] == "OPEN"
        again = w.hr.post(
            f"{w.base}/employees/{employee['id']}/attendance/{other}/reopen",
            json={"reason": "Reopened to add the evening punch"},
            headers=if_match(reopened.json()),
        )
        assert _code(again) == ErrorCode.ATTENDANCE_RECORD_NOT_FINALIZED.value

    def test_an_hr_admin_cannot_correct_their_own_record(
        self, db_session: Any, authenticate: Any
    ) -> None:
        w = make_attendance_world(db_session, authenticate)
        employee = w.employee()
        day = past_working_day()
        w.day(employee, day, ((9, 0), (18, 0)))
        own_hr, _ = w.client_for(employee["id"], Role.HR_ADMIN)
        response = w.correct(
            employee,
            day,
            [{"set_override": {"half": "FIRST_HALF", "status": "ABSENT"}}],
            client=own_hr,
        )
        assert response.status_code == 403
        assert _code(response) == ErrorCode.SELF_CORRECTION_NOT_ALLOWED.value
