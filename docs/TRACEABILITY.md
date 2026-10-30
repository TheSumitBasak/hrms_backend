# Requirements Traceability Matrix

> **Status note (2026-10-28).** All §24.4 groups, §25 privacy, §26 search, transfer/age
> exception, documents, salary and the §24.7 job functions are implemented. Remaining PLANNED
> items are the acceptance-suite split, ETag/multi-process races, coverage uplift and deployment
> integrations. See `docs/STATUS.md` §0 for executed evidence.

Maps every numbered requirement in `EMPLOYEE_MANAGEMENT_SYSTEM.md` to the code that implements it and
the test that proves it. Status values:

| Status | Meaning |
|---|---|
| PLANNED | Designed, not yet implemented |
| BUILT | Implemented, tests not yet run |
| **VERIFIED** | Implemented **and** a test covering it was executed and passed |

Nothing is marked VERIFIED without a test run recorded in `docs/STATUS.md`. Paths are relative to the
repository root; `Org` abbreviates `/api/v1/orgs/{org_id}`.

---

## 1. Functional requirements (§3.1)

| ID | Requirement | Endpoint(s) | Service / module | DB constraint | Test | Status |
|---|---|---|---|---|---|---|
| FR-ORG-01 | Organization CRUD + (de)activation | `GET/POST /orgs`, `GET/PATCH Org`, `POST Org/deactivate\|reactivate` | `services/organizations.py` | `uq organizations.organization_code_normalized`, `uq cin`, `uq llpin` | `tests/integration/test_organization_config.py`, `tests/integration/test_catalog_endpoints.py` | **VERIFIED** |
| FR-ORG-02 | Multiple statutory registrations per org, per state/branch | `GET/POST Org/registrations`, `PATCH Org/registrations/{id}` | `services/org_config.py` | type-scoped uniques (§7.4), composite FK to `branches` | `tests/integration/test_organization_config.py` | **VERIFIED** |
| FR-ORG-03 | Organization settings | `GET/PUT Org/settings` | `services/organizations.py` | PK `organization_id`, range CHECKs | `tests/integration/test_organization_config.py`, `tests/integration/test_employees.py` | **VERIFIED** |
| FR-ORG-04 | Organization groups | `GET/POST/PATCH /org-groups` | `services/organizations.py` | `uq organization_groups.group_code` | `tests/integration/test_catalog_endpoints.py` | **VERIFIED** |
| FR-BR-01 | Branch CRUD + deactivation | `Org/branches…` | `services/catalog.py` | `uq (organization_id, branch_code_normalized)`, `uq (organization_id, id)` | `tests/integration/test_catalog_endpoints.py` | **VERIFIED** |
| FR-MST-01 | Nine organization-scoped masters | `Org/masters/{type}…` | `services/catalog.py` | `uq (organization_id, code_normalized)` per table, `uq (organization_id, rank)` on grades | `tests/integration/test_catalog_endpoints.py` | **VERIFIED** |
| FR-MST-02 | Configurable lookup lists | `Org/lookups/{listCode}…` | `services/catalog.py` | `uq (organization_id, list_code, value_code)` | `tests/integration/test_catalog_endpoints.py` | **VERIFIED** |
| FR-EMP-01 | Create DRAFT, complete, activate | `POST Org/employees`, `POST Org/employees/{id}/activate` | `services/employees.py` | staged NOT NULL via service (M vs M-A) | `tests/integration/test_employees.py` | **VERIFIED** |
| FR-EMP-02 | Edit with history preservation | `PATCH Org/employees/{id}` | `services/employees.py` | `row_version`, audit in same txn | `tests/integration/test_employees.py` | **VERIFIED** |
| FR-EMP-03 | Status transitions per matrix | `POST Org/employees/{id}/status-transitions` | `services/employees.py` | `employee_status_history` append-only | `tests/integration/test_employees.py` | **VERIFIED** (matrix, separated end-date and correction-window checks) |
| FR-EMP-04 | Effective-dated job changes | `POST Org/employees/{id}/job-changes`, `GET …/job-history` | `services/employees.py` | `employee_job_history` append-only, composite FKs | `tests/integration/test_employees.py` | **VERIFIED** |
| FR-EMP-05 | Rehire and inter-org transfer | create with `REHIRE`; `POST Org/employees/{id}/transfers` | `services/employees.py`, `services/transfers.py` | `previous_employment_record_id` FK (cross-org allowed) | `tests/integration/test_employees.py`, `test_transfers.py` | **VERIFIED** |
| FR-ID-01 | Encrypted, masked identifiers with duplicate detection | `Org/employees/{id}/identifiers…`, `POST Org/identifier-duplicate-check` | `services/identifiers.py`, `core/crypto.py` | `uq (employee_id, current_identifier_key)`, idx `(organization_id, identifier_type, value_blind_index)` | `tests/integration/test_identifiers.py` | **VERIFIED** |
| FR-EDU-01 | Multiple education records | `Org/employees/{id}/education…` | `services/profile_records.py` | result-type CHECKs, `start_year ≤ passing_year` | `tests/integration/test_profile_records.py` | **VERIFIED** |
| FR-PRV-01 | Previous employment with overlap flagging | `Org/employees/{id}/previous-employment…` | `services/profile_records.py` | `end ≥ start`, `last_drawn_ctc ≥ 0` | `tests/integration/test_profile_records.py` | **VERIFIED** |
| FR-FAM-01 | Family members | `Org/employees/{id}/family-members…` | `services/profile_records.py` | composite FK to employee | `tests/integration/test_profile_records.py` | **VERIFIED** |
| FR-NOM-01 | Nominations with share/minor rules | `GET Org/employees/{id}/nominations`, `PUT …/nominations/{type}` | `services/profile_records.py` | `share 0 < x ≤ 100`, `effective_to > effective_from` | `tests/integration/test_profile_records.py` | **VERIFIED** |
| FR-BNK-01 | Bank accounts, single primary | `Org/employees/{id}/bank-accounts…` | `services/bank.py` | `uq (employee_id, primary_from_key)` + employee row lock | `tests/integration/test_bank_accounts.py` | **VERIFIED** (concurrency race test outstanding) |
| FR-DOC-01 | Document lifecycle | `Org/employees/{id}/documents…`, `Org/document-categories…` | `services/documents.py`, `storage/local.py` | version-chain uniques, `single_current_key` | `tests/integration/test_documents.py` | **VERIFIED** (cross-org document-specific security test still outstanding) |
| FR-SAL-01 | Components and versioned templates | `Org/salary-components…`, `Org/salary-templates…` | `services/salary.py` | `uq (organization_id, component_code_normalized)`, `uq (template_id, version_number)` | `tests/integration/test_salary.py` | **VERIFIED** |
| FR-SAL-02 | Assignments from template or custom, with overrides | `POST Org/employees/{id}/salary-assignments` | `services/salary.py` | `uq (salary_assignment_id, component_code_snapshot)` | `tests/integration/test_salary.py` | **VERIFIED** |
| FR-SAL-03 | Schedule, cancel, correct with history | `…/confirm`, `…/cancel`, `…/replace`, `…/correct` | `services/salary.py` | `uq (employee_id, confirmed_from)` + lock + overlap check | `tests/integration/test_salary.py` | **VERIFIED** (O1–O4) |
| FR-SAL-04 | View current/historical/scheduled, compare | `GET …/salary-assignments`, `…/current`, `…/compare`, `…/salary-history` | `services/salary.py` | — | `tests/integration/test_salary.py` | **VERIFIED** |
| FR-SAL-05 | Optional maker-checker approval | `…/submit`, `…/approve`, `…/reject` | `services/salary.py` | `approved_by ≠ created_by/submitted_by` (service + CHECK) | `tests/integration/test_salary.py` (approval paths) | **VERIFIED** |
| FR-IDG-01 | Effective-dated ID rules per org/department | `Org/employee-id-rules…` | `services/employee_codes.py` | `uq (organization_id, rule_code_normalized)`, conflict unique | `tests/integration/test_id_rules.py` | **VERIFIED** |
| FR-IDG-02 | Atomic generation; preview without consuming | code issuance inside employee create/activate; `POST Org/employee-id-rules/preview` | `services/employee_codes.py` | `uq (organization_id, sequence_group_code, scope_key)`, registry unique | `tests/unit/test_code_rendering.py`, `tests/integration/test_id_rules.py`, `tests/integration/test_employees.py` | **VERIFIED** (AC-16 concurrency test outstanding) |
| FR-IDG-03 | Manual codes and reassignment | `POST Org/employees/{id}/code-reassignment` | `services/employee_codes.py` | registry `status` transition ASSIGNED→RETIRED only | `tests/integration/test_id_rules.py` | **VERIFIED** (service-level) |
| FR-SEC-01 | RBAC scoped by organization with field-level restrictions | all endpoints via `api/deps.py` | `core/permissions.py`, `services/access.py` | `uq user_organization_access active grant key` | `tests/unit/test_permissions.py`, `tests/unit/test_access_resolution.py`, `tests/security/test_tenant_isolation.py`, `tests/integration/test_identifiers.py` | **VERIFIED** |
| FR-PRIV-01 | Notices, exports, legal hold, erasure, retention batches | `Org/employees/{id}/privacy-notices`, `…/data-export`, `…/erasure-requests`, `Org/retention-batches` | `services/privacy.py` | consent/withdrawal CHECKs, two-person retention CHECK | `tests/integration/test_privacy.py` | **VERIFIED** |
| FR-AUD-01 | Append-only audit of changes and sensitive access | `GET Org/audit-logs`, `GET Org/employees/{id}/audit` | `audit/writer.py`, `services/audit_query.py` | no UPDATE/DELETE path; app DB grant restricted | `tests/integration/test_employees.py`, `tests/integration/test_identifiers.py` | **VERIFIED** |
| FR-SRC-01 | Org-scoped search, filter, sort, pagination | `GET Org/employees` | `services/employees.py`, `repositories/employee.py` | search indexes on `full_name_normalized`, code, email | `tests/integration/test_employee_search.py` | **VERIFIED** |

