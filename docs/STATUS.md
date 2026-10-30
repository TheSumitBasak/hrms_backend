# Implementation Status, Measured Evidence and Known Limitations

Every claim here is backed by a command that was actually run, with its output. Nothing is
marked complete on the strength of code existing. Figures without a recorded measurement are
labelled as such.

**As of:** 2026-10-28 · **Phases complete:** 1 (discovery and design), 2 (foundation)
· **Phases 1–2 complete; Phase 3 domains implemented.** All §24.4 groups, §25 privacy, §26
search, transfer/age-exception, documents, salary and the §24.7 job functions are implemented
and tested. Remaining: ETag/multi-process race breadth, coverage below 80%, the acceptance-suite
split and external deployment integrations; see §3 below.

---

## 0. Continuation pass — final measured results

The Claude Code session stopped after §24.4.1. This pass verified the actual repository state,
then completed and mounted the organization endpoints (including the missing registrations,
identifier-policy and nomination-config routes), and implemented:

| Area | Endpoints / capability | Tests |
|---|---|---|
| §24.4.1 | Organizations (mounted), settings, groups, registrations, identifier policies, nomination configs | `tests/integration/test_organization_config.py` |
| §24.4.2 | Branches (with derived GSTIN/S&E/factory numbers), nine masters, lookups, custom statuses | `tests/integration/test_catalog_endpoints.py` |
| §24.4.7 | ID rule CRUD/publish/archive/preview, sequences, code registry, atomic issuance, reassignment | `tests/unit/test_code_rendering.py`, `tests/integration/test_id_rules.py` |
| §24.4.3 | Employee DRAFT create, detail/update, activation, status matrix, job changes/history, archive, code reassignment | `tests/integration/test_employees.py` |
| §24.4.8 | Organization audit log and per-employee audit, 366-day cap, salary-event filtering | same employee test module |
| §24.4.4 | Encrypted identifiers, masking, duplicate pre-check/override, audited reveal; bank accounts; education, previous employment, family, nominations | `tests/integration/test_identifiers.py`, `test_bank_accounts.py`, `test_profile_records.py` |
| §24.4.5 | Categories, signed local upload/download, complete, versions, verify/reject, archive, expiring list | `tests/integration/test_documents.py` |
| §24.4.6 | Components, versioned templates, assignments, O1–O4 core, approval, timeline/current/compare/history | `tests/integration/test_salary.py` |
| §26 | Filters, sort allowlist, signed cursor pagination, identifier search, field set | `tests/integration/test_employee_search.py` |
| §24.7 | Salary/rule/template refresh, upload cleanup, quarantine purge, integrity findings, scheduler CLI | `tests/integration/test_jobs.py` |
| §25 | Notices/consent, encrypted expiring export, legal hold, erasure, retention batch propose/approve/execute | `tests/integration/test_privacy.py` |
| §10.9 | Age-exception maker-checker, inter-organization transfer with selected data copy | `tests/integration/test_employees.py`, `test_transfers.py` |
| Concurrency | 50-way code issuance, overlapping salary confirmation, primary-bank race | `tests/concurrency/` |

Final checks in this pass (commands actually run from the repository root):

| Check | Command | Result |
|---|---|---|
| Lint | `ruff check app tests scripts` | **All checks passed** |
| Types | `mypy app` | **Success: no issues found in 111 source files** |
| Format | `ruff format --check app tests scripts` | **161 files already formatted** |
| Tests | `pytest tests -q` | **844 passed**, 0 failed, 0 skipped, ~309 s |
| Coverage | `pytest --cov=app/services` | **67%** total; target is 80% (NFR-16) |

'### Targeted review findings (2026-10-28)

- A retention batch now requires **two authenticated signatures**: the first is recorded without
  approving, and a different user must call approve to reach `APPROVED`.
- The salary `_confirm` path serialises on the employee row and the sequence path uses a named
  lock plus atomic UPDATE; the AC-16/salary-overlap/bank races pass.
- Remaining accepted risks: the development export writes its encrypted blob inside the request
  transaction (local storage only); the reveal limiter is process-local; and erasure deletes
  documents directly, so a document referenced by a retained privacy notice would raise a FK
  error rather than a domain error.
- No authorization bypass was found in the new routers: every new path resolves `OrgContext`
  from the path and enforces a permission before touching data.

