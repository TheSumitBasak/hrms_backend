# BACKEND_GAP_REMEDIATION.md
# HRMS Backend (FastAPI): API Gap and Contract Remediation Plan

| Item | Value |
|---|---|
| Document | BACKEND_GAP_REMEDIATION.md |
| Version | 1.0 |
| Date | 2026-10-30 |
| Inputs | Frontend limitations report; HRMS_FRONTEND_IMPLEMENTATION.md v1.1 §25 ("spec"); ONBOARDING_ATTENDANCE_AND_LEAVE.md v1.0 ("ADD"); EMPLOYEE_MANAGEMENT_SYSTEM.md ("EMS"); `openapi.json` (359 operations) |
| Companion | FRONTEND_GAP_REMEDIATION.md (items `FE-xx`) |
| Stack | FastAPI, SQLAlchemy, MySQL (with the ADD §8.7 compensating controls) |
| Goal | Make the OpenAPI file a complete, truthful contract. Implement every endpoint the specs require but the API lacks. Resolve every unclear endpoint. |

---

## 0. Summary

The frontend's limitations have three backend causes:

1. **The contract is incomplete or undeclared.** The error envelope, ETag format, Idempotency-Key, enums, permission codes, master table names, JSON sub-schemas and PATCH null semantics are missing from the OpenAPI file, so the frontend guesses (§2).
2. **Endpoints specified in ADD and EMS were never implemented.** These include the org-wide attendance list, attendance summary, leave calendar, leave summaries, year-end closures, onboarding summary, bulk shift assignment, self-service access, submission values, self uploads, `/me/holidays` and self profile. **Most already have full specifications in ADD §4.14, §5.16, §6.15 and §11. Implement them as written.** This document adds only what ADD leaves open (§4).
3. **Read models for UX are missing.** Embedded names, counts, uniform pagination, filters, sorting, version lists and single GETs (§3).

All changes are **additive within `/api/v1`**, except the error-envelope unification (BE-C01), which the frontend already parses in both shapes (spec §20.3).

---

## 1. Principles

1. **OpenAPI is the deliverable.** An endpoint is done only when its schema, enums, headers, error responses and permission are declared and covered by contract tests.
2. **The specs are the source of behaviour.** Where ADD or EMS define an endpoint, implement it exactly. Deviations are recorded as decisions (§9), never made silently.
3. **Nothing in this work weakens the existing guarantees:** tenant isolation, field-level filtering, audit in the same transaction, lock order (ADD §7.3), idempotency and the computation guardrails (ADD §2.5).
4. **Every new capability is discoverable** through `GET /api/v1/meta` (BE-S09), so the frontend can ship ahead and switch on automatically.

---

## 2. Contract hygiene (Phase B0; unblocks the frontend foundations)

### BE-C01 Declared error envelope

- Register global exception handlers so every error, including `RequestValidationError`, `HTTPException` and domain errors, returns the EMS §24.2 envelope:

```json
{ "error": { "code": "VALIDATION_FAILED", "message": "…",
  "details": [ { "field": "components[2].value_amount", "code": "TOO_LARGE", "message": "…" } ],
  "warnings": [], "correlation_id": "…" } }
```

- `details[].field` is built from FastAPI `loc`: drop the leading `body` or `query`, join keys with `.`, and write indices as `[n]`.
- Declare `ErrorResponse` and `WarningItem` component schemas. Attach them to **every** operation for 400/401/403/404/409/412/422/428/429 using a shared `responses=` helper.
- `correlation_id` echoes `X-Request-Id`, or a generated value if none was sent.
- `429` responses include `Retry-After`.

### BE-C02 ETag and If-Match everywhere

- Every single-resource GET and every mutation response returns `ETag: "<row_version>"`. **This format is the documented contract**, so clients may derive it from `row_version` on list items.
- Every PATCH, PUT and state action on an existing resource declares the `If-Match` header as required. A missing header returns `428 PRECONDITION_REQUIRED`; a stale one returns `412 PRECONDITION_FAILED` with the current `row_version` in `details`.
- The bulk endpoints (finalize, bulk shift assignment, bulk decisions) are exempt and use per-row checks (ADD §12.1).
- Implement it once as a dependency: `require_if_match(model, id)` compares the header against `row_version` inside the same `SELECT … FOR UPDATE`.

### BE-C03 Idempotency-Key declared and enforced on every create