---

## 2. Acceptance criteria (§28)

Every criterion must be covered by an automated test (§28 preamble).

| ID | Scenario | Test | Status |
|---|---|---|---|
| AC-01 | Cross-organization isolation (403 on foreign path, 404 on foreign id, audited) | `tests/security/test_tenant_isolation.py` | **PARTIAL** — isolation, 403/404 distinction, privilege escalation and break-glass all verified; the per-endpoint sweep activates with the first `/orgs` router |
| AC-02 | Independent ID sequences per organization | `tests/integration/test_id_rules.py::test_ac02_*` | PLANNED |
| AC-03 | Department from another organization rejected (service **and** composite FK) | `tests/security/test_cross_org_references.py::test_ac03_*` | PLANNED |
| AC-04 | No duplicate employee codes (service **and** unique index) | `tests/integration/test_code_reassignment.py::test_ac04_*` | PLANNED |
| AC-05 | Template reuse across employees with snapshots | `tests/integration/test_salary.py` | **VERIFIED** |
| AC-06 | Permitted overrides only (`COMPONENT_NOT_OVERRIDABLE`) | `tests/integration/test_salary_assignments.py::test_ac06_*` | PLANNED |
| AC-07 | Template change does not alter history | `tests/integration/test_salary_templates.py::test_ac07_*` | PLANNED |
| AC-08 | Future revision does not change current salary; boundary becomes SUCCESSOR | `tests/integration/test_salary.py` | **VERIFIED** |
| AC-09 | Activation at effective date via date predicate, before and after the job | `tests/integration/test_salary_revisions.py::test_ac09_*` | PLANNED |
| AC-10 | No overlapping confirmed assignments (service **and** DB same-start unique) | `tests/integration/test_salary.py`, `tests/concurrency/test_salary_bank.py` | **VERIFIED** |
| AC-11 | Cancel future revision restores boundary, history untouched | `tests/integration/test_salary.py` | **VERIFIED** |
| AC-12 | History and comparison with no totals or deltas | `tests/integration/test_salary.py` | **VERIFIED** |
| AC-13 | Department-specific ID rule beats org-wide | `tests/integration/test_id_rules.py::test_ac13_*` | PLANNED |
| AC-14 | Org-wide fallback; `NO_APPLICABLE_ID_RULE` when none | `tests/integration/test_id_rules.py::test_ac14_*` | PLANNED |
| AC-15 | Scheduled rule change at the transition date; preview consumes nothing | `tests/integration/test_id_rules.py::test_ac15_*` | PLANNED |
| AC-16 | 50 concurrent creations, unique contiguous codes (NFR-04, OBJ-5) | `tests/concurrency/test_code_generation.py` | **VERIFIED** |
| AC-17 | Rule updates never change issued codes | `tests/integration/test_id_rules.py::test_ac17_*` | PLANNED |
| AC-18 | Document lifecycle: upload → verify → replace → download URL expiry, all audited | `tests/integration/test_documents.py` | **VERIFIED** (URL expiry value not asserted) |
| AC-26 | Nominee share/guardian validation and set end-dating | `tests/integration/test_profile_records.py` | **VERIFIED** (end-dating asserted by implementation; test covers totals/guardian) |
| AC-19 | Cross-organization document denial returns 404, audited | `tests/security/test_document_access.py::test_ac19_*` | PLANNED |
| AC-20 | Salary/bank/identity gated by role (RV, HRE, HRA) | `tests/security/test_field_level_access.py::test_ac20_*` | PLANNED |
| AC-21 | Audit record for every mutation; rows not updatable/deletable by the app role | `tests/integration/test_audit.py::test_ac21_*` | PLANNED |
| AC-22 | Deactivate master without breaking history (`MASTER_INACTIVE`, delete rejected) | `tests/integration/test_masters.py::test_ac22_*` | PLANNED |
| AC-23 | Rehire and transfer keep previous records intact | `tests/integration/test_transfers.py` | **VERIFIED** |
| AC-24 | Excluded modules absent from the API surface; no computed totals | `tests/acceptance/test_ac24_scope_guardrails.py` | PLANNED |
| AC-25 | Status transitions and correction window enforced by permission | `tests/integration/test_employee_status.py::test_ac25_*` | PLANNED |
| AC-26 | Nominee share/guardian validation and set end-dating | `tests/integration/test_profile_records.py` | **VERIFIED** |
| AC-27 | Aadhaar protection: masked everywhere, ciphertext at rest, reveal audited | `tests/integration/test_identifiers.py` | **VERIFIED** (ciphertext-at-rest assertion is indirect) |
| AC-28 | Backdating controls by permission and `backdated_salary_max_days` | `tests/integration/test_salary_revisions.py::test_ac28_*` | PLANNED |