The §23.6 cross-organization sweep is now active' (2 previously skipped tests execute against the
mounted organization routers and pass).

### Files added or changed in this pass

| Kind | Files |
|---|---|
| Repositories | `app/repositories/{branch,master,employee,employee_code,identifier,bank,audit}.py`; additions to `organization.py` |
| Services | `app/services/{catalog,org_config,employees,employee_codes,identifiers,bank,audit_query}.py` |
| Schemas | `app/schemas/{catalog,employee,id_rules,identifier,bank,audit}.py`; additions to `organization.py` |
| Endpoints | `app/api/v1/endpoints/{organizations,catalog,id_rules,employees,identifiers,bank,audit}.py` |
| Wiring | `app/api/deps.py`, `app/api/router.py` |
| Tests | `tests/unit/test_code_rendering.py`; `tests/integration/test_{organization_config,catalog_endpoints,id_rules,employees,identifiers,bank_accounts}.py`; `tests/conftest.py` (`put` helper) |
| Docs | `docs/{STATUS,IMPLEMENTATION_CHECKLIST,TRACEABILITY,API,TESTING,AUTH,DATABASE}.md` |

No migration was added and no model or table was changed: this pass was services, API and tests
over the existing Phase 3 schema. The only non-API production change is the 38 files
reformatted to the project's Ruff format and the BOM removal those files required.

---

## 0d. Continuation pass - onboarding, lifecycle hooks (2026-10-29)

**History.** Built onboarding (addendum 4): templates with versions and the 4.4 selection rule,
instances and tasks (4 task types, dependencies, waive/reopen/reassign/due-date, ad-hoc tasks),
EMS document links (a link submits the task; EMS verification completes or rejects it),
acknowledgements (electronic by the employee only, paper by HR), data submissions for PERSONAL,
CONTACT, EDUCATION and FAMILY (encrypted payload, applied through the EMS services on accept,
payload purged), activation gating (ONB-04, mandatory blocking tasks) and the `/me/onboarding*`
self-service views. Migration `c6f93e2a8b74` (12 tables, 109 in total; round trip verified).
Lifecycle hooks (`services/lifecycle.py`, INT-36 manager-change reroute, 7.5 employment end / archive
cancellations, activation and joining-date hooks for onboarding) are now covered by AC-50 / AC-51.
`LeaveBalanceRepository.lock_or_create` now flushes before refresh (found by AC-51).

**Defects found by the new tests and fixed**

| # | Finding | Status |
|---|---|---|
| 1 | `InstanceOut` required derived fields at validation, so every instance response was a 500. | **Fixed** (defaults, set by the serialiser) |
| 2 | `initiate` returned `row_version` 1 while the committed row was 2 (target date set after the first flush), so the next `If-Match` got 412. | **Fixed** (flush before the view) |
| 3 | Ad-hoc tasks raised `TypeError` (owner / reviewer references passed twice to the model). | **Fixed** (`OWNER_REVIEWER_REFS` popped before construction) |

**Not built (onboarding)**: self-service access provisioning (`/employees/{id}/self-service-access`),
the reveal endpoint (ONB-15), the ONB-13 payload retention purge job for REJECTED / WITHDRAWN
submissions, sections IDENTIFIERS / PREVIOUS_EMPLOYMENT / NOMINATIONS / BANK (refused with
`SECTION_NOT_SUPPORTED`), self upload / download under `/me/onboarding/tasks/{id}/documents`,
`GET /onboarding/summary` and cursor pagination, `onboarding_task_id` on the EMS upload initiation,
ACTIVE-ness check of referenced masters at publish.

Tests added: unit `test_onboarding_rules.py` (11), integration `test_onboarding.py` (templates,
initiation, completion / activation, cancel / reopen, tasks; AC-29..AC-34), `test_onboarding_self_service.py`
(8) and `test_onboarding_documents.py` (3). Full suite: **1280 passed, 0 failed** (36 min 48 s, one pytest session).

## 0c. Continuation pass - regularization and attendance jobs (2026-10-29)

