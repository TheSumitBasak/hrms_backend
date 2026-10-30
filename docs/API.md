# API Guide

Base path: `/api/v1`. The generated OpenAPI document at `/openapi.json` (and `/docs`) is the
authoritative route and schema reference; this page records the cross-cutting conventions.

## Authentication

`Authorization: Bearer <access token>`. Tokens come from `POST /auth/login`, are rotated with
`POST /auth/refresh`, and are revoked by `POST /auth/logout`. Every request re-loads the user,
so deactivation takes effect on the next call. See `docs/AUTH.md`.

## Organization scoping

Organization endpoints live under `/orgs/{org_id}/…`. The organization is taken **only** from
the path; a body field is never trusted for it. A caller without an active grant there receives
`403 ORG_ACCESS_DENIED`. A resource id belonging to another organization returns
`404 RESOURCE_NOT_FOUND`, never a `403` that would confirm its existence.

## Error envelope

Every failure, including unhandled exceptions, uses:

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "The request contains invalid field values.",
    "details": [{"field": "pan", "code": "V-PAN", "message": "..."}],
    "warnings": [],
    "correlation_id": "..."
  }
}
```

The code list and HTTP status mapping live in `app/core/errors.py`.

## Warnings and acknowledgements

A write that raises a warning returns `422 WARNINGS_REQUIRE_ACKNOWLEDGEMENT` with a `warnings`
array. The client reviews them and resubmits the same body plus
`"acknowledged_warnings": ["W-..."]`. Acknowledged codes are recorded in the audit row.

## Optimistic concurrency

`GET` responses for mutable resources carry an `ETag` derived from `row_version`. `PATCH`,
`PUT`, deactivate and archive actions require `If-Match: <etag>`; a stale value returns
`412 PRECONDITION_FAILED`.

## Idempotency

Creation endpoints accept `Idempotency-Key: <uuid>`. A retry with the same key and payload
within 24 hours replays the original outcome instead of creating a second record.

## Implemented endpoint groups

| Section | Routes |
|---|---|
| §24.4.8 | `/auth/*`, `/users/*`, `/users/{id}/org-access`, `/break-glass`, `/roles`, `/permissions` |
| §24.4.1 | `/orgs`, `/org-groups`, `Org/settings`, `Org/registrations`, `Org/identifier-policies`, `Org/nomination-configs` |
| §24.4.2 | `Org/branches`, `Org/masters/{type}`, `Org/lookups/{list}`, `Org/custom-statuses` |
| §24.4.7 | `Org/employee-id-rules`, `…/preview`, `Org/employee-id-sequences`, `Org/employee-code-registry` |
| §24.4.3 | `Org/employees` (create/list/detail/edit/activate/status/job changes/archive/code reassignment) |
| §24.4.4 | `Org/employees/{id}/identifiers`, `Org/identifier-duplicate-check`, `Org/employees/{id}/bank-accounts`, `…/education`, `…/previous-employment`, `…/family-members`, `…/nominations` |
| §24.4.5 | `Org/document-categories`, `Org/employees/{id}/documents` (initiate/complete/versions/verify/archive/download-url), `Org/documents/expiring`, `/storage/{upload,download}` |
| §24.4.6 | `Org/salary-components`, `Org/salary-templates`, `Org/employees/{id}/salary-assignments`, `…/salary-history` |
| §24.4.8 | `Org/audit-logs`, `Org/employees/{id}/audit` |
| §10.9 | `Org/employees/{id}/transfers`, `Org/employees/{id}/age-exception-decision` |
| §25 | `Org/employees/{id}/privacy-notices`, `…/legal-hold`, `…/data-export`, `…/erasure-requests`, `Org/retention-batches`, `/storage/export/{token}` |

`Org` abbreviates `/orgs/{org_id}`.

Housekeeping jobs run via `python -m app.cli run-jobs` (once) or `serve-jobs` (interval; §24.7). `serve-jobs` also executes approved retention batches.
Retention batches need two authenticated approval calls: the first records `approved_by`, a
different user must call approve again to reach `APPROVED`.

### Leave (addendum 6, 11) - `/api/v1/orgs/{orgId}/...`

| Group | Endpoints |
|---|---|
| Types | `GET/POST /leave-types`, `GET/PATCH /leave-types/{id}`, `POST .../deactivate`, `.../reactivate` |
| Policies | `GET/POST /leave-policies`, `GET/PATCH /leave-policies/{id}`, `POST .../versions`, `PATCH .../versions/{vid}`, `POST .../versions/{vid}/publish`, `.../archive`, `POST /leave-policies/resolve` |
| Ledger | `GET /employees/{id}/leave-balances`, `GET/POST /employees/{id}/leave-transactions` (Idempotency-Key), `POST .../{tid}/reverse` |
| Applications | `POST /leave-applications/preview`, `GET /leave-applications`, `GET /leave-applications/{aid}`, `POST .../approve`, `.../reject`, `.../withdraw`, `.../cancel`, `.../reassign`, `.../recalculate`, `POST /employees/{id}/leave-applications` |
| Self-service | `POST/GET /me/leave-applications`, `GET /me/leave-applications/{aid}`, `POST .../withdraw`, `.../cancel`, `GET /me/leave-balances`, `GET /me/leave-transactions` |
| Processing | `GET/POST /leave-processing/runs` (Idempotency-Key; `leave.process_run`) |

Not yet: `leave-calendar`, `leave-summary`, `leave-year-closures`, `me/holidays`, supporting-document upload paths.

### Attendance (addendum 5, 11) - `/api/v1/orgs/{orgId}/...`

| Group | Endpoints |
|---|---|
| Shifts | `GET/POST /shifts`, `GET/PATCH /shifts/{id}` |
| Policies | `GET/POST /attendance-policies`, `GET/PATCH /attendance-policies/{id}`, `POST .../versions`, `PATCH .../versions/{vid}`, `POST .../versions/{vid}/publish`, `.../archive`, `POST /attendance-policies/resolve` |
| Assignments | `GET/POST /employees/{id}/shift-assignments` (truncation needs `W-SHIFT-ASSIGNMENT-TRUNCATES` acknowledged) |
| Punches | `POST /me/attendance/punches`, `POST /employees/{id}/attendance/punches`, `POST /attendance/punch-imports` (<= 1,000 items, each in its own transaction), `GET /employees/{id}/attendance/punches` |
| Records | `GET /employees/{id}/attendance`, `GET /employees/{id}/attendance/{date}`, `GET .../revisions`, `GET /me/attendance`, `GET /me/attendance/{date}` |
| Lifecycle | `POST /attendance/finalize`, `POST .../{date}/reopen`, `POST .../{date}/corrections` |

Not yet: `GET /attendance` (organization list), `GET /attendance/summary`,
`POST /attendance/day-close-runs`, `POST /shift-assignments/bulk`, `PATCH` of an assignment.

### Attendance - regularization and jobs

| Group | Endpoints |
|---|---|
| Regularization | `POST /me/attendance/regularizations`, `POST /employees/{id}/attendance/regularizations`, `GET /attendance/regularizations` (status, approver=me, hr_queue, employee_id), `GET /attendance/regularizations/{rid}`, `GET /me/attendance/regularizations`, `POST .../{rid}/approve`, `.../reject`, `.../withdraw` (also under `/me`), `.../reassign` |
| Jobs | `POST /attendance/day-close-runs` (one date in the last 31 days, `attendance.correct`, Idempotency-Key) |


### Onboarding

| Group | Endpoints |
|---|---|
| Templates | `POST/GET /onboarding-templates`, `GET /onboarding-templates/resolve`, `GET/PATCH .../{id}`, `.../deactivate`, `.../reactivate`, versions: `POST/GET .../{id}/versions`, `GET/PATCH .../versions/{vid}`, `PUT .../versions/{vid}/tasks`, `.../publish`, `.../archive` |
| Instances | `POST/GET /employees/{id}/onboarding` (Idempotency-Key required on POST), `GET /onboarding`, `GET /onboarding-tasks`, `GET/PATCH /onboarding/{iid}`, `.../complete`, `.../cancel`, `.../reopen` |
| Tasks | `POST /onboarding/{iid}/tasks` (ad hoc), `GET .../tasks/{tid}`, `.../start`, `.../complete`, `.../submit`, `.../resubmit`, `.../accept`, `.../reject`, `.../waive`, `.../reopen`, `.../reassign`, `.../due-date`, `.../cancel` |
| Documents, acknowledgements | `POST .../tasks/{tid}/documents`, `.../documents/{lid}/unlink`, `POST .../tasks/{tid}/acknowledgements` (paper) |
| Submissions | `POST/GET .../tasks/{tid}/submissions`, `.../{sid}/accept`, `.../reject`, `.../withdraw` |
| Self-service | `GET /me/onboarding`, `POST /me/onboarding/tasks/{tid}/start`, `.../complete`, `.../submit`, `.../acknowledge`, `.../submissions` |

Mutations take `If-Match: <row_version>`; responses carry the committed `row_version`. SELF users see only
`visible_to_employee` tasks with no remarks, events or assignees.

## Pagination and sorting

Audit lists are capped at the most recent 200 rows and reject a range wider than 366 days.
Employee search implements the §26.2 filter set, the §26.4 sort allowlist, signed cursor
pagination (`limit`, `cursor`, `with_total`), blind-index identifier search, and indirect
reporting search (`include_indirect`, depth ≤ 20).

## Sensitive values

Identifiers and bank account numbers are encrypted at rest (AES-256-GCM with AAD binding),
masked in every normal response, and revealed only through an audited, reason-bearing,
rate-limited `/reveal` call (permission `identity.reveal` / `bank.reveal`). Plaintext never
appears in audit payloads or logs.