- Declare the header on every create and state-creating POST, including those the spec lists as undeclared (CF-26): employees, salary assignments, documents, and the ADD §12.1 list.
- Use one middleware or dependency. Store `(organization_id, user_id, route, key, body_sha256, response_status, response_body)` for 24 hours in table `idempotency_records`, with a unique key on `(user_id, route, key)`. A replay with the same hash returns the stored response; a different hash returns `409 IDEMPOTENCY_KEY_REUSED`.

### BE-C04 Enums declared in OpenAPI

Define a Python `Enum` for each of the values below and use it in the Pydantic models, so OpenAPI emits `enum`. **Values the frontend currently guesses:**

| Domain | Enum |
|---|---|
| Salary | `assignment_status`, `approval_state` |
| Jobs | `JobRunStatus` (RUNNING, SUCCEEDED, PARTIALLY_FAILED, FAILED; ADD §14.2) |
| Versions | `VersionStatus` (DRAFT, ACTIVE, ARCHIVED, plus SCHEDULED and EXPIRED for ID rules). Confirm whether `PUBLISHED` exists; if it does, document it. |
| Documents | `upload_status`, `verification_status`, `expiry_state` |
| Requests | Leave, regularization, onboarding instance, task and submission statuses; `current_approver_type`; slot status |
| Attendance | Day status, half status, half source, punch status, punch source, `work_arrangement` |
| Others | `ScopeType`, `RestorationMode`, `TaskType`, `OwnerType`, `SectionCode`, `JobCode` |

### BE-C05 Permission catalogue

- Declare `PermissionCode` as an enum. `GET /permissions` returns `{code, module, description, configurable, platform_only}`.
- Add the codes the frontend lacks or misuses, and seed grants with a migration:

| New code | Default grants | Replaces |
|---|---|---|
| `privacy.data_export`, `privacy.legal_hold`, `privacy.erasure_request`, `privacy.erasure_decide`, `privacy.notice_manage` | HRA | The frontend's use of `employee.archive` for privacy actions |
| `retention.manage`, `retention.approve` | HRA / OA (two-person rule, BE-X04) | Undeclared |
| `attendance.import_upload` | HRA, HRE | HR punch file import (BE-A07) |
| `salary.revision_approve` | (exists) | Confirm the name; used by BE-SAL02 |
| `export.run` | HRA (decision-gated, BE-X02) | — |

- Confirm and document the exact `self.*`, `leave.*` and `attendance.*` codes against ADD §10.2. Rename any code that differs from ADD in the code, not in the documentation.

### BE-C06 Master table and lookup names

- Declare `MasterTable` as a path-parameter enum on `/masters/{table_name}` with the values actually implemented. The recommended canonical form is snake_case table names from EMS §9.2. Any other value returns 404. This removes the frontend's underscore/hyphen fallback.
- Declare `LookupListCode` as an enum (spec §24.0 list plus `ATTENDANCE_SOURCE`, `JOB_CHANGE_TYPE`, `STATUS_REASON` and so on).

### BE-C07 PATCH semantics: omitted vs null

- Use JSON Merge Patch semantics: an **omitted field is unchanged and `null` clears it**. Implement with `model_dump(exclude_unset=True)`.
- Nullable optional fields are declared `nullable: true`. Sending `null` for a required field returns `422 FIELD_NOT_NULLABLE`.
- Audit before/after values record the clear as `null`.
- Apply this to every PATCH (employee, rows, masters, org, branch, bank, policies and so on).

### BE-C08 Typed schemas for free-form JSON

Replace untyped objects with named schemas:

| Field | Schema |
|---|---|
| Salary component fields and template version component mappings | `SalaryComponentResponse`, `TemplateComponentMapping` (value type, amount, period, percentage reference, override flags) |
| `boundary_adjustments` (preview and shift assignment) | `BoundaryAdjustment {entity_type, entity_id, field, before, after, reason_code}` |
| `employee_id_format_settings` | Publish a schema, or deprecate the field if unused (§5) |
| Revision `before_snapshot`/`after_snapshot` | `AttendanceRecordSnapshot` (ADD §5.13) |
| `field_comments`, `applied_entity_refs` | ADD §4.10 shapes |

### BE-C09 Embedded summaries for names

- Every response item that carries `employee_id` adds `employee: EmployeeSummaryRef {id, employee_code, display_name, department_name, designation_name, status}`. This applies to leave applications, regularizations, tasks, attendance records, onboarding instances, transactions and audit rows.
- **The summary is filtered by the caller's visibility.** If the caller cannot see the employee, return `{id, restricted: true}`.
- Every `*_user_id` (approver, actor, coordinator, assignee, decided_by) adds `UserRef {id, display_name}`. Display names of users in the same organization are visible to any granted user.
- Add `id` (repeatable, ≤ 100) as a filter on `GET Org/employees` for batch lookup.
- **Performance:** load with a single `IN` query per page, never one query per row.