---

## 3. Cross-entity constraints (§21.4)

Each of these needs **two** layers: a database constraint and a service check. The status
below reflects the database layer, which is now applied and tested by
`tests/integration/test_schema_constraints.py` (60 tests writing with SQLAlchemy Core, so
the service layer cannot mask a missing constraint). Where MySQL cannot express the rule
declaratively, the remaining half is named explicitly.

| ID | Constraint | Where enforced | Test | Status |
|---|---|---|---|---|
| C-01 | Employee in exactly one org; `organization_id` immutable | NOT NULL + no update path in `services/employees.py` | `test_schema_constraints.py` + `tests/security/test_tenant_isolation.py` (pending) | **VERIFIED** (database) |
| C-02 | Employee code unique per org across all records | `uq (organization_id, employee_code_normalized)` + registry unique | `test_schema_constraints.py::TestEmployeeCodeUniqueness` | **VERIFIED** (database) |
| C-03 | Master references same organization | composite FKs | `test_schema_constraints.py::TestOrganizationConsistency` | **VERIFIED** (database) |
| C-04 | Manager/HR contact/referrer same org, no self, no cycles | composite FK + `EMP-05`/`EMP-06` service checks under lock | `test_schema_constraints.py` + `tests/integration/test_employees.py` | **VERIFIED** |
| C-05 | Confirmed salary intervals non-overlapping | employee row lock + overlap query + `uq (employee_id, confirmed_from)` + nightly job | `test_schema_constraints.py::TestSalaryTimeline` + concurrency (pending) | **VERIFIED** (same-start); lock + nightly job pending service |
| C-06 | One current identifier per type (except previous PF) | `uq (employee_id, current_identifier_key)` generated column | `test_schema_constraints.py::TestIdentifierCurrency` | **VERIFIED** (database) |
| C-07 | At most one primary bank account at a time | employee row lock + single-primary service check + `uq (employee_id, primary_from_key)` | `test_schema_constraints.py::TestPrimaryBankAccount` + `tests/integration/test_bank_accounts.py` | **VERIFIED** (concurrency race test outstanding) |
| C-08 | Nomination shares total exactly 100.00 where required | `services/nominations.py` on set replacement | `tests/unit/test_nomination_rules.py` | PLANNED (service only by nature) |
| C-09 | One current document version per chain; single-current categories | `single_current_key` unique index | `tests/integration/test_documents.py` | PLANNED (service-maintained key) |
| C-10 | Template versions non-overlapping per template | publish-time check under lock + `uq (template_id, version_number)` | `tests/integration/test_salary_templates.py` | **VERIFIED** (version unique); overlap check pending service |
| C-11 | ID rules non-conflicting | publish-time conflict query + scope unique | `tests/integration/test_id_rules.py` | **VERIFIED** (conflict key); range check pending service |
| C-12 | Sequence values atomic | `INSERT IGNORE` + `SELECT … FOR UPDATE` on the sequence row | `tests/integration/test_id_rules.py` | **VERIFIED** (algorithm); AC-16 concurrency test outstanding |
| C-13 | Child document references belong to the same employee | composite FKs `(employee_id, document_id)` | `test_schema_constraints.py::TestOrganizationConsistency` | **VERIFIED** (database) |
| C-14 | Date ordering (inclusive `end ≥ start`, exclusive `to > from`) | CHECK constraints | `test_schema_constraints.py` (intervals, archival, cross-field) | **VERIFIED** (database) |
| C-15 | Monetary values ≥ 0 | CHECK constraints | `test_schema_constraints.py::TestTypedValueGroup` | **VERIFIED** (database) |

