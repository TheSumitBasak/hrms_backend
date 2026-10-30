# Requirements Decision Log, Assumptions and Open Items

Companion to `docs/ARCHITECTURE.md`. Records every place where the spec was ambiguous,
self-contradictory, silent, or deferred — and exactly what the implementation does about it.

Status legend:

| Status | Meaning |
|---|---|
| RESOLVED | Decided and implemented; no sign-off needed (editorial fix or spec-supported reading) |
| ASSUMED | Implemented under a stated technical assumption; **product owner should confirm** |
| LEGAL | Blocked on legal/compliance sign-off; a safe default is implemented |
| DEFERRED | Spec's own open question (§29.2); the spec's stated default is implemented |

---

## A. Environment and platform decisions

| ID | Item | Decision | Status |
|---|---|---|---|
| DEC-001 | Database dialect | **MySQL 8.0+ only** (user decision). §23.5 exclusion constraints and §23.6 RLS are replaced by the compensating controls the spec itself prescribes (§18.8, §23.1, §23.6). Trade-off recorded once in `ARCHITECTURE.md` §7. | RESOLVED |
| DEC-002 | Pre-existing v1 migrations | The four Alembic revisions describing the deleted v1 "Keka-style HRMS" schema (payroll, attendance, leave, expense — all forbidden by §4.2/AC-24) were **deleted** on user instruction, and a fresh EMS baseline revision was generated. No database was dropped or reset. | RESOLVED |
| DEC-003 | Sync vs async | Synchronous SQLAlchemy with `def` endpoints (ADR-001). | RESOLVED |

---

## B. Documented additions to the schema

The spec names these capabilities but does not define a table for them. Each is the minimum needed to
satisfy an explicit requirement; none invents business behaviour.

| ID | Addition | Required by | Justification | Status |
|---|---|---|---|---|
| DEC-004 | `organization_role_permissions` | §5.3 "**C** = not granted by default, may be enabled per organization by an OA" | The permission matrix has 14 `C` cells (HRE remarks, HRE bank.verify, HRE bgv.view, HRE salary.view, HRE salary.revision_create, HRE document.verify, RDR identity.reveal, OA salary.revision_approve, HRA idrule.manage, HRA employee.code_manual, HRA master.manage, …). Storing them per organization keeps this configuration rather than code (NFR-15). | RESOLVED |
| DEC-005 | `refresh_tokens` | §24.1 bearer auth, §25.5 "user deactivation revokes refresh tokens immediately", NFR-13 | Refresh lifecycle and immediate revocation are mandated but no table is specified. Stores a hash of the token only, never the token. | RESOLVED |
| DEC-006 | `idempotency_keys` | §24.1, §24.5 `IDEMPOTENCY_KEY_REUSED`, §19.7, E-03 | 24-hour replay of the original response and detection of key reuse with a different payload both require server-side storage of key, payload fingerprint and response. | RESOLVED |
| DEC-007 | `break_glass_grants` | §5.3 note 1, §24.4.8 `POST /break-glass` | §23.2 reuses `user_organization_access.expires_at` for break-glass. Implemented that way, plus a dedicated access-event audit action so "every access during it" (§5.3 note 1) is attributable. No separate grant table. | RESOLVED |
| DEC-008 | `integrity_findings` | NFR-05, NFR-06, §18.8 "nightly integrity job that reports any overlap as a P1 defect", §24.7 integrity checker | The nightly checker must *report*; a table gives the report somewhere durable and queryable. | RESOLVED |
| DEC-009 | `employee_change_requests` | §5.6 optional self-view: "Changes are stored as pending and applied only after HRA/HRE acceptance (audited)" | Pending self-service change requests need storage. Only the five fields §5.6 lists are accepted. Feature is off by default (`enable_employee_self_view = false`). | RESOLVED |
| DEC-045 | Official-email uniqueness is enforced by a generated column keyed to `NULL` for archived and separated records | §10.5 `official_email_uniqueness_scope`, §23.4 | §10.5 makes the scope configurable (`ORGANIZATION`, `GROUP`, `NONE`) and §23.4 restricts uniqueness to non-separated, non-archived records. A generated column cannot read another table, so the database constraint covers the **`ORGANIZATION` scope with the built-in separated statuses**; the `GROUP` and `NONE` scopes and organization-defined separated statuses (§10.2) are enforced in the service, which has the settings and custom-status rows to hand. | RESOLVED |
| DEC-010 | `organization_notice_versions` | §25.3 `notice_version` "Matches an organization-published notice version"; "Notice text stored as an organization file" | A published-version registry is needed for that validation to mean anything. | ASSUMED |