### BE-C10 Uniform pagination

- Every list that can grow unboundedly accepts `limit` (1–100, default 25), `cursor` (signed and opaque) and `with_total`, and returns `{items, next_cursor, total_count}` (EMS §26.5).
- **Convert every Shape C endpoint** (spec §12): onboarding, onboarding tasks, onboarding templates, employee attendance, punches, regularizations, processing runs, holidays, leave policies, shifts, attendance policies, leave types, weekly-off patterns, holiday calendars. Adding the parameters is non-breaking.
- **Audit logs** (`Org/audit-logs`, `Emp/audit`, and the new platform audit) become paginated, with `from`/`to` required and a maximum range of 366 days.
- Small configuration lists (masters, lookups, branches) may stay as plain arrays (Shape A). Document which shape each endpoint uses.

### BE-C11 Sorting

- One syntax everywhere: `sort=field` for ascending, `sort=-field` for descending. Allowed fields are declared as an enum per endpoint; anything else returns `422 VALIDATION_FAILED`. The tie-breaker is `id`.
- At minimum: employees (`display_name`, `employee_code`, `joining_date`, `status`, `created_at`); leave applications (`submitted_at`, `start_date`); onboarding (ADD §4.14 list); tasks (`due_date`); regularizations (`submitted_at`, `business_date`); attendance (ADD §5.16).

### BE-C12 Filters dropped from the ADD-specified lists

| Endpoint | Add |
|---|---|
| `GET Org/employees` | `sub_department_id[]`, `id[]` |
| `GET Org/onboarding` | `phase[]`, `template_id`, `hr_coordinator_user_id`, `overdue_only`, `joining_date_from/to`, `department_id[]`, `branch_id[]`, `sort` (ADD §4.14) |
| `GET Org/onboarding-tasks` | `scope` (§5), `assigned_to=me`, `queue=mine`, `phase[]`, `overdue_only`, `employee_id` |
| `GET Org/attendance/regularizations` | `from`, `to`, `stale_approver_only`, `sort` |
| `GET Org/leave-applications` | `schedule_review_required`, `stale_approver_only`, `sort` |
| `GET Org/leave-processing/runs` | `status[]` |
| `GET Org/documents/expiring` | `category_id[]` |
| `GET /users` | `q`, `status`, `organization_id`, `role_code`, `cursor` |
| `GET Org/onboarding-templates`, configuration lists | `q` |

---

## 3. Session, context, identity and platform (Phases B0–B1)

### BE-S01 `GET Org/me/context`, readable by any user with a grant in the org (including SELF)

```json
{
  "organization": { "id": "…", "code": "ABC", "display_name": "ABC Tech", "status": "ACTIVE",
    "timezone": "Asia/Kolkata", "default_currency": "INR", "financial_year_start_month": 4,
    "date_display_format": "DD-MM-YYYY" },
  "modules": { "onboarding": true, "attendance": true, "leave": false },
  "self_service": { "onboarding": true, "attendance": true, "leave": false,
    "employee_self_view": true, "self_view_salary": false },
  "leave": { "leave_year_start_month": 1, "leave_tracking_start_date": "2027-01-01" },
  "attendance": { "attendance_tracking_start_date": "2026-11-01", "day_close_time": "12:00" },
  "documents": { "expiry_warning_days": 30 },
  "me": { "employee_id": "…", "employee_status": "ACTIVE" }
}
```

This closes FQ-02, CF-13 and report §2.1 (modules, dates and time zones). It contains no admin-only settings.

### BE-S02 Settings completeness

- `GET`/`PUT Org/settings` include every ADD §9.1 column: the three module flags, the tracking start dates, `leave_year_start_month`, `default_weekly_off_pattern_id`, `attendance_day_close_time`, the onboarding settings and the three self-service flags. Add any DB columns that are missing.
- Declare the permission (`org.manage` for read and write).
- Enforce ADD rules: LEV-03 (`LEAVE_YEAR_CHANGE_NOT_ALLOWED`) and the immutability of tracking start dates.

### BE-S03 Auth additions