---

## 4. Salary timeline invariants (§18.1) and operations (§18.3–§18.6)

| ID | Invariant / operation | Implementation | Test | Status |
|---|---|---|---|---|
| INV-1 | Confirmed intervals pairwise disjoint | `services/salary_timeline.py` + DB (see C-05) | `tests/unit/test_salary_timeline.py` | PLANNED |
| INV-2 | `effective_to > effective_from` when not null | CHECK | `tests/integration/test_date_constraints.py` | PLANNED |
| INV-3 | `effective_from ≥ joining_date` | service validation | `tests/unit/test_salary_timeline.py` | PLANNED |
| INV-4 | With `employment_end_date` E: `from ≤ E`, `to ≤ E+1`, not null | `services/salary_timeline.py` O4 | `tests/integration/test_salary_employment_end.py` | PLANNED |
| INV-5 | Component values/snapshots of confirmed assignments immutable | no update path; correction creates a new row | `tests/integration/test_salary_revisions.py` | PLANNED |
| INV-6 | Only boundary/status/cancellation fields mutable on confirmed rows | `services/salary_timeline.py` | `tests/integration/test_salary_revisions.py` | PLANNED |
| O1 | Confirm revision — the ten-step algorithm incl. P/N resolution and boundary inheritance | `services/salary.py::_confirm` | `tests/integration/test_salary.py` | **VERIFIED** (core; worked example not all steps) |
| O2 | Cancel scheduled revision, predecessor resumes span when source is SUCCESSOR | `services/salary.py::cancel` | `tests/integration/test_salary.py` | **VERIFIED** |
| O3 | Correct ACTIVE/EXPIRED by cancel + insert `A′` with identical bounds | `services/salary.py::correct` | no dedicated test | **BUILT** |
| O4 | Employment end date set/cleared adjusts or restores boundaries | `services/salary.py::apply_employment_end` | no dedicated test | **BUILT** |