**History.** Added regularization (addendum 5.14, ATT-20..23): requests, immutable items and
append-only actions (migration `b5e82d1f7a63`, 3 tables), a service on the shared approval model
(`services/approvals.py`: routing, self-approval prevention, distinct level-2 approver, HR override,
reassignment), and its endpoints. Added ATTENDANCE_DAY_CLOSE and ATTENDANCE_AUTO_FINALIZE
(`services/attendance_jobs.py`) on a shared job base (`services/job_runs.py`, extracted from the leave
jobs), wired into `app.cli run-jobs`, plus a manual `POST /attendance/day-close-runs`.
Manual and automatic finalization now share `RecordEngine.try_finalize`.
Tests added: 4 job tests, 6 regularization tests (AC-40, AC-41 and the ATT-20..23 rules).
Last full run before this pass: **1230 passed, 0 failed** (29 minutes).

**Targeted review findings**

| # | Finding | Status |
|---|---|---|
| 1 | Tests logged a user in before committing it (401) and created a manager's account after the application was routed (HR queue). | **Fixed** (test ordering) |
| 2 | Run bookkeeping was duplicated between the leave and attendance jobs. | **Fixed** (`JobBase`) |
| 3 | The regularization list has no cursor (newest 100). | Accepted, `shortcut:` until volumes are measured |
| 4 | `supporting_document_id` is checked by composite FK to the organization only; same-employee and ATTENDANCE_EVIDENCE category are not verified. | **Open** |
| 5 | SUBMITTED regularizations are not cancelled when employment ends (7.5). | **Open** |
| 6 | The scheduled day close is run by the interval scheduler, not at `attendance_day_close_time`; reruns are idempotent. | Accepted |

## 0b. Continuation pass - attendance core and leave integration (2026-10-29)

**History.** Added the attendance core (addendum 5.3-5.13, 7.4): shifts, attendance policies and
versions, shift assignments, punches (self, manual, integration with replay), daily records with
the 5.10 evaluation, revisions, manual finalize/reopen and administrative correction. Migration
`a3d91c7e5b42` (7 tables; a new revision, not an append to `d9a53b6ce232`, DEC-OAL-3). A shared
`ScheduleResolver` now implements 5.6 for both modules (shift-assignment pattern, policy default
pattern, organization default) with cached loads, replacing about five queries per date. Leave
hooks: LEV-30 punch-conflict warning, LEV-31 override conflict (at submission and again at final
approval), INT-10 on approval, INT-11 on record creation, INT-12 on cancellation.
Tests added: 22 unit (business date, evaluation, DST), 23 integration (config, punches, evaluation,
import, finalize/correct), 6 leave-attendance (AC-42, AC-43), 3 concurrency.

**Targeted review findings (attendance)**

| # | Finding | Status |
|---|---|---|
| 1 | `POST /attendance/punch-imports` returned `200` with per-item `REJECTED` to callers without `attendance.import`. | **Fixed** (`require_import` before the loop); covered by the role test |
| 2 | `remove_leave` used `any()` over a generator, so only the first half was cleared on cancellation. | **Fixed** (list comprehension); covered by the cancellation test |
| 3 | Leave called `day_type` per slot (about five queries per date). | **Fixed** (cached `ScheduleResolver`) |
| 4 | Policy resolution still uses current employee attributes (ATT-07 asks for job history as of the date). | Accepted, DEC-OAL-7 |
| 5 | Flexible employees have no shift midpoint, so any VALID punch on a leave date conflicts with both halves (LEV-30). | Accepted interpretation, DEC-OAL-10 |
| 6 | Attendance `DER (stored)` columns are written by the evaluation service, not generated columns. | Accepted, DEC-OAL-11 |
| 7 | Parallel pytest sessions on one `ems_test` schema cause deadlocks, "role not seeded" and cross-organization errors in fixtures. | Environment caveat: run one session at a time |
| 8 | Not built: regularization (5.14), day-close and auto-finalize jobs, holiday/assignment recalculation (INT-21), org-wide list, summaries, bulk assignment, ATT-10 re-derivation, pending ON_DUTY check in LEV-31. | **Open**, marked `shortcut:` |

## 0a. Continuation pass - leave module (2026-10-29)

**History.** Leave (addendum 6, 7.1, 11, 14) was implemented on the existing schedule layer: 10 tables
(migration `eab64c7df343`), rules, config/ledger/application/job services, ~30 endpoints, a daily
`run-jobs` hook and a manual run endpoint. Tests added: 13 unit, 13 config/ledger, 25 application,
5 concurrency (real threads), 7 job tests. Last full run: 1167 passed, 4 failed (stale schema tripwires,
fixed afterwards); unit suite 713 passed; `ruff` and strict `mypy` clean.