| Change | Detail |
|---|---|
| `auth/me.is_platform_admin` | Boolean. Replaces role-name matching in the frontend. |
| `auth/me.session` | `{idle_timeout_seconds, absolute_timeout_seconds, access_token_ttl_seconds}` (EMS: 1800 / 43200 / ≤ 900) |
| Password change | `POST /auth/password/change {current_password, new_password}`. Revokes other sessions. |
| Admin password reset | `POST /users/{uid}/password-reset {reason}` (`user.manage`, local accounts only). Returns a one-time temporary password in the response, because there is no email system (ADD §2.4). Sets `must_change_password`. Audited without the value; responds with `Cache-Control: no-store`. |
| `must_change_password` | Login returns `PASSWORD_CHANGE_REQUIRED`, and the token is restricted to `/auth/password/change` |
| OIDC (decision-gated, FQ) | `GET /auth/oidc/{provider}/authorize`, `GET /auth/oidc/{provider}/callback` → the same token pair. Scheduled only after security sign-off. |

### BE-S04 Break-glass early revoke and listing

`POST /break-glass/{grant_id}/revoke {reason ≥ 10}` (SSA, owner or any SSA). Effective on the next request, audited `BREAK_GLASS_REVOKED`. Also `GET /break-glass?active=true`.

### BE-S05 Users

| Endpoint | Purpose | Permission |
|---|---|---|
| `GET /users/{uid}` | Single GET | `user.manage` |
| `POST /users/{uid}/reactivate {reason}` | Reactivation; restores the account, not the grants | `user.manage` |
| `GET /users?q&status&organization_id&role_code&cursor` | Filtered list | `user.manage` |
| `GET Org/user-directory?q&id[]&has_permission&department_id&limit` | **Picker data** `{id, display_name, linked_employee: EmployeeSummaryRef?}`. With `has_permission`, returns only valid assignees or approvers. | Any of `onboarding.manage`, `onboarding.template_manage`, `leave.approve_hr`, `attendance.regularize_approve_hr`, `user.manage` |

### BE-S06 Organization groups in summaries

`OrganizationSummary` (in `GET /orgs` and `auth/me.organizations[]`) gains `group_id` and `group_name` for orgs the caller can access. `GET /orgs?q=` is usable by any authenticated user and is scoped to their orgs.

### BE-S07 Platform audit log

`GET /audit-logs?from&to&actor&action&organization_id&cursor` (SSA). Covers platform events and break-glass.

### BE-S08 Organization and branch edits

- `PATCH /orgs/{id}` accepts address objects (registered, correspondence) with BE-C07 semantics.
- Branch statutory numbers remain create-only (spec). Document that edits go through Registrations. If product requires direct edit, add these fields to `PATCH Org/branches/{bid}` with an audit reason.

### BE-S09 `GET /api/v1/meta`

Returns `{api_version, build_sha, capabilities: string[]}`. Each new feature in this document registers a capability string, for example `attendance.org_list`, `leave.calendar` or `onboarding.submission_values`. This is the frontend's switch for Phase 3 pages.

---

## 4. Missing endpoints by module (Phases B1–B3)

Rules for every new endpoint: org-scoped four checks, field filtering, RLS or repository filter, audit in the same transaction, module flag check (`403 MODULE_NOT_ENABLED`), BE-C01 to C03 headers, and BE-C09/C10 response shapes.

### 4.1 Attendance

| ID | Endpoint | Specification | Notes beyond ADD |
|---|---|---|---|
| BE-A01 | `GET Org/attendance` | ADD §5.16 row "Organization or team list" | Uses the partial index `(organization_id, business_date, day_status)`. DM scope applied in SQL. Range ≤ 31 days. |
| BE-A02 | `GET Org/attendance/summary` and `GET Org/me/attendance/summary` | ADD §5.16, §11 | Half-day units and minutes only. **No "payable" or "paid" labels** (AC-60). Aggregate in SQL by (employee, status) for the range. |
| BE-A03 | `POST Org/attendance/finalize` accepting filters | ADD §5.12: `{employee_ids? \| department_id[], branch_id[], employee filters; from, to ≤ 31 days}` | ≤ 500 matching employees → synchronous, one transaction per record. More → `202` with a job run (`ATTENDANCE_FINALIZE_BULK`). Results are `finalized_count` and `skipped[] {record_id, reason}`. The per-employee shape stays valid. |
| BE-A04 | `POST Org/shift-assignments/bulk` | ADD §5.16, ATT-11 | ≤ 500 employees, all-or-nothing, locks in ascending id order, warnings protocol for truncation |
| BE-A05 | `GET Org/attendance/day-close-runs`; `GET Org/job-runs?job_code&run_key&status&cursor`; `GET Org/job-runs/{id}` | ADD §14.2 `background_job_runs` | Makes `HolidayResponse.recalculation_run_id` readable. Permission: the job's own permission (`attendance.correct`, `leave.process_run`). `failure_details` contains no personal data. |
| BE-A06 | Regularization filters | BE-C12 | — |
| BE-A07 | `POST Org/attendance/punch-imports/manual` | New. `{batch_remarks ≥ 10, items[] ≤ 1000 {employee_code \| employee_id, punch_at, punch_type, work_arrangement?}}` | Permission `attendance.import_upload`. Creates `source = MANUAL` punches with `remarks = batch_remarks`. Each item runs in its own transaction with ATT-13/15/16 rules. Per-item outcomes as in the integration import. Audit `ATTENDANCE_IMPORT_BATCH` with counts. Requires `manual_punch_enabled`. The integration endpoint stays AIC-only. |
| BE-A08 | `POST Org/me/attendance/evidence` (+ `/{docId}/complete`) | ADD §11 | Category fixed to `ATTENDANCE_EVIDENCE`. Only for the caller's own record dates. Same EMS §15.4 flow and scanning. |