---

## C. Spec contradictions and editorial defects

| ID | Location | Problem | Resolution | Status |
|---|---|---|---|---|
| DEC-011 | §17.1, `employee_id` row | The field table literally reads `employee status ∉ {DRAFT when confirming? see note}` — an unresolved authoring note. | Implemented per the row's own Notes column: a **DRAFT** salary assignment may be created for a DRAFT employee; **confirmation** requires only that the employee is not archived. Confirmation for a DRAFT employee is therefore permitted, since §17.1 `effective_from ≥ joining_date` is checkable in DRAFT and §10.2 does not list salary as an activation prerequisite. | ASSUMED |
| DEC-012 | §24.4.6, salary table | The create-assignment endpoint appears **twice**: a truncated row ("…effective_") immediately followed by the complete row. | Treated as one endpoint; the complete row is authoritative. | RESOLVED |
| DEC-013 | §7.2, `logo_document_id` | Validation reads "PNG/JPEG/SVG-sanitized not allowed → PNG/JPEG only", which contradicts itself. | Implemented as **PNG and JPEG only**, ≤1 MB. SVG is rejected (consistent with §15.5, which always blocks `.svg`). | RESOLVED |
| DEC-014 | §12.2, `overlap_flag` | Marked `DER` (derived) but also appears as a row field, while §23.3 defines no column for it. | Computed on read from the employee's other previous-employment intervals and `joining_date`. Only `overlap_review_status`/`overlap_reviewed_by`/`overlap_reviewed_at` are stored. | RESOLVED |
| DEC-015 | §8.1 | `gstin`, `shops_establishments_registration_number`, `factory_license_number` are `DER` on `branches` but the create/edit API accepts them. | Per the §8.1 design decision: not stored on `branches`; the service writes/updates the corresponding `organization_registrations` row **in the same transaction** and the branch response derives them. | RESOLVED |
| DEC-016 | §26.2 vs §20.2 | §26.2 mandates auditing identifier search as `IDENTIFIER_SEARCHED`, but that action is absent from the §20.2 audited-event vocabulary. | `IDENTIFIER_SEARCHED` added to the audit action vocabulary. | RESOLVED |
| DEC-017 | §15.2 vs §23.3 | Single-current enforcement is described as "enforced by service + trigger", which is not a declarative constraint. | Service-maintained nullable `single_current_key` plus a real unique index (ARCHITECTURE §7). No trigger, so the logic stays in one reviewable place. | RESOLVED |
| DEC-018 | Warning codes | Sixteen `W-*` codes are referenced across §7–§15 (`W-ORG-NAME`, `W-PAN-TYPE`, `W-CIN-TYPE`, `W-AGE-HIGH`, `W-CONFIRM-EARLY`, `W-EMERGENCY-SAME`, `W-PF-EST-MISMATCH`, `W-PASSPORT-EXPIRED`, `W-IW-NATIONALITY`, `W-DUP-REHIRE`, `W-DUP-GROUP`, `W-EDU-DUP`, `W-BANK-NAME`, `W-BANK-DUP`, `W-DOC-DUP`, `W-MANAGER-SEPARATED`) but no consolidated registry exists. | A single `core/warnings.py` registry defines every code with its field and message, and the §21.3 acknowledgement protocol is implemented once over it. | RESOLVED |
| DEC-019 | §23.3, `employee_id_sequences` | `CHECK last_issued_value ≥ −100` looks wrong at first reading. | Correct and implemented as written: a sequence row is seeded at `sequence_start − increment`, and with `sequence_start = 0`, `increment = 100` the minimum legal value is −100. | RESOLVED |
| DEC-038 | §5.2 vs §5.3 | `salary.custom_component` is defined in the §5.2 permission list and required by §17.3, but has **no row in the §5.3 permission matrix**, so no role is stated to hold it. | Granted to **HRA** by default (§5.1 gives HRA "full HR data authority", and HRA holds every other salary permission) and made a **configurable `C` cell for HRE**, matching how HRE's `salary.revision_create` is treated. No other role can create assignments, so no other role could use it. | ASSUMED |
| DEC-039 | §5.4 vs §10.5 | The DM/RV visible-field list includes "official phone (if any)", but §10.5 defines no official-phone column on `employees` (only `personal_mobile`, `alternate_mobile`, `personal_email`, `official_email`). | The field set omits it. `personal_mobile` is explicitly *not* substituted: it is personal data gated by `employee.view_personal`, which DM and RV do not hold. An official phone column is not invented. | ASSUMED |
| DEC-040 | §20.2 vs AC-01/AC-19 | AC-01 requires cross-organization access attempts to be *audited* ("attempts are audited"), and AC-19 likewise ("attempt audited"), but §20.2 defines no action for a refused access. | Added `CROSS_ORG_ACCESS_DENIED` to the audit vocabulary. Without it the two acceptance criteria could not be satisfied, since a `404` with no audit row leaves no evidence of the attempt. | RESOLVED |