**Targeted review findings (leave)**

| # | Finding | Status |
|---|---|---|
| 1 | Generated balance columns were stale in the response after a posting (`available_days` read 0.00). | **Fixed** (`LeaveBalanceRepository.reload`), covered by ledger tests |
| 2 | `test_model_schema` tripwires (module/table count, reserved word `trigger`, `leave_` forbidden) failed. | **Fixed** (DEC-OAL-9) |
| 3 | Two application tests used ranges ending on a weekend. | **Fixed** (test data) |
| 4 | Leave duration calls `ScheduleService.day_type` per slot (several queries per day). | **Open** - addressed in the attendance-core pass by a range resolver |
| 5 | Policy resolution uses current employee attributes (DEC-OAL-7). | Accepted, `shortcut:` |
| 6 | Accrual runs up to 3 frequencies x 12 periods x 2 policy resolutions per (employee, type); no measurement. | Accepted until profiled (NFR-28 unproven) |
| 7 | Attendance effects of leave (INT-10..12, LEV-30/31) not wired. | **Open** - attendance-core pass |
| 8 | Intermittent `401` / `test_bootstrap` flake under long runs. | Not reproduced in the last full run; cause not found |

## 1. What has actually been verified

Commands run from the repository root with the project virtual environment:

| Check | Command | Result |
|---|---|---|
| Lint | `ruff check app tests scripts` | **All checks passed** (130 Python files) |
| Format | `ruff format --check app tests scripts` | **161 files already formatted** |
| Types | `mypy app` | **Success: no issues found in 111 source files** (strict mode) |
| Tests | `pytest tests -q` | **807 passed**, 0 skipped, 0 failed, ~139 s |
| Seed | `python -m app.cli seed` (via explicit settings) | 8 roles, 42 permissions, 73 links, 37 states, 18 identifier configs; **idempotent on re-run** |
| Database | `python -m app.cli check-db` | Connected to **MySQL 8.4.11** |
| Migration | `alembic upgrade head` on schema `ems` | **Applied: 61 tables** |
| Migration round-trip | upgrade → downgrade → upgrade on `ems_test` | **Clean both ways** |

Test counts by suite:

| Suite | Tests | Needs a database |
|---|---|---|
| `tests/unit` | 612 | no |
| `tests/integration` | 208 | yes |
| `tests/security` | 21 | yes |
| `tests/concurrency` | 3 — AC-16, salary overlap, primary bank | yes |
| `tests/acceptance` | 0 — AC suite still to split out | yes |

Applied schema, measured from `information_schema` after `alembic upgrade head`:

| Property | Count |
|---|---|
| Tables | 61 (plus `alembic_version`) |
| CHECK constraints | 287 |
| Foreign keys | 234, of which **41 are composite** (the §6.3 tenant-consistency keys) |
| Indexes | 416 |
| Stored generated columns (business) | 23 — the ADR-003 partial/exclusion-constraint emulation |
| FULLTEXT indexes | 1 (`employees.full_name_normalized`, for §26.2 search) |

**Coverage is not yet measured.** NFR-16 targets ≥80% line coverage on domain services; there
are no domain services yet, so a percentage now would be meaningless. It will be recorded here
once services land.

---

## 2. What is implemented

### Phase 1 — discovery and design (complete)

The full 2,616-line specification was read, the repository inspected, and four design
documents produced: `ARCHITECTURE.md` (layering, isolation model, MySQL integrity strategy,
ADR-001..006), `DECISIONS.md` (44 decisions covering schema additions, spec contradictions,
legal defaults and §29 open questions), `TRACEABILITY.md` (requirement → endpoint → service →
constraint → test matrix) and `IMPLEMENTATION_CHECKLIST.md`.

### Phase 2 — foundation (complete and verified)