### 4.2 Leave

| ID | Endpoint | Specification | Notes |
|---|---|---|---|
| BE-L01 | `GET Org/leave-calendar` | ADD §6.15 | Range ≤ 92 days. The label rule (type name vs "On leave") is evaluated server-side. No reasons or documents. Includes holidays. |
| BE-L02 | `GET Org/leave-summary`, `GET Org/employees/{id}/leave-summary` | ADD §6.15 | Days only. Reads `leave_balances`; no recomputation. |
| BE-L03 | `GET Org/leave-year-closures` | ADD §6.15 | Includes `deferred_reason` and blocking application IDs |
| BE-L04 | Application filters and sort | BE-C11, C12 | `stale_approver_only` is derived per ADD INT-37 |
| BE-L05 | `POST Org/me/leave/supporting-documents` (+ complete) | ADD §11 | Category fixed to `LEAVE_SUPPORTING_DOCUMENT` (restricted) |
| BE-L06 | `GET Org/me/holidays?from&to` | ADD §11 | Resolved per ADD §5.6. Optional holidays include `already_taken` and the remaining optional-holiday balance for the year. |

### 4.3 Onboarding

| ID | Endpoint | Specification | Notes |
|---|---|---|---|
| BE-O01 | `GET Org/onboarding/summary` | ADD §4.14 | Counts by status and phase, overdue tasks, blocked activations; same filters as the list |
| BE-O02 | List and task filters and sort | BE-C11, C12 | — |
| BE-O03 | `POST`/`GET`/`DELETE Org/employees/{id}/self-service-access[/{grantId}]` | ADD §4.14 | Grants SELF only; revocation effective within 60 s. Credentials are not sent by the system (OD-02); an admin-set temporary password follows BE-S03 when the provider is local. |
| BE-O04 | Submission values and reveal | ADD ONB-15/16: `GET …/submissions/{sid}` returns the **masked payload** while not purged; `POST …/submissions/{sid}/reveal {field, reason}` | Decrypt with the KMS key version. Identifier and bank values are masked using EMS rules. Reveal is audited `ONBOARDING_SUBMISSION_VALUE_REVEALED`, rate-limited and returned with `no-store`. Never returned to DM or OTO. |
| BE-O05 | `POST Org/me/onboarding/tasks/{taskId}/documents` (+ complete), `POST Org/me/onboarding/documents/{docId}/download-url` | ADD §4.11, §11 | Only for open EMPLOYEE-owned tasks assigned to the caller |
| BE-O06 | `GET Org/onboarding/section-schemas` (+ `GET Org/me/onboarding/section-schemas`) | New | JSON Schema (draft 2020-12) per `section_code`, generated from the same Pydantic models used to validate ONB-11. Already filtered by `collect_*` settings and identifier policies, so the client can render forms and never sends a non-permitted field. |
| BE-O07 | `GET Org/files`, `POST Org/files` (initiate) → upload → `POST Org/files/{id}/complete`, `POST Org/files/{id}/download-url` | EMS §23.2 `organization_files` | Permission: `onboarding.template_manage` or `org.manage`. Same scanning and pre-signed URL rules as documents. |
| BE-O08 | `GET Org/onboarding-templates/{tid}/versions` | New | Version list with status, effective dates, change note and task count |

### 4.4 People and documents

