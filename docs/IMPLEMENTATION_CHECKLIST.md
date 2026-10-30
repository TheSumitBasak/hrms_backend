# Implementation Checklist

Resumable work tracker. A session that loses context can resume from here. Statuses:
`[ ]` not started · `[~]` in progress · `[x]` done **and verified by a test that was actually run**.

A feature is only `[x]` when all ten conditions of the brief's "definition of done" hold. "Model
exists" is never sufficient.

---

## Phase 1 — Discovery and design

- [x] Read `EMPLOYEE_MANAGEMENT_SYSTEM.md` in full (2,616 lines, §1–§29 + Appendix A)
- [x] Inspect repository, git status, dependencies, migrations, env config
- [x] Load skills: `fastapi-rules`, `fastapi-architect`, `python-clean-architecture`,
      `owasp-security`, `test-driven-development`
- [x] Confirm database decision with user (MySQL 8 only) and v1 migration disposition (delete)
- [x] `docs/ARCHITECTURE.md` — layering, isolation, MySQL integrity strategy, ADR-001..006
- [x] `docs/DECISIONS.md` — 37 decisions: additions, contradictions, legal defaults, §29 defaults
- [x] `docs/TRACEABILITY.md` — requirement → endpoint/service/constraint/test matrix
- [x] This checklist

---

## Phase 2 — Foundation

**Complete and verified**: ruff clean, mypy strict clean (39 files), 537 tests passing.
Evidence in `docs/STATUS.md`.

- [x] `pyproject.toml` — dependencies, ruff, mypy, pytest configuration
- [x] `app/core/config.py` — validated settings; refuses placeholder secrets, undersized or
      identical keys, and unsafe staging/production configurations
- [x] `app/core/logging.py` — structured JSON logs, correlation ids, redaction filter
- [x] `app/core/errors.py` — all 44 spec error codes + 6 documented additions, one status map
- [x] `app/core/warnings.py` — 29 warning codes (16 spec-named, 13 conventional) + §21.3 protocol
- [x] `app/core/permissions.py` — §5.2 enum, §5.3 matrix, `C`-cell resolution, §5.4 scoping
- [x] `app/core/security.py` — bcrypt with pre-hash, local token issuer, OIDC seam (unconfigured)
- [x] `app/core/crypto.py` — AES-256-GCM with AAD binding, HMAC blind indexes, HKDF subkeys
- [x] `app/core/masking.py` — §11.6 masks, bank/document masks, audit-safe values
- [x] `app/core/clock.py` — UTC, org-timezone "today", half-open intervals, FY and `{YEAR}` tokens
- [x] `app/core/exception_handlers.py` — §24.2 envelope on every error path
- [x] `app/db/types.py` — UUIDv7 `BINARY(16)`, UTC `DATETIME(6)`, money/percentage decimals
- [x] `app/db/base.py` / `session.py` / `mixins.py` — base, unit of work, §1.5 columns,
      MySQL error translation, session guarantees
- [x] `app/validation/` — all 24 `V-*` rules, normalisers, Verhoeff + GSTIN mod-36
- [x] `app/audit/actions.py` / `events.py` — §20.2 vocabulary, §20.3 redaction
      (the persisting writer lands with `audit_logs` in Phase 3.1)
- [x] `app/schemas/common.py` — signed cursor pagination, sort allowlist, ETag/`If-Match`
- [x] `app/api/middleware.py` — correlation ids, security headers, no-store cache policy
- [x] `app/main.py` — application factory, `/health`, `/ready`, no import-time side effects
- [x] `app/cli.py` — `generate-keys`, `check-db`, `show-config`
- [x] `tests/conftest.py` — ephemeral test settings, `_test` schema interlock, MySQL fixtures
- [x] Alembic env wired to EMS metadata with a caller-overridable URL; verified against MySQL 8.4.11
- [x] `app/api/deps.py` — db session, current user, `OrgContext`, permission guards,
      `If-Match`, `Idempotency-Key`; settings resolved from the application, not the ambient
      environment (DEC-051)
- [x] Initial baseline migration — generated, hand-reviewed, applied to MySQL 8.4.11, and
      verified upgrade → downgrade → upgrade