---

## C.1 Deployment-environment findings

Discovered while verifying against the live MySQL server; each one constrains the schema or
the session configuration.

| ID | Finding | Consequence and handling | Status |
|---|---|---|---|
| DEC-041 | The target MySQL 8.4.11 server runs with **global `sql_mode = ANSI`**, which includes `ANSI_QUOTES`, making `"` an identifier quote. | The connect hook now **appends** the required strict modes to the server's own (`SET SESSION sql_mode = CONCAT(@@SESSION.sql_mode, ',STRICT_ALL_TABLES,…')`) instead of replacing them. Replacing them stripped `ANSI_QUOTES` *after* SQLAlchemy's dialect had already detected it, so the dialect kept emitting `"name"` while the session had stopped accepting it and every reflection query failed with a syntax error. Verified by `tests/integration/test_migrations.py::TestSessionConfiguration`. | RESOLVED |
| DEC-042 | The server enforces **`sql_require_primary_key = ON`**. | No change needed: §1.5 gives every business table and every append-only table a UUID primary key, so the schema is compatible. A regression test records the setting so a future table without a key fails with a clear reason instead of mid-migration. | RESOLVED |
| DEC-043 | The existing development schema `hrms_dev_db` contains the **complete v1 HRMS schema (67 tables)** — payroll, payslips, attendance, leave, expenses, assets, loans, tax — i.e. precisely the modules §4.2 forbids. Its `employees`, `organizations`, `audit_logs`, `departments`, `designations`, `cost_centers` and `salary_components` tables collide by name with EMS tables of different shape. | EMS migrates into its **own schema**, `ems` (tests use `ems_test`), created by `scripts/create_databases.py`. `hrms_dev_db` is left completely untouched — not dropped, not altered, not migrated. `DATABASE_URL` must therefore point at `ems`. | RESOLVED |
| DEC-044 | The first TCP connection to this server costs roughly two seconds (subsequent pooled connections ~200 ms), most likely host-name resolution. | Test fixtures that build an application are session- or class-scoped rather than per-test, which took the contract suite from 62 s to under 9 s. Not an application defect; worth `skip_name_resolve` on the server if it persists in deployment. | RESOLVED |
| DEC-050 | **A configuration error was reaching clients as `422 VALIDATION_FAILED`.** The global handler caught `pydantic.ValidationError`, which covers both request validation *and* a settings object that fails to load. An unfilled `.env` therefore produced a `422` naming the offending setting and its remediation hint — schema internals in a client response, which §24.2 and §8 of the brief forbid. | Narrowed the handler to FastAPI's request-scoped `RequestValidationError`. An internal `ValidationError` now falls through to the 500 handler, which logs the detail and returns only a correlation id. | RESOLVED |
| DEC-051 | **An application built with explicit settings still read the ambient `.env`.** `get_app_settings` called the process-wide `get_settings()`, so every dependency in a test (or a second app in one process) validated the developer's `.env` on each request. | `create_app` publishes its settings on `app.state`, and the dependency reads from there, falling back to the process singleton. An application is now self-contained. | RESOLVED |
| DEC-052 | **Refresh-token replay containment was being rolled back.** On detecting a replayed rotated token, the service revokes the user's whole token family and then raises `401`; the `unit_of_work` caught that exception and rolled the revocation back, so the containment never persisted. | `_handle_unusable_refresh` now commits explicitly — a documented exception to services leaving transaction control to the unit of work, because a security containment action must outlive the failed request that triggered it. | RESOLVED |
| DEC-053 | Pydantic's `EmailStr` rejects special-use and reserved domains (`.local`, `.test`, `example.com`), which is **stricter than §21.1 V-EMAIL** — that rule asks only for a WHATWG-valid address of at most 254 characters whose domain contains a dot. An internal HR deployment may legitimately use `hr@corp.local`. | Replaced `EmailStr` with an `EmailAddress` annotated type that applies the §21.1 rule and returns the normalised (trimmed, lower-cased) value. Diverging from the spec to be stricter is still diverging from the spec. | RESOLVED |
| DEC-046 | **`rank` and `separator` are MySQL 8 reserved words** and are used as column names (`grades.rank` per §9.2, `employee_id_generation_rules.separator` per §19.1). Referencing them bare inside raw `CHECK` SQL makes `CREATE TABLE` fail with a syntax error that names nothing useful. | The column names are kept as the spec defines them and are backtick-quoted inside constraint SQL. `tests/unit/test_model_schema.py::TestMysqlReservedWords` fails if any reserved column name is ever referenced unquoted, and asserts these two are still the only reserved-word columns. Cost two partially applied migrations before being found. | RESOLVED |
| DEC-047 | `LargeBinary(n)` maps to MySQL **`BLOB`**, which cannot be indexed without a prefix length — so the blind-index columns could not carry the unique/lookup indexes that §11.5 duplicate detection and §26.2 search depend on. | Added explicit `ciphertext()` and `blind_index()` column types producing `VARBINARY(512)` and `VARBINARY(32)`, matching §1.4's `BYTES → VARBINARY(n)`. Asserted by `TestSensitiveColumnTypes`. | RESOLVED |
| DEC-048 | MySQL reports a **violated `CHECK` constraint as `OperationalError`** (3819), not `IntegrityError`, and likewise for over-long values (1406) and out-of-range values (1264); duplicate keys (1062) and failed foreign keys (1452) do arrive as `IntegrityError`. | The error translator originally dispatched on the SQLAlchemy exception *class*, so every CHECK violation fell through and would have reached clients as `500 INTERNAL_ERROR` instead of `422 VALIDATION_FAILED`. Rewritten to dispatch on the MySQL **error number**; unknown numbers are re-raised rather than flattened into a 422. Pinned by `tests/unit/test_db_error_translation.py`. | RESOLVED |
| DEC-049 | Alembic's autogenerated `downgrade()` is **not runnable** for this schema: it emits `drop_index` before `drop_table`, and MySQL refuses to drop an index a foreign key needs (error 1553). The schema also contains legitimate FK cycles (`users.employee_id → employees`, every table's `created_by → users`), so no drop order satisfies all of them. | The baseline revision's `downgrade()` is **hand-written**: suspend `FOREIGN_KEY_CHECKS`, drop tables in reverse creation order, restore checks in a `finally`. Safe because every table is being dropped. Round-trip (upgrade → downgrade → upgrade) verified against MySQL 8.4.11. This is the "review the generated script by hand" step of §10 earning its place. | RESOLVED |