| ID | Endpoint | Notes |
|---|---|---|
| BE-P01 | `GET Org/me/profile` (EMS §5.6 self-view fields; salary only with `self_view_salary`); `POST Org/me/profile/change-requests {section: CONTACT \| PERSONAL_LIMITED, payload}`; HR side `GET Org/profile-change-requests`, `POST …/{id}/approve \| reject` | Resolves OD-03. New table `employee_change_requests`, modelled on `onboarding_data_submissions` (encrypted payload, field names, purge, reviewer ≠ requester, apply through EMS services). Requires an ACTIVE record and `enable_employee_self_view`. |
| BE-P02 | Single GETs: `Emp/education/{rid}`, `Emp/previous-employment/{rid}`, `Emp/family-members/{rid}`, `Org/registrations/{id}`, `Emp/salary-assignments/{aid}`, `/users/{uid}`, `Org/leave-policies/{pid}/versions/{vid}`, `Org/attendance-policies/{pid}/versions/{vid}` | All return ETag (BE-C02) |
| BE-P03 | `POST Emp/identifiers/{type}/verify {status, remarks, proof_document_id?}` (`identity.verify`, new); `GET Emp/identifiers?include_history=true` | Defines `is_current` semantics. History rows are masked. |
| BE-P04 | `PATCH Emp/documents/{docId}` (title, description, document_number, issue_date, expiry_date) | `document.upload` holders; If-Match; category and file are immutable; recompute `expiry_state`; audit |
| BE-P05 | `POST Emp/age-exception-requests {reason}`, `GET Emp/age-exception-requests` | Pairs with the existing decision endpoint; the decider must differ from the requester |
| BE-P06 | `PATCH Emp/employment` (probation_months, notice_period_days, offer and appointment dates, source_of_hire, referrer_employee_id) and `PUT Emp/statutory-profile` | `employee.edit`; reason ≥ 10; audited; recompute `probation_end_date` consistently with EMS. Resolves CF-24 and FQ-32. Job attributes stay on job changes. |
| BE-P07 | `GET /reference/states` (ISO 3166-2:IN), `GET /reference/identifier-types` (`format_regex`, `checksum`, `example_masked`) | Closes FQ-09 and the states list item |
| BE-P08 | Attachment fields on education (`certificate_document_id`) and previous employment (`relieving_letter_document_id`, `experience_letter_document_id`) | Composite FK to the same employee's documents (ADD §8.4 pattern). Add only if not already present in the schema. |

### 4.5 Salary

| ID | Endpoint | Notes |
|---|---|---|
| BE-SAL01 | `GET Org/salary-templates/{tid}/versions`, `GET …/versions/{vid}` (with component mappings) | Closes FQ-26 |
| BE-SAL02 | `GET Org/salary-assignments?approval_state=PENDING&cursor&employee_id&sort` | `salary.revision_approve`. Each item has `can_act` (false for creator or submitter). Values are as recorded; **no totals or differences**. |
| BE-SAL03 | Contract clarifications: (a) `POST Emp/salary-assignments` with `components` omitted copies the template version defaults into the DRAFT; with `components` present, the supplied values override. (b) `compare` accepts DRAFT and SCHEDULED IDs. (c) `approve {confirm: true}` accepts `backdate_reason`; if it is missing for a backdated item → `422 BACKDATE_REASON_REQUIRED`. | Documented in OpenAPI descriptions and tests |

---

## 5. Resolving unclear endpoints (spec §25.3)

Each resolution must be written into the OpenAPI `description` of the endpoint and covered by a test.