| Module | Purpose | Verified by |
|---|---|---|
| `app/core/config.py` | Validated settings; refuses placeholder secrets, undersized keys, identical encryption/blind-index keys, and unsafe staging/production configurations | config validators exercised indirectly by the test harness |
| `app/core/errors.py` | All 44 spec error codes plus 6 documented additions; one code→status mapping | `tests/unit/test_errors_and_warnings.py` (transcribes the §24.3/§24.5 tables) |
| `app/core/warnings.py` | 29 warning codes (16 spec-named, 13 conventional) and the §21.3 acknowledgement protocol | same file |
| `app/core/permissions.py` | §5.2 permission enum, §5.3 matrix, `C`-cell resolution, §5.4 scoping and field sets | `tests/unit/test_permissions.py` (56 assertions against the matrix) |
| `app/core/crypto.py` | AES-256-GCM field encryption with AAD binding, HMAC blind indexes, HKDF subkeys | `tests/unit/test_crypto_and_masking.py` |
| `app/core/masking.py` | §11.6 mask formats, bank and document masks, audit-safe values | same file, against the spec's own examples |
| `app/core/clock.py` | UTC time, organization-timezone "today", half-open intervals, financial years, `{YEAR}` tokens, sequence scope keys | `tests/unit/test_clock.py` |
| `app/core/security.py` | bcrypt password hashing with pre-hash, local token issuer, OIDC verifier seam | covered indirectly; dedicated tests land with the auth endpoints in Phase 3.1 |
| `app/core/logging.py` | Structured JSON logs, correlation ids, redaction filter | `tests/integration/test_api_contract.py` (correlation id behaviour) |
| `app/core/exception_handlers.py` | §24.2 envelope for every error path | `tests/integration/test_api_contract.py` |
| `app/validation/` | All 24 `V-*` rules, normalisers, Verhoeff and GSTIN mod-36 checksums | `tests/unit/test_validation_rules.py` (including checksum property tests) |
| `app/audit/` | §20.2 action vocabulary, change-set builder with §20.3 redaction | `tests/unit/test_audit_events.py` |
| `app/db/` | UUIDv7 `BINARY(16)` and UTC `DATETIME(6)` types, declarative base, §1.5 column mixins, unit of work, MySQL error translation | `tests/integration/test_migrations.py` |
| `app/schemas/common.py` | Signed cursor pagination, sort allowlist, ETag/`If-Match` | `tests/unit/test_pagination_and_etags.py` |
| `app/api/middleware.py` | Correlation ids, security headers, no-store cache policy | `tests/integration/test_api_contract.py` |
| `app/main.py` | Application factory, `/health`, `/ready` | same |
| `app/cli.py` | `generate-keys`, `check-db`, `show-config` | run manually; `generate-keys` and `check-db` both exercised |
| `alembic/env.py` | Bound to application settings, caller-overridable URL | `tests/integration/test_migrations.py` |

### Phase 3 (in progress) — persistence layer complete and verified

All 61 tables of the §22/§23.2 entity catalogue are modelled across 15 modules, and the
baseline migration is applied to a real MySQL 8.4.11 schema.

| Domain | Module | Tables |
|---|---|---|
| Reference data | `reference.py` | `states` |
| Platform | `platform.py` | users, roles, permissions, role links, refresh tokens, idempotency keys, login attempts |
| Organizations | `organization.py` | groups, organizations, settings, organization files |
| Access control | `access.py` | organization access grants, per-organization permission overrides |
| Audit | `audit.py` | append-only audit log, integrity findings |
| Branches | `branch.py` | branches |
| Masters | `master.py` | nine master tables, lookup values, custom statuses |
| Employees | `employee.py` | employees, addresses, job history, status history |
| Identifiers | `identifier.py` | encrypted identifiers with blind indexes |
| Profile records | `profile.py` | education, previous employment (+ payslip links), family, nominees, bank accounts |
| Documents | `document.py` | categories, category MIME allowlists, document versions |
| Salary | `salary.py` | components, templates, versions, mappings, assignments, assignment values, revision history |
| Organization config | `org_config.py` | registrations, identifier policies, nomination configs, platform identifier config, notice versions |
| Employee codes | `employee_code.py` | ID rules, sequence counters, code registry |
| Privacy | `privacy.py` | notices/consent, change requests, erasure requests, purge batches |

**What the database now enforces by itself** — verified by 60 integration tests that write with
SQLAlchemy Core, deliberately bypassing the service layer, because §18.8 requires more than
application validation and AC-03/AC-04/AC-10 each demand that a *direct insert* be rejected:

| Constraint | Enforcement verified |
|---|---|
| C-02 employee code unique per organization | duplicate rejected, case-insensitively; same code in another organization accepted; many code-less drafts accepted; registry rejects reuse |
| C-03 master references stay in the tenant | a department from another organization is rejected by the composite FK (AC-03) |
| C-04 manager in the same organization | rejected across organizations |
| C-05 / INV-1 salary timeline | two confirmed assignments with the same start rejected; DRAFT and CANCELLED do not occupy the slot; adjacent intervals accepted |
| C-06 one current identifier per type | second current PAN rejected; historical row coexists; multiple current `PREVIOUS_PF_MEMBER_ID` accepted |
| C-07 one primary bank account | two primaries starting the same day rejected |
| C-13 child records belong to the same employee | cross-organization child insert rejected |
| C-14 / C-15 dates and money | reversed intervals and negative amounts rejected |
| §1.5 archival | archiving without a reason, or with a reason under ten characters, rejected |
| §7.3 identifier applicability | CIN required for a private limited company, forbidden for a trust; LLPIN only for an LLP; PAN unique except for proprietorships |
| §7.4 registration scopes | GSTIN unique across tenants, TAN only within one; state-scoped types require a state code |
| §10.2 / §10.7 employee rules | separated and ON_NOTICE statuses require an end date; EMP-01 and EMP-05 enforced; rehire requires a linked record |
| §16.2 typed values | a percentage in the amount column is rejected; FIXED_AMOUNT requires a period; OTHER_CONFIGURED requires a regex |
| §17.4 maker-checker | self-approval rejected by the schema, not only the service |
| §9.2 / §9.4 masters | grade rank unique per organization and range-checked; master codes unique case-insensitively and pattern-checked |

### Phase 3 slice complete: platform, authentication and access control

Everything above the persistence layer now exists for the §24.4.8 domain, end to end.

| Layer | What landed |
|---|---|
| Context | `ActorContext` / `OrgContext` — immutable, carrying the caller, their organization and resolved permissions |
| Repositories | `OrgScopedRepository`, which **cannot be constructed without an organization id** and filters every query it builds — the mandatory data-access filter §23.6 requires of a MySQL deployment; plus platform and organization repositories |
| Audit | `AuditWriter`, sharing the caller's session so a business change and its audit row commit together (NFR-10), with the §20.3 payload check re-applied before insert |
| Services | `AuthService` (login, refresh rotation with replay detection, logout), `AccessService` (permission resolution, the §5.5 60-second cache, grant/revoke, break-glass, deactivation) |
| Seed | §5 roles and permissions, §21.2 identifier validation config, §23.2 states; per-organization §24.4.1 seeding of settings, 19 lookup lists, 25 document categories, 8 employment types, 9 identifier policies, 4 nomination configs |
| Dependencies | `app/api/deps.py` — session, caller, organization context from the **path only**, `require(...)` permission guards, `If-Match`, `Idempotency-Key` |
| Endpoints | 16 operations across `/auth/*`, `/users/*`, `/users/{id}/org-access/*`, `/break-glass`, `/roles`, `/permissions` |

**Security properties now verified by test, not asserted by comment:**

| Property | Evidence |
|---|---|
| A grant in one organization confers nothing in another | `tests/security/test_tenant_isolation.py` |
| `403 ORG_ACCESS_DENIED` vs `404 RESOURCE_NOT_FOUND` are distinct, and neither discloses existence | same |
| An Organization Admin cannot grant SSA, nor grant into an organization it does not administer (§5.3 note 4) | same |
| An HR Admin cannot manage users — full HR data authority is not administration | same |
| SSA holds **no** employee-data permission, and no `C` cell can grant one | same |
| Break-glass is capped at 4 hours, needs a ≥20-character reason, and is audited into the *target* organization's log | same |
| A revoked or expired grant stops resolving | same |
| Unknown email and wrong password are indistinguishable in body and in timing | `tests/integration/test_auth_endpoints.py` |
| A rotated refresh token is consumed; replaying it revokes the whole family | same |
| Deactivation invalidates a live access token on its next use | same |
| An override row cannot widen a role beyond the §5.3 matrix | `tests/unit/test_access_resolution.py` |

The §23.6 "attempt cross-organization access for **every** endpoint" suite is written and
enumerates the live route table, so it covers each organization-scoped endpoint the moment one
is registered. It currently skips with an explicit message, because no `/orgs/{org_id}` router
exists yet — the skip is the signal that will turn into coverage.

### Defects found and fixed during Phase 3 (services and endpoints)