---

## D. Underspecified contracts filled in

| ID | Item | Convention adopted | Status |
|---|---|---|---|
| DEC-020 | Success envelope | §24.2 defines only the **error** envelope. Success responses therefore return the resource (or `{items, next_cursor, …}` for lists) at the top level, exactly as the §24.6 example payloads show — no wrapper object. | RESOLVED |
| DEC-021 | `warnings` on success | §21.3 returns warnings inside a `422`. The §24.6 create-employee 201 example also carries `"warnings": []`, so write responses include a `warnings` array listing the codes that were acknowledged. | RESOLVED |
| DEC-022 | Archive endpoints | §24.4 writes "`POST archive`" for sub-resources without a path. Implemented uniformly as `POST …/{id}/archive` with `{reason}`. | RESOLVED |
| DEC-023 | Master `type` path segment | §24.4.2 gives kebab-case segments (`sub-departments`, `business-units`, `cost-centers`, `employment-types`, `work-locations`, `employee-categories`); table names are snake_case. Routing uses the kebab-case segments verbatim and maps them to models through one registry. | RESOLVED |
| DEC-024 | `ETag` scope | `ETag` is emitted for single-resource GETs only (not list responses) and is the quoted `row_version`. `If-Match` is required on every `PATCH`/`PUT` and on state-changing POSTs against an existing resource. | ASSUMED |
| DEC-025 | Reveal rate limit | 30 reveals per hour per user (§24.3 `RATE_LIMITED`, flagged `[ASSUMPTION]` in the spec). Implemented as configurable, default 30/hour, enforced per `(user, organization)`. | ASSUMED |
| DEC-026 | Cursor signing | §26.5 requires an opaque cursor "signed to prevent tampering, valid 24 hours". Implemented as HMAC-SHA256 over the sort key, id and issue time with the app secret; an invalid or expired cursor returns `422 VALIDATION_FAILED`. | RESOLVED |
| DEC-027 | `total_count` threshold | §26.5: exact `total_count` only when `with_total=true` and the filtered set is ≤10,000 rows, otherwise `total_count_estimate`. The estimate uses MySQL index statistics rather than a full count. | RESOLVED |