| Endpoint | Resolution |
|---|---|
| Task `submit` vs `resubmit` | `submit` is valid from NOT_STARTED or IN_PROGRESS only; `resubmit` from REJECTED only. The wrong one returns `422 INVALID_ONBOARDING_TASK_TRANSITION`. |
| Leave withdraw/cancel: org path vs `/me` (BE-U02) | The `/me` path is for the owner. The org path is for HR on behalf: withdraw needs `leave.apply_on_behalf`, cancel needs `leave.cancel_any` (+ `leave.admin_correct` for closed years). An owner without those permissions on the org path gets `403`. |
| Regularization withdraw: org vs `/me` | Same rule. The org path needs `attendance.punch_manage`. |
| `GET /me/leave-applications/{aid}` vs org GET | `/me` returns the owner's full view. The org path applies ADD §10.5 field filtering for approvers and viewers. |
| Privacy notices | The org path `…/acknowledge` records a **paper** acknowledgement by HR (requires a supporting document). Add `POST Org/me/privacy-notices/{nid}/acknowledge` for electronic acknowledgement by the employee. Onboarding PRIVACY_NOTICE tasks write the same rows (ADD §4.12). |
| Retention batches | See BE-X04 |
| `GET Org/settings` permission | `org.manage`. All other users use `/me/context` (BE-S01). |
| Employee archive vs draft discard | Archive covers both. For a DRAFT, it cancels the open onboarding instance (`EMPLOYEE_ARCHIVED`, ADD §7.5) and records `archive_reason_code = DRAFT_DISCARDED`. |
| Salary approve with confirm, and empty components | BE-SAL03 |
| `GET Org/onboarding-tasks` default scope | Add `scope ∈ {actionable (default), all}`. `actionable` means assigned to the caller, their department queue, or awaiting their review. `all` requires `onboarding.view`. |
| `employee_id_format_settings` | Publish a schema if used; otherwise mark it `deprecated: true` and stop returning it in v1.x |
| `masters/{table_name}` values | BE-C06 |
| `recalculation_run_id` | Readable via BE-A05 |
| `ATTENDANCE_SOURCE` lookup | Labels `integration_source_code` values only. Punch `source` labels come from the enum (BE-C04). |
| `IdentifierResponse.is_current` | Current only by default; `include_history=true` adds history (BE-P03) |
| `GET /orgs?group_id=` | SSA only. Non-SSA users get group info embedded (BE-S06). |

---

## 6. Cross-cutting features

### BE-X01 Counts for My work and Overview

- `GET Org/me/work-summary` returns `{onboarding_tasks, onboarding_reviews, leave_awaiting_me, leave_hr_queue, regularizations_awaiting_me, regularizations_hr_queue, salary_approvals}`. Only keys the caller has permission for are included. It uses `COUNT` on the existing partial indexes and should be cacheable for 30 s per user.
- `GET Org/overview` returns permission-filtered counts: employees by status, onboarding in progress and overdue, documents expiring within the configured warning days, pending approvals, and on leave today (from ACTIVE slots of APPROVED applications). **No monetary values.**
- `with_total` is supported on every queue list (BE-C10).

### BE-X02 List exports (decision-gated, FQ-19)

`POST Org/exports {type: EMPLOYEES | ATTENDANCE | LEAVE_APPLICATIONS | LEAVE_BALANCES, filters, masked: true}` → `202` job run → `GET Org/exports/{id}` returns `download_url` and `expires_at`. Unmasked exports require `export.run` plus the reveal permissions, return a `one_time_password` once, and are audited. Salary data is excluded unless the caller holds `salary.view`, and computed money is never included.

### BE-X03 Storage CORS and security headers

- The bucket CORS policy allows `PUT` from configured app origins, headers `Content-Type` and `Content-MD5`, exposes `ETag`, and sets `MaxAgeSeconds` to 600. Publish the origin list for the frontend CSP `connect-src`.
- API responses set `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` on personal-data responses, `Strict-Transport-Security`, and `Referrer-Policy: no-referrer`. CORS on the API is closed when the BFF is same-origin.

### BE-X04 Retention batches: complete the contract

- Document permissions (`retention.manage` to create, `retention.approve` to approve; the approver must differ from the creator), the scope schema (`data_class` per ADD §13.5, `cutoff_date`, `organization_id`), the dry-run counts in the create response, the exclusion of legal-hold records, and asynchronous `execute` as a job run (BE-A05).
- Every state change is audited.

### BE-X05 Job architecture (decision-gated, FQ-28)

The JA endpoints exist. If product approves building the UI:

1. Confirm JA permissions in the catalogue (BE-C05).
2. Apply BE-C01 to C11 to all JA lists and actions.
3. Document `position_change` in `POST Emp/job-changes` and `422 HEADCOUNT_CHANGE_REQUIRED`.
4. Add JA scenarios to the contract tests.

If it is not approved, keep the endpoints and ensure `PUT Org/settings` preserves JA settings when the client sends them back unchanged.

### BE-X06 Bulk approve (decision-gated, FQ-17)

`POST Org/leave-applications/bulk-decisions` and `POST Org/attendance/regularizations/bulk-decisions` with `{items: [{id, row_version, decision, comment}] ≤ 50}`. Each item runs in its own transaction with full single-item rules (INT-32 to INT-34, LEV-29) and returns a per-item outcome. Not all-or-nothing.

---

## 7. Database migrations

