# Database and Migration Guide

MySQL 8.0+ is the only supported dialect (ADR-002). Verified against **MySQL 8.4.11**.

> **Continuation pass (2026-10-28).** No migration was added and no table, column, index or
> constraint was changed. The pass implemented services and endpoints over the existing
> baseline migration, including the row-locked code-issuance path
> (`INSERT IGNORE` + `SELECT … FOR UPDATE`), encrypted identifier/bank writes with
> tenant-bound AAD, composite-FK-backed reference checks and append-only audit reads. Because
> the schema is unchanged, `alembic upgrade head` remains the deployment step already recorded
> in `STATUS.md`; there is no new revision to apply.

---

## 1. Schema conventions

### Logical types (§1.4)

| Logical | MySQL | Implementation |
|---|---|---|
| UUID | `BINARY(16)` | `app.db.types.Uuid7` — UUIDv7, time-ordered |
| TIMESTAMP | `DATETIME(6)` in UTC | `app.db.types.UtcDateTime` |
| BOOLEAN | `TINYINT(1)` | SQLAlchemy `Boolean` |
| Money | `DECIMAL(15,2)` | `money()` — `Decimal`, never float |
| Component percentage | `DECIMAL(7,4)` | `percentage()` |
| Share / disability percentage | `DECIMAL(5,2)` | `share_percentage()` |
| BYTES | `VARBINARY(n)` | `LargeBinary` |
| ENUM | `VARCHAR(40)` + `CHECK` | upper-snake-case codes |
| JSON | native `JSON` | only where §23.1 permits |

**Why UUIDv7.** InnoDB clusters rows on the primary key, so random UUIDv4 keys scatter inserts
across the B-tree and fragment it badly at the one-million-record scale of NFR-17. UUIDv7's
leading 48-bit millisecond timestamp keeps inserts sequential. The timestamp is a creation
hint; it is never read back as business data.

**Why `DATETIME(6)` is wrapped.** MySQL `DATETIME` carries no timezone, so `UtcDateTime`
enforces the convention at the boundary: naive values are rejected on write and values read
back are always tagged UTC. "All timestamps in UTC" becomes a property of the schema rather
than a habit.

### Standard columns (§1.5)

Business tables carry `id`, `created_at`, `created_by`, `updated_at`, `updated_by`,
`row_version`, `archived_at`, `archived_by`, `archive_reason` via `StandardColumns`.
Append-only tables (audit logs, history and event tables, the code registry) carry only `id`,
`created_at`, `created_by` via `AppendOnlyColumns` — deliberately with no update machinery, so
the tables §20.1 says are never updated have no mechanism for being updated.

`StandardColumns` sets SQLAlchemy's `version_id_col` to `row_version`, so every `UPDATE`
carries `WHERE row_version = :loaded` and a lost update raises `StaleDataError`, which the
unit of work translates to `412 PRECONDITION_FAILED`. The same counter is the HTTP `ETag`.

### Organization scoping

Every organization-scoped table carries `organization_id` **and** `UNIQUE (organization_id,
id)`. The unique constraint is redundant as a uniqueness claim — `id` is already the primary
key — but it is what makes composite foreign keys possible:

```sql
FOREIGN KEY (organization_id, department_id)
  REFERENCES departments (organization_id, id)
```

These are layer 4 of the isolation model: the database refuses a child row whose parent lives
in another organization, which is what AC-03 verifies by direct insert as well as through the
API.

### Naming

Constraints follow a fixed convention (`app/db/base.py`) so Alembic names them
deterministically and downgrades are reliable. MySQL caps identifiers at 64 characters;
`tenant_index()` raises rather than letting MySQL truncate a name.

---

## 2. MySQL-specific integrity

MySQL has no exclusion constraints and no row-level security, so the specification's MySQL
variant applies (§18.8, §23.1, §23.6). Full mapping in `ARCHITECTURE.md` §7. Summary:

| Invariant | Mechanism |
|---|---|
| Non-overlapping confirmed salary assignments (INV-1, C-05) | Employee row lock (`SELECT … FOR UPDATE`), in-transaction overlap query, `UNIQUE (employee_id, confirmed_from)` on a stored generated column blocking same-start, nightly integrity job |
| One primary bank account per date (C-07) | Row lock, interval check, unique on a generated window key, nightly job |
| ID-rule conflicts (C-11) | Publish-time conflict query under lock, scope unique, nightly job |
| One current identifier per type (C-06) | `UNIQUE (employee_id, current_identifier_key)` where the generated key is `NULL` unless current and not `PREVIOUS_PF_MEMBER_ID` |
| Case-insensitive codes | Stored generated `code_normalized` columns |
| Tenant isolation | Mandatory repository filter, composite FKs, adversarial tests |
| Name search (§26.2) | `full_name_normalized` prefix index plus a `FULLTEXT` index (no `pg_trgm`) |

**Partial indexes are emulated with generated columns.** MySQL has no `WHERE` clause on an
index, so a column is generated to be `NULL` whenever the condition is false — `NULL`s do not
collide in a unique index, which gives exactly the semantics of a PostgreSQL partial unique
index. These are written by hand in the migration: **Alembic autogenerate does not emit
generated columns.**

### Session configuration

Set on every pooled connection by `app/db/session.py`:

| Setting | Value | Why |
|---|---|---|
| `transaction_isolation` | `READ-COMMITTED` | §18.8. MySQL defaults to `REPEATABLE READ`, under which the in-transaction overlap check would read a stale snapshot and the INV-1 compensating control would not work |
| `sql_mode` | server's own **plus** `STRICT_ALL_TABLES`, `ERROR_FOR_DIVISION_BY_ZERO`, `NO_ENGINE_SUBSTITUTION`, `NO_ZERO_DATE`, `NO_ZERO_IN_DATE` | Without strict mode MySQL truncates over-long values and coerces out-of-range ones instead of raising. **Appended, not replaced** — see DEC-041 |
| `time_zone` | `+00:00` | So `CURRENT_TIMESTAMP(6)` defaults are UTC |
| `innodb_lock_wait_timeout` | `DB_LOCK_WAIT_TIMEOUT_SECONDS` | A contended row returns a retryable `409` instead of hanging |

### Server requirements and observed settings

| Setting | Requirement | Observed on the verification server |
|---|---|---|
| Version | 8.0+ | 8.4.11 ✅ |
| `sql_require_primary_key` | Compatible either way | `ON` — every EMS table declares a UUID primary key, so no change needed (DEC-042) |
| Global `sql_mode` | Any | `ANSI` (includes `ANSI_QUOTES`) — handled by appending rather than replacing (DEC-041) |
| Character set | `utf8mb4` | `utf8mb4` / `utf8mb4_0900_ai_ci` ✅ |

---

## 3. Migrations

### Setup

```bash
python scripts/create_databases.py    # creates `ems` and `ems_test`
alembic upgrade head
```

`scripts/create_databases.py` issues `CREATE DATABASE IF NOT EXISTS` only. It never drops or
alters a schema, and it takes the schema **name** from `--name` rather than from
`DATABASE_URL`, because this repository previously hosted an unrelated HRMS whose schema
defines `employees`, `organizations`, `audit_logs` and `salary_components` with different
columns (DEC-043).

### Where the URL comes from

`alembic/env.py` resolves it in this order:

1. a URL already set on the Alembic config — how the test harness and CI target a schema;
2. otherwise application settings.

`alembic.ini` deliberately carries **no** URL, so there is no third source to disagree and no
credential in version control. A test asserts this.

### Creating a revision

```bash
alembic revision --autogenerate -m "add employee identifiers"
```

Then **review the generated script by hand**. Autogenerate reliably misses:

- MySQL **stored generated columns** (the partial-index emulation above);
- `CHECK` constraint changes in some cases;
- index and constraint *renames*, which it emits as drop-plus-create;
- data migrations of any kind.

Rules (§10 of the build brief):

- Never modify an already-applied revision in a way that breaks existing deployments.
- Never use `alembic stamp` to paper over a missing schema change.
- Never drop or recreate a database to make a migration pass.
- `transaction_per_migration` is enabled, but **MySQL DDL is not transactional** — a failed
  migration leaves the schema partially changed. Each revision is therefore written to be
  independently reviewable, and a production deployment should take a backup first and use
  `alembic upgrade --sql` to produce a reviewable script.