---

## E. Legal / compliance items (§25.8) — safe defaults implemented

None of these block implementation; each ships with the most restrictive setting the spec allows.

| ID | §25.8 item | Default implemented | Status |
|---|---|---|---|
| DEC-028 | Lawful basis per data category and notice content | `employee_privacy_notices.basis` defaults to `NOTICE_ONLY`; no basis is inferred by the system. | LEGAL |
| DEC-029 | DPDP Rules commencement, breach procedure | Out of application scope; no automated breach notification is built. | LEGAL |
| DEC-030 | Aadhaar collection | `AADHAAR` seeded as `NOT_COLLECTED`; values rejected with `422 FIELD_NOT_COLLECTED` until an OA enables it with a purpose statement (≥30 chars). Full value never displayed, never a key, never searchable. | LEGAL |
| DEC-031 | Retention periods | §25.6 placeholders are configuration, and the retention purge job performs **no deletion** unless a purge batch is explicitly approved. `legal_hold` blocks purge and erasure. | LEGAL |
| DEC-032 | Under-18 employees | `minimum_employment_age_years = 18`, `absolute_minimum_age_years = 14`; below the minimum requires an HRA age exception by a decider other than the requester; below the absolute minimum is hard-blocked. | LEGAL |
| DEC-033 | Copying personal data on inter-org transfer | The transfer endpoint requires an explicit `copy_authorization_confirmed = true` flag; without it the request is refused and nothing changes in either organization (§10.9, E-31). | LEGAL |
| DEC-034 | Group duplicate detection | Group blind-index matching is active only within an organization group, and a `W-DUP-GROUP` warning is returned **only** to callers holding grants in both organizations. | LEGAL |
| DEC-035 | Nomination scheme eligibility | Only share totals, minor/guardian and DOB rules are enforced (§29 Q-11); no scheme-specific eligibility is hardcoded. `scheme_rule_notes` is free text. | LEGAL |
| DEC-036 | Religion / social category / disability | All three are `false` by default and rejected with `422 FIELD_NOT_COLLECTED` when disabled; enabling religion or social category requires a purpose statement of ≥30 chars. | LEGAL |
| DEC-037 | Hosting and cross-border transfer | Storage backend and region are configuration only; no provider or region default is chosen by the code (§13 of the brief). | LEGAL |