| Migration | Contents |
|---|---|
| M1 | `idempotency_records` (BE-C03) |
| M2 | Settings columns from ADD §9.1 if missing (BE-S02) |
| M3 | Permission rows and default grants (BE-C05) |
| M4 | `users.must_change_password`, `password_changed_at`; `break_glass_grants.revoked_at`, `revoked_by`, `revoke_reason` |
| M5 | `employee_change_requests` (BE-P01): encrypted payload, field names, status, reviewer, purge columns; partial unique index for one pending request per section |
| M6 | `age_exception_requests` (BE-P05) |
| M7 | Attachment FK columns on education and previous-employment rows (BE-P08), if absent |
| M8 | Indexes for new filters and sorts: `onboarding_instances (organization_id, hr_coordinator_user_id, status)`; `leave_applications (organization_id, schedule_review_required)` partial; `attendance_regularization_requests (organization_id, business_date)`; `users (status)`, `user_organization_access (organization_id, role_code)` |
| M9 | MySQL generated columns for any new partial-unique rules (ADD §8.7 pattern) |

All migrations are reversible, run online (no long table locks on attendance tables; use `ALGORITHM=INPLACE` where possible), and are verified by the extended integrity checker.

---

## 8. Testing and quality gates

| Gate | Tool | Requirement |
|---|---|---|
| Contract conformance | Schemathesis against `openapi.json` in CI | No undeclared status codes, no schema violations, every error matches `ErrorResponse` |
| Breaking-change check | `oasdiff breaking` against the last released spec | Fails CI on unannounced breaking changes. The BE-C01 envelope change is the one approved exception. |
| Permission matrix | Parametrized pytest: every endpoint × every role from ADD §10.3 | Expected 2xx or 403 per matrix; field filtering asserted for DM, RV, RDR and OTO |
| Tenant isolation | ADD AC-53 suite extended to every new endpoint | XYZ path → 403; XYZ IDs → 404; no leakage in embedded summaries |
| Concurrency | Existing AC-45/47/48 plus bulk shift, bulk finalize and bulk decisions | No lost updates; locks follow ADD §7.3 |
| Acceptance | ADD AC-29 to AC-64 | All automated; new endpoints mapped in `tests/TRACEABILITY.md` |
| Performance | Load tests for BE-A01, A02, L01, X01 against ADD NFR-22/26/27 | p95 targets met on a 100k-employee seed |
| Publication | CI publishes `openapi.json` as a build artifact on every merge | The frontend regenerates its client from this artifact (FE-02) |

---

## 9. Delivery phases

| Phase | Items | Unblocks (frontend) |
|---|---|---|
| **B0 Contract** | BE-C01 to C08, C11, S01, S02, S03 (`is_platform_admin`, session fields), S09 | FE Phase 0 and 1: FE-02, 04, 05, 09, 12, 13, 23 |
| **B1 Read models** | BE-C09, C10, C12, X01, P02, SAL01, O08, A05, S05 (directory), S06, U02 resolutions (§5) | FE-07, 08, 10, 20, 21, 22, 46, 49, 50, 52, 76, 78 |
| **B2 Spec-defined gaps** | BE-A01, A02, A04, L01 to L03, O01, O03, O04, O06 | FE-70 to 74, 48 |
| **B3 Self-service and admin writes** | BE-P01, P03 to P08, A03, A07, A08, L05, L06, O05, O07, S04, S05 (reactivate), S03 (password), S07, S08, SAL02, SAL03, X03, X04 | FE-34, 35, 41, 42, 60, 62, 75 to 77, 79 to 82 |
| **B4 Decision-gated** | BE-X02, X05, X06, OIDC | FE-84, 85, 90 |

Every item is registered in `/meta` capabilities on deploy, so the frontend switches it on without a coordinated release.

---

## 10. Decisions needed from product, security and legal

| ID | Decision | Default if not decided |
|---|---|---|
| D-1 | Build the job architecture UI (FQ-28)? | No. Preserve settings only. |
| D-2 | List exports (FQ-19)? | No |
| D-3 | Bulk approve (FQ-17)? | No |
| D-4 | OIDC providers and timeline | Local login only |
| D-5 | Admin-issued temporary passwords acceptable without email delivery? | Yes, shown once, with forced change |
| D-6 | Direct edit of branch statutory numbers (BE-S08)? | No; edit through registrations |
| D-7 | HR punch file import allowed (BE-A07)? | Yes, as MANUAL source with batch remarks |
| D-8 | Self profile change-request sections (BE-P01) | CONTACT only |