---

## Phase 3 — Core functionality

> **Continuation status (2026-10-28).** All §24.4 groups, §25 privacy, §26 search, §10.9
> transfer/age exception, documents, salary and the §24.7 job functions are now implemented
> **and covered by executed tests**. Remaining: ETag/multi-process races, acceptance-suite split,
> coverage ≥80% and external deployment integrations. Evidence: `docs/STATUS.md` §0.


Each item below means: model + constraints + migration + schema + repository + service + endpoint +
tests, all together.

**Status of the persistence layer: complete for every domain below.** All 61 tables are
modelled with their §23.3 constraints and indexes, the baseline migration is applied, and 60
integration tests verify the database rejects direct inserts that violate C-02..C-07 and
C-13..C-15. What remains for each item is the repository, service, endpoint and behavioural
tests. Marked `[~]` accordingly.

### 3.1 Platform and access (§5, §24.4.8) — **complete**
- [x] `users`, `roles`, `permissions`, `role_permissions`, `user_organization_access`
- [x] `organization_role_permissions` (DEC-004), `refresh_tokens` (DEC-005), `idempotency_keys` (DEC-006)
- [x] Local login, refresh rotation with replay detection, immediate revocation on
      deactivation (§25.5)
- [x] `/auth/login|refresh|logout|me`, `/users`, `/users/{id}/org-access`, `/roles`,
      `/permissions`, `/break-glass` — 16 operations
- [~] DM reporting-tree scope and RV/RDR field sets — resolution and field allowlists
      implemented and unit-tested; applied when the employee endpoints land

### 3.2 Organizations (§7)
- [~] `organization_groups`, `organizations`, `organization_settings`, `organization_registrations`
- [ ] `organization_files`
- [x] `identifier_requirement_policies`, `nomination_type_configs` (service + endpoints + tests)
- [x] `identifier_type_configs` (platform, §21.2), `states` reference data
- [x] Entity-type → CIN/LLPIN/PAN applicability (§7.3) incl. `W-PAN-TYPE` / `W-CIN-TYPE`
- [x] Registration type rules, GSTIN↔PAN check (`E-GSTIN-PAN`), uniqueness scopes
- [x] Org creation seeds settings, lookups, document categories, employment types, policies
- [x] Lifecycle: deactivate/reactivate; organization response includes the registration summary; code-issuance immutability covered by the ID-rule lifecycle

### 3.3 Branches, masters, lookups (§8, §9, §10.2)
- [x] `branches` + derived registration fields written through `organization_registrations` (DEC-015)
- [x] Nine masters with common fields + master-specific fields (§9.2)
- [x] `lookup_values` with the 19 seeded lists
- [x] `employee_custom_statuses`
- [x] Deactivation rules, `MASTER_INACTIVE`, `MASTER_IN_USE_BY_ID_RULE`

### 3.4 Employee ID generation (§19) — before employees, which depend on it
- [x] `employee_id_generation_rules`, `employee_id_sequences`, `employee_code_registry`
- [x] Pattern parser/renderer (7 tokens, ≤30 chars, upper-case)
- [x] Rule selection algorithm (§19.5) and lifecycle/immutability (§19.3)
- [x] Conflict detection (§19.4) with MySQL compensating controls
- [x] Atomic generation under sequence row lock (§19.6), collision skip, 1,000-collision limit (AC-16 race test outstanding)
- [x] Preview (no locks, no writes), manual codes, reassignment with retirement

### 3.5 Employees (§10)
- [x] `employees`, `employee_addresses`, `employee_job_history`, `employee_status_history`
- [x] Create DRAFT, activation checks, M vs M-A field staging
- [x] Status transition matrix + correction window + `is_correction`
- [x] Job changes (effective-dated snapshots), EMP-01..EMP-12 incl. reporting-cycle detection (dedicated cycle test outstanding)
- [x] Age policy and age-exception maker-checker decision endpoint
- [x] Permanent-address derivation (the conflicting-input rejection is enforced by the request model)
- [x] Rehire with linked previous record; inter-organization transfer endpoint with selected categories
- [x] Archive/discard draft with code retirement