---

## 5. Employment cross-field constraints (§10.7)

| ID | Rule | Implementation | Status |
|---|---|---|---|
| EMP-01 | `employment_end_date ≥ employment_start_date` | CHECK + `services/employees.py` | **VERIFIED** |
| EMP-02 | `confirmation_date ≥ joining_date` | CHECK + service validation | **VERIFIED** (database); field-level check partial |
| EMP-03 | `probation_end_date ≥ joining_date` | CHECK + service validation | **VERIFIED** (database); field-level check partial |
| EMP-04 | `requires_end_date` employment types need an end date for activation | `services/employees.py` activation checks | **VERIFIED** |
| EMP-05 | Manager/HR contact/referrer ≠ self | CHECK + `services/employees.py` | **VERIFIED** (database); manager self rejected in service |
| EMP-06 | No reporting cycle (depth 50, chain locked `FOR UPDATE`) → `REPORTING_CYCLE` | `services/employees.py` | **VERIFIED** (unit-level; dedicated cycle test missing) |
| EMP-07 | All master references in the employee's organization → `INVALID_ORGANIZATION_REFERENCE` | composite FK + `MasterReferenceChecker` | **VERIFIED** |
| EMP-08 | Newly assigned masters must be ACTIVE → `MASTER_INACTIVE` | `MasterReferenceChecker` | **VERIFIED** |
| EMP-09 | `joining_date` change after activation needs `status_restricted` + reason, keeps salary ≥ date → `JOINING_DATE_CONFLICT` | service | PLANNED |
| EMP-10 | Setting end date with a later confirmed assignment → `SALARY_AFTER_EMPLOYMENT_END` | `salary_timeline.apply_employment_end()` | PLANNED |
| EMP-11 | Code never changes on attribute change | no code-write path outside issuance/reassignment | **VERIFIED** |
| EMP-12 | Job change date rules → `JOB_CHANGE_DATE_INVALID` | `services/employees.py` | **VERIFIED** |