---

## F. Spec open questions (§29.2) — spec default implemented

| ID | Default implemented |
|---|---|
| Q-01 | No group-level shared masters; masters are per organization (composite FKs make cross-org reference impossible). |
| Q-02 | Reporting managers must be in the same organization (composite FK). |
| Q-03 | `employment_start_date` must equal `joining_date`; validated, with the check isolated so a future policy change is a one-line switch. |
| Q-04 | One identifier requirement policy per organization, not per employment type. |
| Q-05 | Rehires never reuse a previous employee code; retired codes stay reserved forever. |
| Q-06 | No future-dated job changes; `effective_date ≤ today`. |
| Q-07 | The comparison view shows no arithmetic differences. |
| Q-08 | `effective_from` of an ACTIVE/EXPIRED assignment cannot be changed; use a backdated revision (O1) or a correction (O3). |
| Q-09 | India-only hosting assumed; enforced by deployment configuration, not code. |
| Q-10 | Retention periods are configurable placeholders. |
| Q-11 | See DEC-035. |
| Q-12 | See DEC-030. |
| Q-13 | See DEC-032. |
| Q-14 | Component values are **not** validated against Annual CTC (validation would require calculation, forbidden by §4.3). |
| Q-15 | Single-level maker-checker approval only. |
| Q-16 | No IFSC reference dataset; `bank_name` is entered manually. |
| Q-17 | Scanner is an interface; quarantine on detection. No vendor is chosen in code. |
| Q-18 | All document downloads audited by default (`audit_all_document_downloads = true`); restricted categories always audited regardless. |
| Q-19 | Employee self-view disabled by default. |
| Q-20 | No official-email domain restriction. |
| Q-21 | No custom statuses seeded. |
| Q-22 | HR Executives do not see salary by default (a `C` cell, see DEC-004). |
| Q-23 | DM indirect-reportee depth 20; reporting-cycle detection walks to depth 50 (§10.7 EMP-06). |

---

## G. Spec assumptions (§29.1) carried into the build

`A-01`–`A-12` are adopted as written. Two have direct code consequences worth naming:

- **A-05 / A-06** — no KMS and no malware scanner exist in this environment. Both are implemented as
  interfaces with a development implementation and remain **unconfigured** for production; see
  `docs/CONFIGURATION.md` and the limitations section of `docs/STATUS.md`.
- **A-11** — an override reason is required for any component that has a template default.

**NFR targets** marked `[ASSUMPTION]` in §3.2 (NFR-01..04, 07..09, 13, 16, 17, 21) are treated as
targets to validate, not as proven guarantees. No performance claim is made in this repository that
is not backed by a measurement recorded in `docs/STATUS.md`.

---

## H. Continuation-pass decisions (2026-10-28)