### 3.6 Identifiers (§11)
- [x] `employee_identifiers` with ciphertext, blind index, last four, key version
- [x] Requirement policies incl. `FIELD_NOT_COLLECTED`
- [x] Duplicate detection matrix (§11.5) incl. group scope and override
- [x] Masking formats (§11.6), reveal with reason + audit + rate limit
- [x] `POST /identifier-duplicate-check`

### 3.7 Profile sub-records (§12–§14)
- [x] `employee_education` with result-type rules
- [x] `employee_previous_employment` + payslip link table + overlap flag/review
- [x] `employee_family_members`
- [x] `employee_nominees` + `nomination_type_configs`, atomic set replacement, share totals
- [x] `employee_bank_accounts`: encryption, primary-account invariant, verify, close, reveal

### 3.8 Documents (§15)
- [x] `document_categories` + `document_category_mime_types` + 25 seeded categories
- [x] `employee_documents` with version chain and single-current key
- [x] Storage backend (private), signed upload/download, magic-byte detection, scanner hook (local backend)
- [x] Verify / reject / archive, restricted authorization, expiry states
- [x] `GET Org/documents/expiring`

### 3.9 Salary reference data (§16–§18)
- [x] `salary_components` with typed-value group and immutability rules
- [x] `salary_templates`, `salary_template_versions`, `salary_template_components`, publish rules
- [x] `employee_salary_assignments`, `employee_salary_components`, `salary_revision_history`
- [x] O1 confirm (§18.3) core algorithm with boundary inheritance
- [x] O2 cancel, O3 correct, O4 employment-end boundary adjustment (dedicated O3/O4 tests outstanding)
- [x] Approval maker-checker, backdating controls, `preview-timeline` dry run
- [x] History, as-of reads by date predicate, comparison with no arithmetic

### 3.10 Audit and search (§20, §26)
- [~] `audit_logs` append-only (no update/delete path), integrity hash chain
- [x] `GET Org/audit-logs`, `GET Org/employees/{id}/audit`, 366-day range cap
- [x] Employee search: filters, sort allowlist, signed cursor pagination, total, identifier and indirect reporting search

### 3.11 Privacy (§25)
- [x] `employee_privacy_notices`, `legal_hold`
- [x] Encrypted data export, erasure requests, legal hold
- [x] Retention batch propose/two-person approve/execute

### 3.12 Jobs (§24.7)
- [ ] Salary status refresh, ID-rule status refresh, pending-upload cleanup, quarantine purge,
      retention purge, nightly integrity checker

---

## Phase 4 — Verification

- [x] Alembic baseline applied to a real MySQL 8 database; migration script reviewed by hand
- [x] `ruff check` / `ruff format --check` clean
- [x] `mypy app` clean
- [x] Unit tests green, with counts recorded (612)
- [x] Integration tests green against MySQL 8 (208)
- [x] Security/adversarial tests green (21; document-specific cross-org sweep still outstanding)
- [x] Concurrency tests green for 50-way code generation, salary overlap and primary bank; ETag races still outstanding
- [ ] All 28 acceptance criteria AC-01..AC-28 automated and green
- [x] Coverage measured: 67% on `app/services` (target 80% not met)
- [~] Targeted security/architecture review performed and recorded in `STATUS.md`; full-review skill pass not run
- [~] Manual route/service structural review clean; fastapi-architect skill audit not run

---

## Phase 5 — Documentation and handover

- [x] `README.md` — setup and execution
- [x] `docs/ARCHITECTURE.md`
- [x] `docs/DATABASE.md` — schema and migration guide, MySQL specifics
- [x] `docs/API.md` — usage, envelopes, pagination, idempotency, ETags
- [x] `docs/AUTH.md` — authentication and authorization model
- [x] `docs/CONFIGURATION.md` — every setting, key management, secret rotation
- [x] `docs/TESTING.md` — how to run each suite
- [x] `docs/DECISIONS.md`
- [x] `docs/TRACEABILITY.md` kept current with evidence (section 1 reconciled; later sections still show the plan)
- [x] `docs/DEPLOYMENT.md` — ASGI config, checklist, backup/restore
- [x] `docs/STATUS.md` — implementation status, measured results, known limitations
















