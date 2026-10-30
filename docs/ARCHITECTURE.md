# EMS Backend — Architecture Overview

Source of truth for requirements: `EMPLOYEE_MANAGEMENT_SYSTEM.md` (referred to below as "the spec";
section references like §18.3 point into it). This document describes *how* the spec is realised in
code. Where the two disagree, the spec wins and this document is a defect.

---

## 1. Scope boundary (non-negotiable)

The EMS is a **system of record for employee information**. It is not an HRMS.

Per §4.2/§4.3 and AC-24 the backend MUST NOT contain payroll, payslip, tax/TDS, PF/ESIC/PT/LWF
contribution, attendance, leave, recruitment, performance, training, expense, asset, F&F or
accounting logic, and MUST NOT compute net/gross pay, component sums, deltas, percentages of a base,
projections or cost-to-company. Salary data is stored **as entered**; `PERCENTAGE` component values
are display labels and are never applied to an amount.

This is enforced structurally, not just by convention:

- there is no arithmetic over `employee_salary_components.value_*` anywhere in `app/services/salary*`;
- the comparison endpoint (§17.6) emits `change_type` labels only (`ADDED/REMOVED/CHANGED/UNCHANGED`);
- an acceptance test asserts the OpenAPI document exposes no out-of-scope paths and greps the salary
  package for arithmetic over value columns.

**Continuation status (2026-10-28):** the salary package and its comparison endpoint do not
exist yet, so the last three bullets describe the constraint that will apply when §16–§18 is
implemented, not a currently executed test. No payroll, tax, attendance or leave code was added
in the continuation pass.

---

## 2. Technology baseline

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.13+ (dev venv runs 3.14.2) | `.python-version` pins the local toolchain |
| Web | FastAPI + Starlette | `/api/v1` prefix (§24.1) |
| Validation | Pydantic v2 + pydantic-settings | request/response contracts only |
| ORM | SQLAlchemy 2.x typed (`Mapped`/`mapped_column`) | **synchronous** — see ADR-001 |
| Migrations | Alembic | single linear chain, EMS baseline |
| Database | **MySQL 8.0+ only** | see ADR-002 and §7 below |
| Crypto | `cryptography` (AES-256-GCM, HMAC-SHA256) | §23.7; no hand-rolled schemes |
| Passwords | `bcrypt` | local accounts only; OIDC is the primary path (A-04) |
| Tests | pytest + httpx/TestClient | unit / integration / security / concurrency / acceptance |
| Lint & types | ruff + mypy | commands in `docs/TESTING.md` |
| Server | uvicorn (dev), gunicorn + uvicorn workers (prod) | `docs/DEPLOYMENT.md` |

---

## 3. Layering

Dependencies point **downwards only**. A module never imports from a layer above it.

```
api/v1/endpoints      HTTP only: parse, delegate, serialise. No business rules, no queries.
        |
      services        Use cases, invariants, transaction boundaries, audit emission.
        |
    repositories      Data access. Every org-scoped query is tenant-filtered here.
        |
      models          SQLAlchemy ORM. Tables, constraints, generated columns.
```

Cross-cutting packages (importable by any layer above them):

| Package | Responsibility |
|---|---|
| `core/` | config, logging, errors, permissions, security, crypto, clock, warning protocol |
| `db/` | declarative base, session/unit of work, custom column types, standard-column mixins |
| `validation/` | the §21.1 `V-*` rule dictionary, normalisers, checksums (Verhoeff, GSTIN mod-36) |
| `audit/` | append-only audit writer; enrolled in the caller's transaction (NFR-10) |
| `storage/` | `StorageBackend` protocol + private local / S3-compatible implementations |
| `integrations/` | `MalwareScanner`, `KeyProvider` boundaries (A-05, A-06) |
| `schemas/` | Pydantic request/response models, masking-aware serialisers |
| `jobs/` | the §24.7 scheduled jobs, invocable from CLI/cron; idempotent |

**Rules enforced in review (the `fastapi-rules` checklist):**