### Applying

```bash
alembic current            # where is this database?
alembic history --verbose
alembic upgrade head
alembic upgrade --sql head > migration.sql    # offline review
alembic downgrade -1
```

The application **never** creates or migrates the schema at startup: no `create_all`, no
automatic upgrade. Schema change is an explicit, auditable step.

---

## 4. Soft deletion and uniqueness (§23.4)

Archival sets `archived_at`; archived rows are excluded from default queries but **remain in
uniqueness checks** unless stated otherwise:

| Value | Scope of uniqueness |
|---|---|
| Employee code | Unique across **all** records including archived — never reused (C-02) |
| Master codes | Unique including inactive and archived rows |
| Official email | Unique only among non-separated, non-archived records, so an address can be reused after someone leaves. The database constraint covers `ORGANIZATION` scope; `GROUP` and `NONE` are service-enforced (DEC-045) |
| Identifier duplicates | Per the §11.5 matrix; archived drafts excluded |

Hard deletion is limited to never-referenced configuration rows, never-submitted salary
drafts, `PENDING_UPLOAD` documents and approved retention purges.

---

## 5. Backup and restore

Operational detail in `DEPLOYMENT.md`. NFR-09 asks for daily full backups plus continuous
binlog for point-in-time recovery, 35-day retention, encrypted at rest, with a quarterly
restoration test. Note that a restore must also restore the **key material** for `[E]` fields:
a database backup without its KMS keys is unreadable by design.


## 6. Leave tables (migration `eab64c7df343`)

`leave_types`, `leave_policies`, `leave_policy_versions` (stored `applicability_specificity`, `draft_key`),
`leave_balances` (stored `posted_balance_days`, `available_days`), `leave_transactions` (append-only; unique
`(organization_id, idempotency_key)` and one reversal per target), `leave_year_closures`,
`leave_applications`, `leave_application_slots` (stored `active_key`; unique
`(employee_id, slot_date, half, active_key)` is the overlap guarantee), `leave_application_actions`
(append-only), `background_job_runs` (stored `running_key`: one RUNNING row per organization and job).
All carry composite `(organization_id, ...)` foreign keys. Downgrade drops the tables (MySQL cannot drop an
index a foreign key still uses). 87 tables in total after this migration.


## 7. Attendance core tables (migration `a3d91c7e5b42`)

`shifts` (stored `is_overnight`, `duration_minutes`), `attendance_policies`,
`attendance_policy_versions` (stored `applicability_specificity`, `draft_key`),
`employee_shift_assignments`, `attendance_records` (unique `(organization_id, employee_id,
business_date)`; halves link to `leave_applications (employee_id, id)` so the database enforces
same-employee), `attendance_punches` (unique `(organization_id, integration_source_code,
external_event_id)` is the replay guard), `attendance_record_revisions` (append-only, unique
`(attendance_record_id, revision_no)`). 94 tables in total. The migration is a separate revision
after `eab64c7df343` because the stage-1 revision is already applied (DEC-OAL-3).


## 8. Regularization tables (migration `b5e82d1f7a63`)

`attendance_regularization_requests` (stored `pending_key`; unique `(attendance_record_id,
pending_key)` allows one SUBMITTED request per record), `attendance_regularization_items`
(append-only, CHECKs tie each item type to its columns) and `attendance_regularization_actions`
(append-only). 97 tables in total.

## 9. Onboarding tables (migration `c6f93e2a8b74`)

Templates (`onboarding_templates`, `onboarding_template_versions`, `onboarding_template_tasks`,
`onboarding_template_task_dependencies`), instances (`onboarding_instances`, `onboarding_instance_events`),
tasks (`onboarding_tasks`, `onboarding_task_dependencies`, `onboarding_task_events`,
`onboarding_task_documents`), `onboarding_acknowledgements` and `onboarding_data_submissions`
(a PRIVACY_NOTICE acknowledgement also writes the existing `employee_privacy_notices`). Stored generated columns replace partial uniques: `draft_key` (one draft version per
template), `open_key` (ONB-02, one open instance per employee) and `pending_key` (ONB-10, one pending
submission per task). 109 tables in total.