| ID | Decision | Rationale |
|---|---|---|
| DEC-055 | Audit reads return the most recent 200 rows and reject a `from`/`to` span wider than 366 days. | §24.4.8 requires the 366-day cap; the row limit prevents an unbounded response while cursor pagination for audit is not yet wired. |
| DEC-056 | ID-rule conflict detection compares department scope, priority and overlapping `[effective_from, effective_to)` in the service; the generated `conflict_key` unique constraint remains the database backstop. | MySQL cannot express the §23.5 exclusion constraint; the PostgreSQL rule is reproduced by a query plus the same-start unique key (ADR-003). |
| DEC-057 | Sequence creation uses MySQL `INSERT IGNORE` followed by `SELECT … FOR UPDATE`. | Matches §19.6 step 3 without a read-then-insert race, and serialises concurrent issuers. |
| DEC-058 | Identifier reveal is limited to `IDENTIFIER_REVEAL_RATE_PER_HOUR` (default 30) per user per organization, in-process. | Implements §24.3/DEC-025. The limiter is process-local; a multi-worker deployment needs a shared counter (recorded as a risk). |
| DEC-059 | Activation checks required identifiers against the employee's current identifier rows, not merely against the policy table. | Makes `REQUIRED_FOR_ACTIVATION` implementable once identifiers are recorded; an org with such a policy is otherwise unable to activate anyone. |
| DEC-060 | No migration was added in this pass. | All tables, constraints and indexes already existed in the baseline migration; the work was service/API/test code over that schema. |

| DEC-061 | Data export is a signed, 72-hour local-storage URL plus a separately returned one-time password used as the AES-GCM key. | Implements §25.7's encrypted/expiring export without adding an email integration; the caller is responsible for delivering the OTP separately. |
| DEC-062 | Retention approval is two sequential authenticated signatures: first records `approved_by`, a different user finalises to `APPROVED`. | Enforces the two-person rule at the service layer; a single caller supplying another user's id is not sufficient. |
| DEC-063 | Erasure anonymises the employee tombstone and deletes identifiers, bank accounts, addresses, nominee sets and document objects; salary component values are removed but assignments remain as tombstones. | Matches §25.6/§25.7 while preserving the statutory code/employment dates and audit trail. |
| DEC-064 | Transfer copies education, family, previous employment, nominations and identifiers when requested; bank, salary and documents are not copied. | §10.9 default; identifiers are re-encrypted with target-organization AAD rather than copying ciphertext. |
| DEC-065 | The job scheduler is a CLI loop (`serve-jobs`), not an in-process web-worker task. | Keeps long-running jobs out of the request process; retention batches remain explicitly approved and executed through the API. |

| DEC-OAL-1 | **Superseded by DEC-OAL-2 (2026-10-29): the full addendum is now in the repo.** Onboarding (§30, §32–§39) and Attendance (§31) are **not implemented**. | `ONBOARDING_ATTENDANCE_AND_LEAVE.md` v1.1 contains only §0 and §40–§48 (Leave). Lines 160–169 are blank where §30–§39 belong, so tables, rules, APIs, permissions and AC-29..AC-45 for those modules do not exist in the repo. Inventing them would violate "do not silently change business rules". Resolution needed: supply the v1.0 text (changelog §48 says it exists) or approve a spec-by-assumption. |


---

## I. Onboarding / attendance / leave addendum decisions (2026-10-29)