- route handlers contain no `Session` use, no queries, and no conditionals expressing business rules;
- services receive `Session` by constructor injection and never import `get_db`;
- exactly one `get_*_service` provider per service, all of them in `app/api/deps.py`;
- ORM objects are never returned from an endpoint — always a Pydantic model or an explicit dict
  produced by a field-policy serialiser;
- utils are pure and stateless.

Deviation from `fastapi-rules`: that skill assumes a `src/{project}/` or `apps/`+`packages/` monorepo
layout and `AsyncSession`. This project keeps the pre-existing `app/` package layout (also prescribed
by the build brief §6) and synchronous sessions (ADR-001). All other rules apply verbatim; the Path
Map is translated `src/{project}/` → `app/`.

---

## 4. Domain modules

Implemented in dependency order (see `docs/IMPLEMENTATION_CHECKLIST.md`):

1. **platform** — users, roles, permissions, org access grants, break-glass, auth tokens (§5, §24.4.8)
2. **organizations** — organizations, groups, settings, registrations, identifier and nomination configs (§7)
3. **branches** (§8) and **masters/lookups** — nine master types, `lookup_values`, custom statuses (§9, §10.2)
4. **employees** — core record, addresses, lifecycle/status, job history, rehire and transfer (§10)
5. **identifiers** — encrypted identifiers, blind-index duplicate detection, mask/reveal (§11)
6. **profile sub-records** — education, previous employment, family, nominees, bank accounts (§12–§14)
7. **documents** — categories, upload/complete flow, versions, verify, archive, presigned download (§15)
8. **salary** — components, versioned templates, assignments, revision timeline O1–O4, compare (§16–§18)
9. **employee codes** — ID rules, selection, sequences, registry, preview, manual, reassignment (§19)
10. **audit and search** — audit log reads, employee search/filter/sort/cursor pagination (§20, §26)
11. **privacy** — notices/consent, data export, erasure requests, retention purge (§25)

**Implementation status after the 2026-10-28 continuation pass:** modules 1–6 (organizations,
branches, masters/lookups, employees, identifiers, bank accounts) and modules 9–10 (employee
codes, audit reads) are implemented; employee search is basic. Profile sub-records other than
identifiers/bank (education, previous employment, family, nominees), documents, salary, privacy
operations and scheduled jobs are still not implemented. Tests are in
`tests/{unit,integration,security}`; see `docs/STATUS.md` §0.

The service/repository split introduced by this pass is: `services/organizations.py` and
`services/org_config.py` (tenant root and configuration), `services/catalog.py` (branches,
masters, lookups, custom statuses), `services/employees.py` (lifecycle and job history),
`services/employee_codes.py` (rendering, selection, issuance), `services/identifiers.py` and
`services/bank.py` (encrypted sensitive fields), and `services/audit_query.py` (read-only).
Endpoints remain thin and receive collaborators through `app/api/deps.py`.

---

## 5. Multi-organization isolation (§6, §23.6)

Isolation is a **security boundary**, enforced at four layers. MySQL has no row-level security, so
the spec's MySQL variant applies: *"equivalent filter enforced in a mandatory repository layer;
automated tests attempt cross-organization access for every endpoint"* (§23.6).

| Layer | Mechanism |
|---|---|
| 1. Transport/API | every org-scoped path is `/api/v1/orgs/{org_id}/…`; `OrgContext` is resolved from the path and the caller's **active grants**, never from a request body |
| 2. Service | `require_permission(...)` plus object checks: a loaded row whose `organization_id` differs from the context is reported as `404 RESOURCE_NOT_FOUND` (no existence disclosure, §24.3) |
| 3. Repository | `OrgScopedRepository` cannot be constructed without an `OrgContext`, and every `select()` it issues carries `WHERE organization_id = :ctx`. Services never build org-scoped queries by hand |
| 4. Database | composite FKs `(organization_id, child_id) → parent(organization_id, id)` for every relationship in §6.3; every org-scoped table carries `UNIQUE (organization_id, id)` so it can be a composite-FK target (§23.1) |