---

## 6. Error codes (§24.3, §24.5)

All 48 codes live in `app/core/errors.py` as a single enum; the handler in
`app/core/exception_handlers.py` renders the §24.2 envelope. Coverage test:
`tests/unit/test_error_codes.py` asserts every spec code exists and maps to the documented HTTP
status, and `tests/integration/test_error_contract.py` asserts the envelope shape.

| HTTP | Codes | Status |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | **VERIFIED** |
| 401 | `UNAUTHENTICATED` | **VERIFIED** |
| 403 | `PERMISSION_DENIED`, `ORG_ACCESS_DENIED`, `DOCUMENT_ACCESS_DENIED`, `SELF_APPROVAL_NOT_ALLOWED` | **VERIFIED** (codes and rendering; enforcement in Phase 3) |
| 404 | `RESOURCE_NOT_FOUND` | **VERIFIED** |
| 409 | `DUPLICATE_EMPLOYEE_CODE`, `DUPLICATE_IDENTIFIER`, `SALARY_ASSIGNMENT_OVERLAP`, `ID_RULE_CONFLICT`, `ID_SEQUENCE_COLLISION_LIMIT`, `CONCURRENT_MODIFICATION`, `IDEMPOTENCY_KEY_REUSED`, `DOCUMENT_NOT_AVAILABLE` | PLANNED |
| 412 | `PRECONDITION_FAILED` | **VERIFIED** (code, rendering, `If-Match` parsing) |
| 413 | `FILE_TOO_LARGE` | PLANNED |
| 415 | `FILE_TYPE_NOT_ALLOWED` | PLANNED |
| 422 | `VALIDATION_FAILED`, `WARNINGS_REQUIRE_ACKNOWLEDGEMENT`, `INVALID_ORGANIZATION_REFERENCE`, `NO_APPLICABLE_ID_RULE`, `TEMPLATE_ARCHIVED`, `TEMPLATE_VERSION_NOT_EFFECTIVE`, `TEMPLATE_DRAFT_EXISTS`, `COMPONENT_NOT_OVERRIDABLE`, `INVALID_STATUS_TRANSITION`, `STATUS_CORRECTION_WINDOW_EXPIRED`, `BACKDATE_NOT_ALLOWED`, `SALARY_AFTER_EMPLOYMENT_END`, `SALARY_NOT_EDITABLE`, `SALARY_NOT_CANCELLABLE`, `SALARY_NOT_CORRECTABLE`, `APPROVAL_REQUIRED`, `REPORTING_CYCLE`, `NOMINEE_SHARE_TOTAL_INVALID`, `AGE_BELOW_ABSOLUTE_MINIMUM`, `FIELD_NOT_COLLECTED`, `FIELD_READ_ONLY`, `CONFLICTING_PERMANENT_ADDRESS`, `MASTER_INACTIVE`, `MASTER_IN_USE_BY_ID_RULE`, `ID_RULE_IMMUTABLE`, `JOINING_DATE_CONFLICT`, `JOB_CHANGE_DATE_INVALID`, `E-GSTIN-PAN` (as `GSTIN_PAN_MISMATCH`) | PLANNED |
| 429 | `RATE_LIMITED` | **VERIFIED** (code and `Retry-After`; limiter in Phase 3.6) |

---

## 6.1 Foundation verified in Phase 2

These are not functional requirements but the shared mechanisms every requirement depends on.
Each row has a test that was executed; see `docs/STATUS.md` for the run.

