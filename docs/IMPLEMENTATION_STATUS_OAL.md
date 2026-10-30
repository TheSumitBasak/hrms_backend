# Onboarding / Attendance / Leave addendum — implementation status

Spec: `ONBOARDING_ATTENDANCE_AND_LEAVE.md` (read with offset/limit; 2,915 lines). Base: `EMPLOYEE_MANAGEMENT_SYSTEM.md`.
Updated: 2026-10-29. Only items with a test that actually ran are marked **done**.

## Migration chain

`c4a71e5d9f08` (JA permissions) → `b7e31f4a9c10` foundation → `c8f42a5bd121` onboarding (**empty stub**) →
`d9a53b6ce232` attendance (stage 1) → `eab64c7df343` leave (10 tables) → `a3d91c7e5b42` attendance core (7 tables) → `b5e82d1f7a63` regularization (3 tables) → `c6f93e2a8b74` **onboarding (12 tables)**.
Round trip (upgrade → downgrade → upgrade) verified on `ems_test`; `alembic check` shows no leave drift.

## Done (tests run)

| Area | What | Evidence |
|---|---|---|
| §9, §10 | 15 org-settings columns, `ATTENDANCE_SOURCE` list, roles OTO/AIC, 34 permission codes | `test_permissions.py`, `test_bootstrap.py`, `test_migrations.py` |
| §16 | 77 error codes, 8 warnings, 83 audit actions | `test_errors_and_warnings.py` |
| §5.2 | Weekly-off patterns, holiday calendars, holidays, branch assignments | `test_schedule_rules.py`, `test_schedule.py` |
| §6.1, 6.6, 6.9 | Leave-year, rounding R(x), accrual, upfront proration, duration (pure rules) | `tests/unit/test_leave_rules.py` (spec worked examples) |
| §6.2-6.4 | Leave types, policies, versions, publish (LEV-05/06 under parent lock), resolution | `integration/test_leave_config_ledger.py` |
| §6.5, 6.13 | Ledger, materialized balances, opening balance, adjustment, reversal, idempotency, self-adjustment block | same |
| §6.8-6.12, §7.1 | Preview, submit (self / on behalf), reserve, approve (1- and 2-level, HR override), reject, withdraw, tail/full cancel + restoration, reassign, recalculate; approval routing in `services/approvals.py` | `integration/test_leave_applications.py` (25 tests) |
| AC-45 etc. | Concurrent submissions never over-reserve; duplicate dates → one winner; idempotency replay; approve once; cancel restores once (real threads, MySQL) | `tests/concurrency/test_leave.py` (5) |
| §6.6, 6.7, §14 | LEAVE_ACCRUAL (AC-58 example, cap, upfront), LEAVE_YEAR_END (carry/lapse, defer, idempotent), `background_job_runs`, manual `POST /leave-processing/runs`, scheduled via `app.cli run-jobs` | `integration/test_leave_jobs.py` (7) |
| §5.3-5.13 | Shifts, attendance policies/versions (publish, conflict, resolve), shift assignments (truncation warning), punches (self / manual / integration with replay and duplicate detection), records with 5.10 evaluation, revisions, finalize / reopen, administrative correction | `unit/test_attendance_rules.py` (22), `integration/test_attendance.py` (23) |
| §5.6 | One `ScheduleResolver` for leave and attendance (shift-assignment pattern, policy default, organization default; cached) | `test_attendance.py::..._drives_both_attendance_and_leave` |
| §5.14 | Regularization: submit (self / on behalf), approve (1- and 2-level, HR override), reject, withdraw, reassign; ATT-20..23; AC-40, AC-41 | `integration/test_attendance_regularization.py` (6) |
| §5.12, §14 | ATTENDANCE_DAY_CLOSE (7-day catch-up, per employee), ATTENDANCE_AUTO_FINALIZE, manual day close; scheduled by `run-jobs` | `integration/test_attendance_jobs.py` (4) |
| §7.4 | AC-42, AC-43; LEV-30 warning and LEV-31 conflict at submission and approval; INT-10 on approval, INT-11 on record creation, INT-12 on cancellation | `integration/test_attendance_leave.py` (6) |
| §5.7 races | Duplicate punches, replayed imports, parallel manual punches | `concurrency/test_attendance.py` (3) |
| §11 | `/me/leave-*` self-service: identity from user link, `SELF_SERVICE_NOT_ENABLED` gate | applications tests |

## Onboarding (migration `c6f93e2a8b74`, 12 tables)

Done with tests that ran: templates and versions, instances and tasks, activation gating, document
tasks, acknowledgements, CONTACT / PERSONAL / EDUCATION / FAMILY submissions, `/me/onboarding*`,
lifecycle hooks (AC-29..AC-34, AC-50, AC-51). Not built: see STATUS section 0d.

## Known gaps (leave)

- Attendance integration is wired for existing records (INT-10..13, LEV-30, LEV-31). Remaining `shortcut:` items: the pending ON_DUTY regularization check in LEV-31 (needs regularization) and INT-22 flagging on holiday changes.
- Policy resolution uses the employee's **current** attributes, not job history as of the date (§6.4); `shortcut:` in `leave_config.py`.
- Not built: team `leave-calendar`, `leave-summary`, `leave-year-closures` list, `GET /me/holidays`, supporting-document upload paths, LEV-03 enforcement on settings change, INT-22 `schedule_review_required` flagging on holiday changes, INT-36 manager-change re-routing, §7.5 lifecycle hooks (employment end / archive cancels pending leave), integrity-checker extensions (LEV-INV-1/2), AC-56 reconstruction test, applicability-master ACTIVE check at publish.
- `ATTENDANCE_*` job codes exist in the enum only.
- Reasons in application lists are shown to the owner and `leave.approve_hr` only (past approvers see them on the detail view).
- Known flake: occasional `401 UNAUTHENTICATED` / `test_bootstrap` under long full-suite runs (cause not found).

## Known gaps (attendance)

- **ATTENDANCE_RECALCULATION (INT-21)** and the holiday / calendar-assignment change flow (INT-20..22).
- Regularization: supporting-document ownership/category check; system cancellation of SUBMITTED requests on employment end (7.5); list has no cursor.
- Not built: organization-wide `GET /attendance`, `GET /attendance/summary`, bulk assignment, assignment PATCH, ATT-10 re-derivation for backdated assignments, AC-55 reconstruction test, ATT-01 handling of employment-end side effects (SUBMITTED regularizations).
- Policy resolution uses current employee attributes (DEC-OAL-7).
- Parallel pytest sessions on `ems_test` collide (deadlocks, "not seeded"); run one session at a time.

## Not started

- Onboarding extras listed in STATUS 0d (access provisioning, reveal, retention job, four submission sections).
- Cross-module lifecycle hooks (§7.5), NFR tests, the AC-29.. acceptance suite, frontend.

## Deployment

`alembic upgrade head`; schedule `python -m app.cli run-jobs` daily (or run `serve-jobs`). For attendance also set `enable_attendance_module`, `attendance_tracking_start_date`, `enable_attendance_self_service`, the configurable `self.attendance` SELF permission, a shift, and a published attendance policy; add `ATTENDANCE_SOURCE` lookup values for integration clients.
Enable per organization:
`enable_leave_module`, `leave_tracking_start_date`, optionally `enable_leave_self_service`, and the configurable `self.leave` SELF permission.