`403 ORG_ACCESS_DENIED` means "you hold no grant for this `{orgId}`". `404 RESOURCE_NOT_FOUND` means
"this id is not in the org you do have access to" — including when the row exists in another org.
Both are audited.

Cross-organization references are permitted **only** for the three cases in §6.4
(`employees.previous_employment_record_id`, group-scoped duplicate detection, platform tables).

---

## 6. Authentication, authorization, field-level access (§5)

- **Identity:** OAuth2/OIDC bearer tokens are the primary mechanism (§24.1, A-04). No identity
  provider is configured in this environment, so `core/security.py` exposes a verification boundary
  (`TokenVerifier`) with a local implementation for local accounts and tests; the OIDC JWKS verifier
  is wired but inert until `OIDC_*` settings are supplied. See `docs/AUTH.md`.
- **Access tokens** are short-lived (≤15 min, NFR-13). Refresh tokens are opaque, hashed at rest,
  rotated on use, and **revoked immediately** on user deactivation (§25.5).
- **Permissions, not role names.** `core/permissions.py` holds the §5.2 `Permission` enum and the
  §5.3 role→permission matrix as the single source of truth. Endpoints declare
  `Depends(require(Permission.EMPLOYEE_VIEW))`. No endpoint branches on a role code except where the
  spec itself scopes by role (the DM reporting tree, the RV/RDR field sets).
- **"C" cells** of §5.3 (not granted by default, enablable per organization by an OA) are stored in
  `organization_role_permissions` — a documented addition, see DEC-004.
- **Object-level checks** happen for every protected resource, not only on list endpoints.
- **Field-level filtering** is applied during serialisation by `schemas/field_policy.py`: fields the
  caller may not see are **omitted from the payload**, never returned as `null` (§5.5). Masked fields
  render the §11.6 formats from `value_last_four` with **no decryption**.
- **Maker-checker:** salary approval requires `approved_by` to differ from both `created_by` and
  `submitted_by` (§17.4, `SELF_APPROVAL_NOT_ALLOWED`); the age-exception decider must differ from the
  requester (§10.4).
- **Break-glass:** SSA has *no* employee-data access by default. `POST /break-glass` mints a
  time-bound grant (≤240 min) with a mandatory reason (≥20 chars); the grant and **every access made
  under it** are audited into the target organization's log (§5.3 note 1).

---

## 7. MySQL-only integrity strategy (ADR-002/ADR-003)

The deployment target is MySQL 8.0+, so the §23.5 PostgreSQL exclusion constraints and §23.6 RLS are
unavailable. The spec prescribes the substitutes used here (§18.8, §23.1, §23.6, NFR-06):

| Invariant | PostgreSQL (spec) | MySQL implementation here |
|---|---|---|
| INV-1 confirmed salary intervals disjoint (C-05) | `EXCLUDE USING gist` | `SELECT … FOR UPDATE` on the employee row (lock order employee → assignments), in-transaction overlap query, `UNIQUE (employee_id, confirmed_from)` on a generated column blocking same-start, nightly integrity job reporting any overlap as P1 |
| One primary bank account per date (C-07) | exclusion constraint | employee row lock, in-transaction interval check, `UNIQUE (employee_id, primary_window_key)`, nightly job |
| ID-rule conflicts (C-11) | exclusion constraint | publish-time conflict query under lock, `UNIQUE (organization_id, dept_scope_key, priority, effective_from)`, nightly job |
| Template versions non-overlapping (C-10) | exclusion constraint | publish-time check under template lock, `UNIQUE (template_id, version_number)`, nightly job |
| One current identifier per type (C-06) | partial unique index | `UNIQUE (employee_id, current_identifier_key)`, where the generated key is `NULL` unless `is_current` and the type is not `PREVIOUS_PF_MEMBER_ID` |
| Employee code uniqueness incl. archived (C-02) | `unique(org, upper(code))` | `UNIQUE (organization_id, employee_code_normalized)` on a stored generated column, plus the independent `employee_code_registry` unique index |
| Case-insensitive master codes | functional index | stored generated column `code_normalized` |
| Official email partial uniqueness | partial unique index | generated column keyed to `NULL` for separated/archived rows, covering `ORGANIZATION` scope; `GROUP` and `NONE` scopes are enforced in the service, see DEC-045 |
| Single current document version per chain (C-09) | partial unique index | service-maintained nullable `single_current_key` plus `UNIQUE (employee_id, document_category_id, document_type, single_current_key)` |
| Tenant isolation | RLS policies | mandatory repository filter, composite FKs, adversarial tests per endpoint |
| Name search (§26.2) | `pg_trgm` GIN | `full_name_normalized` prefix index plus a MySQL `FULLTEXT` index for word matching |