Each was found by a failing test, not by inspection:

1. **A configuration error reached clients as `422` with internal detail.** The global handler
   caught `pydantic.ValidationError`, which covers request validation *and* a settings object
   that fails to load — so an unfilled `.env` produced a `422` naming the setting and its
   remediation hint. Narrowed to `RequestValidationError`; internal validation errors now
   return an opaque 500 (DEC-050).
2. **An app built with explicit settings still read the ambient `.env`** on every request,
   because the settings dependency called the process singleton. Settings are now published on
   `app.state` (DEC-051).
3. **Refresh-token replay containment was rolled back.** Revoking the token family then raising
   `401` meant the `unit_of_work` discarded the revocation. It now commits explicitly, with the
   reasoning documented at the call site (DEC-052).
4. **`EmailStr` was stricter than §21.1** and would reject a legitimate internal domain such as
   `hr@corp.local`. Replaced with an `EmailAddress` type that applies V-EMAIL exactly (DEC-053).

### Defects found and fixed during Phase 3

Each was found by a test or a failing migration, not by inspection:

1. **CHECK violations would have returned 500 instead of 422.** MySQL reports a violated CHECK
   (3819), an over-long value (1406) and an out-of-range value (1264) as `OperationalError`,
   while duplicate keys and failed foreign keys arrive as `IntegrityError`. The translator
   dispatched on the exception *class*, so every CHECK violation fell through to the generic
   handler. Rewritten to dispatch on the MySQL error number (DEC-048).
2. **Blind-index columns could not be indexed.** `LargeBinary` becomes `BLOB` on MySQL, which
   MySQL refuses to index without a prefix length — so the duplicate-detection and
   identifier-search indexes of §11.5 and §26.2 could not exist. Fixed with explicit
   `VARBINARY` column types (DEC-047).
3. **Two reserved-word columns broke `CREATE TABLE`.** `grades.rank` and
   `employee_id_generation_rules.separator` are MySQL 8 reserved words and were referenced bare
   in raw CHECK SQL. Cost two partially applied migrations. Now backtick-quoted, with a test
   that fails on the next occurrence (DEC-046).
4. **The autogenerated downgrade was not runnable.** Alembic emits `drop_index` before
   `drop_table`, and MySQL refuses to drop an FK-backed index; the schema also has legitimate FK
   cycles. The baseline downgrade is hand-written and the round-trip is verified (DEC-049).
5. **Constraint names were doubled** (`ck_roles_ck_roles_role_code_length`) because SQLAlchemy's
   `ck` naming convention substitutes an explicitly given name. The convention entry was removed
   in favour of fully-qualified names at the point of definition.

### Defects found and fixed during Phase 2

These were found by the tests, not by inspection:

1. **Correlation id lost on 500s.** `RequestContextMiddleware` cleared the request context
   before re-raising, but Starlette handles unhandled exceptions in `ServerErrorMiddleware`,
   which sits *outside* the middleware stack. The 500 envelope therefore carried
   `correlation_id: null` — on the one response where traceability matters most. Fixed by
   reading `request.state` as well as the context variable, and by not clearing the context
   on the error path.
2. **`CORS_ORIGINS` could not be loaded.** pydantic-settings JSON-decodes a `list[str]` field
   before validators run, so the comma-separated form documented in `.env.example` raised
   `SettingsError` at startup. Fixed with the `NoDecode` marker plus a before-validator.
3. **`/ready` reported the wrong database.** `check_connection()` used the lazily created
   process engine built from the ambient `.env`, even when the application had been
   constructed with explicit settings — so a configuration error surfaced as "database down".
   Fixed by binding the process database handle from the application's own settings at
   startup.
4. **Session `sql_mode` replacement broke reflection** — see DEC-041. Replacing `sql_mode`
   stripped `ANSI_QUOTES` after SQLAlchemy's dialect had detected it, so every reflection
   query failed. Fixed by appending the required modes to the server's own.

---

## 3. Known limitations

### Remaining gaps