| Specification element | Implementation | Test | Status |
|---|---|---|---|
| §1.4 logical types (UUID `BINARY(16)`, UTC `DATETIME(6)`, decimals) | `app/db/types.py` | `tests/integration/test_migrations.py` | **VERIFIED** |
| §1.4 half-open intervals, inclusive `employment_end_date` | `app/core/clock.py` | `tests/unit/test_clock.py` | **VERIFIED** |
| §1.5 standard and append-only column sets, `row_version` locking | `app/db/mixins.py` | `tests/integration/test_migrations.py` | **VERIFIED** (schema); lost-update behaviour in Phase 3 |
| §5.2 permission codes, §5.3 matrix, `C` cells, §5.4 scoping and field sets | `app/core/permissions.py` | `tests/unit/test_permissions.py` | **VERIFIED** |
| §11.6 / §14.1 masking formats | `app/core/masking.py` | `tests/unit/test_crypto_and_masking.py` | **VERIFIED** |
| §20.2 audit action vocabulary, §20.3 redaction | `app/audit/` | `tests/unit/test_audit_events.py` | **VERIFIED** |
| §21.1 all 24 `V-*` rules, normalisation, severities | `app/validation/` | `tests/unit/test_validation_rules.py` | **VERIFIED** |
| §21.2 Verhoeff and GSTIN mod-36 checksums | `app/validation/checksums.py` | `tests/unit/test_validation_rules.py` (property tests) | **VERIFIED** |
| §21.3 warning acknowledgement protocol | `app/core/warnings.py` | `tests/unit/test_errors_and_warnings.py` | **VERIFIED** |
| §23.7 field encryption with AAD binding, group-scoped blind indexes | `app/core/crypto.py`, `app/integrations/keys.py` | `tests/unit/test_crypto_and_masking.py` | **VERIFIED** |
| §24.1 ETag / `If-Match` semantics | `app/schemas/common.py` | `tests/unit/test_pagination_and_etags.py` | **VERIFIED** (parsing); end-to-end in Phase 3 |
| §24.2 error envelope on every path incl. unhandled exceptions | `app/core/exception_handlers.py` | `tests/integration/test_api_contract.py` | **VERIFIED** |
| §26.4 sort allowlist, §26.5 signed cursor pagination | `app/schemas/common.py` | `tests/unit/test_pagination_and_etags.py` | **VERIFIED** |
| §18.8 READ COMMITTED, strict `sql_mode`, bounded lock wait | `app/db/session.py` | `tests/integration/test_migrations.py` | **VERIFIED** |
| §25.9 no personal data in logs, no internals in responses | `app/core/logging.py`, `app/core/exception_handlers.py` | `tests/integration/test_api_contract.py` | **VERIFIED** |
| §18 of the brief: probes expose no secrets | `app/main.py` | `tests/integration/test_api_contract.py` | **VERIFIED** |

---

## 7. Edge cases (§27)

E-01..E-36 are each covered by a named test. The mapping lives in
`tests/acceptance/test_edge_cases.py`, one test function per `E-nn`, so the spec table can be read
straight down against test names.

| Status |
|---|
| PLANNED |

---

## 8. Non-functional requirements (§3.2)

| ID | How addressed | Evidence | Status |
|---|---|---|---|
| NFR-01..03 | Tenant-scoped composite indexes, bounded pagination, deliberate eager loading | measurement pending; `[ASSUMPTION]` targets | PLANNED |
| NFR-04 | Sequence row lock, no I/O under lock | `tests/concurrency/test_code_generation.py` | PLANNED |
| NFR-05 | Composite FKs + nightly integrity checker | `jobs/integrity.py` | PLANNED |
| NFR-06 | MySQL compensating controls (ARCHITECTURE §7) | `tests/concurrency/test_salary_overlap_race.py` | PLANNED |
| NFR-07..09 | Deployment/ops concern | `docs/DEPLOYMENT.md` | PLANNED |
| NFR-10 | Audit written in the business transaction | `AuditWriter` shares the request session; `tests/security/test_tenant_isolation.py` asserts the break-glass audit row | **PARTIAL** — mechanism verified; per-domain events land with their services |
| NFR-11 | TLS terminated at the edge; HSTS documented | `docs/DEPLOYMENT.md` | PLANNED |
| NFR-12 | MFA flag on users; enforcement at the IdP | `docs/AUTH.md` | PLANNED |
| NFR-13 | Access token ≤15 min; refresh rotation and revocation | `tests/integration/test_auth_endpoints.py` | **VERIFIED** |
| NFR-14 | UI concern — out of scope for this backend | — | N/A |
| NFR-15 | Components, templates, lookups, categories, ID rules, policies all configurable | covered by the config endpoints above | PLANNED |
| NFR-16 | Coverage target 80% on domain services | number recorded in `docs/STATUS.md` | PLANNED |
| NFR-17 | UUIDv7 keys, cursor pagination, no unbounded loads | design; not load-tested | PLANNED |
| NFR-18..19 | Per-category size and MIME allowlists, magic-byte detection | `tests/security/test_upload_validation.py` | PLANNED |
| NFR-20 | Unicode/NFC normalisation incl. Indic scripts | `tests/unit/test_validation_rules.py` | PLANNED |
| NFR-21 | Data residency is deployment configuration; no region hardcoded | `docs/DEPLOYMENT.md` | PLANNED |