**Accepted trade-off (stated once).** MySQL cannot make overlap prevention purely declarative, so
NFR-06's "zero overlapping confirmed assignments" rests on the row lock plus the same-start unique
index plus the nightly checker — exactly the arrangement §18.8 permits. The tests in
`tests/concurrency/` exercise it. Moving to PostgreSQL would make it declarative; the models are
written dialect-neutrally so that remains possible without a rewrite.

Other MySQL mappings (§1.4): `UUID → BINARY(16)` (UUIDv7, time-ordered, index-friendly),
`TIMESTAMP → DATETIME(6)` stored in UTC, `BOOLEAN → TINYINT(1)`, money `DECIMAL(15,2)`, component
percentage `DECIMAL(7,4)`, shares `DECIMAL(5,2)`, `BYTES → VARBINARY`, native `JSON`. Enums are
`VARCHAR(40)` with a `CHECK`, holding upper-snake-case codes.

---

## 8. Transactions, concurrency, idempotency

- One request is one transaction, opened by the `get_db` dependency and committed by the service's
  unit of work. `db/session.py` exposes an explicit `unit_of_work()` context manager; repositories
  never commit.
- Audit rows are written through `audit/writer.py` **inside the caller's transaction**, so a business
  change and its audit record commit or roll back together (NFR-10, AC-21).
- No transaction performs network I/O. Storage, malware scanning and KMS network calls happen before
  the transaction opens (§24.7); only local AES/HMAC operations against a cached data key run inside
  it.
- **Lock order** is fixed to avoid deadlocks: `employees` → `employee_salary_assignments`.
  `employee_id_sequences` is locked alone and held for under 200 ms with no external calls (§19.7).
- **Optimistic locking:** `row_version` is returned as a strong `ETag` on GET; `PATCH`/`PUT` and
  state-changing POSTs on existing resources require `If-Match` and return `412 PRECONDITION_FAILED`
  when stale (§20.4, §24.1, E-30).
- **Idempotency:** `Idempotency-Key` (UUID) is required on the create endpoints for employees, salary
  assignments and documents. Keys are stored with a request-payload fingerprint; a replay within 24 h
  returns the original response, and the same key with a different payload returns
  `409 IDEMPOTENCY_KEY_REUSED` (§24.1, §24.5, E-03).
- Serialization and lock-wait failures surface as retryable `409 CONCURRENT_MODIFICATION`.

---

## 9. Sensitive data (§11, §14, §23.7, §25)

- `[E]` fields (identifier values, bank account numbers, document numbers) are stored only as
  AES-256-GCM ciphertext with a `key_version`. The AAD binds organization, employee and field name so
  ciphertext cannot be relocated between rows or tenants.
- Keys come from a `KeyProvider` (`integrations/keys.py`). The envelope scheme follows §23.7: a DEK
  per organization wrapped by a master key held in a KMS. No KMS exists in this environment, so an
  env-backed provider serves local development and tests while the KMS provider remains an explicit,
  documented boundary (`docs/CONFIGURATION.md`). **No key is ever hardcoded or committed.**
- **Blind indexes** are HMAC-SHA256 over the normalised value using a key *separate from the
  encryption key*, scoped per organization group (or per organization when ungrouped), so cross-group
  matching is impossible by construction (§23.7, §11.5 final column).