| Missing | Specification | Status |
|---|---|---|
| Coverage ≥80% | NFR-16 | measured **67%** on `app/services`; the largest gaps are `salary.py` (54%), `organizations.py` (28%) and `employees.py` (64%) |
| ETag race and multi-worker concurrency breadth | §24.1, NFR-04 | AC-16, salary-overlap and primary-bank races are tested; ETag and multi-process races are not |
| Acceptance-criteria suite split | §28 | 28 AC scenarios are covered piecemeal across integration tests; there is no `tests/acceptance` suite |
| Deployment integrations | NFR-07..21 | KMS, S3, malware scanner, OIDC/MFA and region selection remain deployment configuration |
| Transfer copy scope | §10.9 | education/family/previous employment/nominations/identifiers can be copied; bank, salary and documents are intentionally not copied |

The cross-organization sweep, permission matrix, employee lifecycle, ID issuance, identifier
masking/reveal, audit reads and the single-primary bank invariant are tested. Of the 28
acceptance criteria, the following now have direct automated coverage: AC-01 (isolation),
AC-02/AC-13/AC-14/AC-15/AC-17 (ID rules, at service/endpoint level), AC-04 (code uniqueness),
AC-20/AC-27 (identifier and bank masking/reveal permissions), AC-21 (audit row per mutation).
AC-16's fifty-way race is **not yet automated**; the row-lock algorithm is implemented but the
concurrency test is outstanding. AC-03 and the remaining salary/document criteria cannot be
automated until their services exist.

The primary-bank-account interval check is implemented under an employee row lock, but there is
no concurrency test yet.

### Unconfigured integrations

| Integration | State | Where |
|---|---|---|
| **Identity provider (OIDC)** | Not configured. `OidcTokenVerifier` is wired but raises until `OIDC_ISSUER`, `OIDC_AUDIENCE` and `OIDC_JWKS_URL` are supplied. Local password accounts work as the documented fallback (A-04). **NFR-12's MFA requirement for privileged roles cannot be met by local login**, which is why `Settings` refuses a staging/production configuration that enables local login without OIDC. | `app/core/security.py`, `docs/AUTH.md` |
| **KMS / HSM** | Not available. `KeyProvider` defaults to `EnvKeyProvider`, which derives per-organization keys from an environment master key with HKDF — the §23.7 envelope *shape* without a KMS. `KmsKeyProvider` raises rather than falling back, and `Settings` refuses `key_provider=env` in staging or production (A-05). | `app/integrations/keys.py`, `docs/CONFIGURATION.md` |
| **Malware scanner** | Not available. `MALWARE_SCANNER_BACKEND=noop` is permitted locally only; staging and production require `external` (A-06). | `docs/CONFIGURATION.md` |
| **Object storage** | Only the local private-directory backend is planned for development; S3-compatible storage is required in staging and production. Data residency (NFR-21, Q-09) is deployment configuration — **no region is chosen in code**. | `docs/CONFIGURATION.md` |

### Environment caveats

- **`DATABASE_URL` must point at the `ems` schema.** The repository's current `.env` is the
  v1 file: it points at `hrms_dev_db` (the v1 HRMS schema, see DEC-043) and lacks
  `ENCRYPTION_MASTER_KEY` and `BLIND_INDEX_MASTER_KEY`, so the application and `alembic`
  currently refuse to start. This is deliberate fail-fast behaviour. See the README for the
  three lines to add.
- `.env` was **not modified** — it holds developer secrets.
- The test harness never reads `.env` secrets: it builds settings in memory with freshly
  generated throwaway keys and forces the schema name to end in `_test`.
- An empty schema `hrms_dev_db_test` was created by an earlier run of the database script
  before the schema separation in DEC-043 was decided. It is unused and can be dropped.

### Performance

No performance target has been measured. NFR-01..NFR-04, NFR-07..NFR-09, NFR-13, NFR-16,
NFR-17 and NFR-21 are all flagged `[ASSUMPTION]` in §3.2 and remain **targets to validate**,
not guarantees. The only timing recorded so far is incidental: ~2 s for a first MySQL
connection on this server and ~200 ms for subsequent pooled connections (DEC-044).

### Accepted design trade-off

MySQL has no exclusion constraints or row-level security, so INV-1 (non-overlapping confirmed
salary assignments), the single-primary-bank-account rule and ID-rule conflict prevention rest
on an employee row lock, an in-transaction check, a same-start unique index on a generated
column, and a nightly integrity job — the arrangement §18.8 explicitly permits. Tenant
isolation uses a mandatory repository filter plus composite foreign keys plus adversarial
tests, per §23.6's MySQL variant. `ARCHITECTURE.md` §7 has the full mapping.




