| ID | Decision | Rationale |
|---|---|---|
| DEC-OAL-2 | `ONBOARDING_ATTENDANCE_AND_LEAVE.md` (2,915 lines) is the implementation source for the three modules; DEC-OAL-1 is superseded. | The full text is now in the repository. |
| DEC-OAL-3 | Each module owns its own migration revision, chained after the previous one: foundation `b7e31f4a9c10` -> onboarding `c8f42a5bd121` (empty) -> attendance stage 1 `d9a53b6ce232` -> leave `eab64c7df343` -> attendance core (a new revision). Already-applied revisions are never refilled; later attendance stages get a **new** revision rather than appending to `d9a53b6ce232`. | An applied revision is not re-run, so appending to it would silently no-op on databases that already passed it. This departs from the "append to this revision" note in the stage-1 migration. |
| DEC-OAL-4 | Partial-unique and exclusion constraints are stored generated columns (`draft_key`, `running_key`, `active_key`) under UNIQUE keys; policy-version non-overlap (LEV-06) is a service check under a row lock on the parent policy. | MySQL has neither (ADR-003). |
| DEC-OAL-5 | `leave_balances` is created with `INSERT ... ON DUPLICATE KEY UPDATE id = id`, then `SELECT ... FOR UPDATE`; generated columns (`posted_balance_days`, `available_days`) are re-read with flush+refresh before any response. | `INSERT IGNORE` would also swallow FK errors; generated columns are stale in memory until reloaded (found by a test). |
| DEC-OAL-6 | Scheduled jobs act as a non-interactive `system@ems.invalid` user (OIDC, no password, INACTIVE) created on first run. | `created_by` / `requested_by` are FKs to `users`; the account can never authenticate. |
| DEC-OAL-7 | Leave policy resolution (6.4) uses the employee's **current** attributes, not job history as of the date. | `shortcut:` in `leave_config.py`; upgrade when back-dated runs need it. |
| DEC-OAL-8 | Applications lists show `reason` only to the owner and `leave.approve_hr`; the detail view also shows it to current and past approvers (10.5). | Lists carry no action history; avoids an N+1 on actions. |
| DEC-OAL-9 | The 4.2 table-name tripwire no longer forbids `leave_`; the table count and module count are updated with each migration. | The addendum moved leave into scope. |
| DEC-OAL-10 | LEV-30 "punches in that half": for shift records a VALID punch belongs to the first half before the shift midpoint (start + floor(duration / 2)), else the second; for flexible records any VALID punch on the date conflicts with both halves. | The spec does not define a midpoint for flexible employees; the stricter reading never hides a real conflict. |
| DEC-OAL-11 | The derived record columns of 5.9 (first_in_at, worked_minutes, halves, day_status, flags) are ordinary columns written by `RecordEngine.evaluate_into` in the same transaction as the punch that changes them. | A DB-generated column cannot express the evaluation (it reads other rows); determinism is covered by pure-function unit tests and `evaluation_version`. |
| DEC-OAL-12 | Attendance version selection and leave version selection share `services/versioning.select_version`; employee status as of a date is `services/employee_status.status_on`. | Both modules order by specificity, priority, effective_from, code; one implementation avoids drift. |
| DEC-OAL-13 | A batch import authorizes the whole request first (`attendance.import`) and then runs each item in its own transaction. | Per-item errors must not block a batch, but a caller without the permission must get `403`, not `200` with rejections. |
| DEC-OAL-14 | Day close creates records only for dates where ATT-01 holds and a policy version applies; it sets `day_close_processed_at` only once the window has closed, so open windows are retried by the next run. | Matches 5.12; keeps the job idempotent. |
| DEC-OAL-15 | A regularization approval that finds the record FINALIZED fails with `ATTENDANCE_RECORD_FINALIZED` and leaves the request SUBMITTED (ATT-22); a pending request makes finalization skip the record (`PENDING_REGULARIZATION`). | The two operations exclude each other through the record lock and the pending flag. |

| DEC-OAL-16 | Employee-owned onboarding tasks (acknowledgement, data submission) are performed only by the SELF user linked to the employee; HR may record a paper acknowledgement or submit on behalf with a reason, and the submitter can never review their own submission. | 4.10, 4.12; keeps the evidence honest. |
| DEC-OAL-17 | Submission payloads are encrypted with `FieldCipher`, never returned or logged, applied through the existing EMS services on accept, and purged at decision time. | Reuses EMS validation and audit; no second write path. |
| DEC-OAL-18 | Document verification stays in `DocumentService`; the endpoint calls `OnboardingService.on_document_verified` in the same unit of work. | Keeps the EMS service free of an onboarding dependency; one transaction. |
| DEC-OAL-19 | Linking a document to a DOCUMENT_UPLOAD task submits it (there is no direct `submit`). | The link is the submission; verification then completes or rejects the task. |