- Masked by default everywhere. Full values come only from the explicit reveal endpoints, which
  require the reveal permission and a reason (≥10 chars), return `Cache-Control: no-store`, are
  rate-limited and are audited **without the value** (§11.6, §14.2).
- Full Aadhaar never appears in a list, detail, export, log, audit record or error message, and is
  never a key, a default search field or part of an employee code (§11.6).
- Audit payloads carry masked before/after values for sensitive fields (`"***changed***"` or last
  four) and never carry secrets, tokens, keys or file contents (§20.3).
- Application logs are structured JSON with a correlation id and **no personal data**; request bodies
  are never logged (§25.9).

---

## 10. Documents (§15)

File bytes live in private object storage, never in a business table and never under a public static
directory. Metadata lives in `employee_documents`; versions are rows in the same table linked by
`supersedes_document_id` (the §15.3 design decision).

Upload follows the three-step flow of §15.4. `POST …/documents` validates permission, category,
declared type and size, then returns a short-lived upload URL bound to size and content type.
`POST …/documents/{id}/complete` verifies the object exists, enforces **magic-byte** MIME detection
against the category allowlist (never the extension or the client-supplied MIME type), computes
SHA-256, runs the scanner, and then flips the row to `AVAILABLE`, `QUARANTINED` or `FAILED`.
Filenames are sanitised (path separators and control characters stripped). Storage keys are
`org/{org}/emp/{emp}/{doc}`, contain no PII and are never returned by the API. Downloads are
five-minute presigned GETs with `Content-Disposition: attachment`, and are audited (always for
restricted categories).

---

## 11. Observability

Structured JSON logs with `request_id`/`X-Request-Id` correlation, request-latency and DB-error
counters, a `/health` liveness endpoint and a `/ready` readiness endpoint that expose a status and
nothing sensitive, and explicit timeouts on every external call. Permission checks and sensitive data
are **not** cached beyond the 60-second permission cache the spec mandates (§5.5), which is keyed per
`(user, organization)` and invalidated on any grant change.

---

## 12. Architecture decision records

**ADR-001 — Synchronous SQLAlchemy with `def` endpoints.**
*Context:* §18.3 and §19.6 require `SELECT … FOR UPDATE` serialisation, strict single-transaction
semantics and deterministic lock ordering, and the brief forbids mixing sync DB calls into async
handlers. *Decision:* use synchronous SQLAlchemy 2.x sessions and `def` endpoints, which FastAPI
executes in a worker threadpool. *Consequences:* no event-loop blocking; simpler and lower-risk
transactional code; deviates from the `AsyncSession` assumption in `fastapi-rules`; throughput is
bounded by the threadpool and DB pool, sized in `docs/DEPLOYMENT.md`.

**ADR-002 — MySQL 8.0+ is the only supported dialect.**
*Context:* user decision; the spec permits MySQL with compensating controls. *Decision:* target MySQL
8 exclusively while keeping models dialect-neutral so PostgreSQL stays reachable later.
*Consequences:* §7 above; NFR-06 is upheld by lock + unique + nightly checker rather than
declaratively.

**ADR-003 — Emulate partial and exclusion constraints with stored generated columns.**
See the table in §7. *Consequences:* the constraints live in the database rather than only in
application code, which the spec requires ("application validation alone is insufficient", §18.8).

**ADR-004 — UUIDv7 primary keys stored as `BINARY(16)`.**
Per §1.4. Time-ordered keys keep InnoDB clustered-index inserts sequential at the one-million-record
scale of NFR-17, and a local generator avoids a new dependency.

**ADR-005 — Explicit view builders for field-level permissions.**
Rather than a reflective serialiser, each resource has a function mapping
`(row, permission_set, role_set) → dict` that *omits* disallowed keys. §5.5 requires omission rather
than nulling, and an explicit mapping is auditable and directly testable in a way a generic
mechanism is not.

**ADR-006 — Reference data ships as seed data.**
`states` with GST state codes, ISO country and currency lists are seeded rather than hardcoded in
conditionals, so §7.1/§11.4 validation stays configuration (NFR-15).