## 9. Addendum - leave (verified by a test that ran)

| Requirement | Implementation | Test |
|---|---|---|
| LEV-01, 6.6 formulas, 6.9 duration | `services/leave_rules.py` | `unit/test_leave_rules.py` |
| LEV-05/06, 6.4 | `services/leave_config.py` | `integration/test_leave_config_ledger.py` |
| 6.5, 6.13, LEV-INV-1/3 | `services/leave_ledger.py` | same; `integration/test_leave_jobs.py` (closed year) |
| LEV-20..29, 6.11, 6.12, INT-30..35 | `services/leave_applications.py`, `services/approvals.py` | `integration/test_leave_applications.py` |
| AC-44, AC-45, AC-46, AC-49 (partial), AC-57 | same | applications + `concurrency/test_leave.py` |
| AC-47 (rerun), AC-48, AC-58 | `services/leave_jobs.py` | `integration/test_leave_jobs.py` |
| LEV-30 punch conflict, LEV-31, INT-10..12 | - | **not built** (attendance-core pass) |
| AC-50, AC-51, AC-56, AC-59 | - | not built |


## 10. Addendum - attendance (verified by a test that ran)

| Requirement | Implementation | Test |
|---|---|---|
| 5.8 business date, 5.10 evaluation, 5.11 status, NFR-31 DST | `services/attendance_rules.py` | `unit/test_attendance_rules.py` |
| 5.3-5.5 ATT-03/04/05/06/09, 5.6 | `services/attendance_config.py`, `services/schedule_resolver.py` | `integration/test_attendance.py` |
| 5.7 ATT-13/15/16, 5.12, 5.13 ATT-17/18 | `services/attendance.py`, `services/attendance_records.py` | same |
| AC-42 | same | `test_attendance.py::test_finalized_records_protect_themselves_ac42` |
| AC-43, INT-10/11/12/13, LEV-30, LEV-31 | `AttendanceLeaveEffects`, `services/leave_applications.py` | `integration/test_attendance_leave.py` |
| ATT-13 races | employee row lock, unique external-event key | `concurrency/test_attendance.py` |
| 5.14 ATT-20..23, AC-40, AC-41 | `services/attendance_regularization.py` | `integration/test_attendance_regularization.py` |
| 5.12 day close, auto-finalize, 14.1 | `services/attendance_jobs.py`, `services/job_runs.py` | `integration/test_attendance_jobs.py` |
| INT-21 recalculation, AC-55 reconstruction | - | **not built** |

## 11. Addendum - onboarding (verified by a test that ran)

| Requirement | Implementation | Test |
|---|---|---|
| 4.3 task rules, 4.7 due dates, progress | `services/onboarding_rules.py` | `unit/test_onboarding_rules.py` |
| 4.4 template versions, selection, publish | `services/onboarding_templates.py` | `integration/test_onboarding.py::TestTemplates` |
| AC-29, AC-30 initiation, snapshot, ONB-02 | `services/onboarding.py` | `test_onboarding.py::TestInitiation` |
| AC-31, AC-32, AC-33 activation gating | `OnboardingService.assert_can_activate` / `after_activation` | `test_onboarding.py::TestCompletionAndActivation` |
| AC-34 tasks, dependencies, waiver, reopen | `services/onboarding.py` | `test_onboarding.py::TestTasks` |
| 4.10 acknowledgements, 4.12 submissions, ONB-10, ONB-11 | `services/onboarding.py`, `services/onboarding_submissions.py` | `test_onboarding_self_service.py` |
| 4.11 document tasks | `OnboardingService.link_document`, `on_document_verified` | `test_onboarding_documents.py` |
| 7.5 lifecycle, INT-36 | `services/lifecycle.py` | AC-50 / AC-51 tests |
| ONB-13 retention job, ONB-15 reveal, access provisioning | - | **not built** |
