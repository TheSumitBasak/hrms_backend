# Testing Guide

## Running the suites

```bash
pytest tests -q                 # everything
pytest tests/unit -q            # pure domain logic, no database
pytest -m integration -q        # FastAPI, SQLAlchemy, Alembic — needs MySQL
pytest -m security -q           # adversarial / access control — needs MySQL
pytest -m concurrency -q        # race conditions — needs MySQL
pytest -m acceptance -q         # AC-01..AC-28 and the §27 edge cases
pytest --cov=app --cov-report=term-missing
```

Markers are inferred from the directory, so a test in `tests/security/` is automatically
marked `security` and an unmarked test in a marked directory is impossible.

## Current results

Last run recorded in `STATUS.md` (continuation pass, 2026-10-28):

```
844 passed in 308.60s
ruff check app tests scripts   → All checks passed
ruff format --check            → 130 files already formatted
mypy app                       → Success: no issues found in 97 source files
```

| Suite | Tests |
|---|---|
| `tests/unit` | 612 |
| `tests/integration` | 208 |
| `tests/security` | 21 |
| `tests/concurrency` | 0 — AC-16 race still outstanding |
| `tests/acceptance` | 0 — AC tests still to be split out |

Domain services now exist for organizations, catalogue, employees, ID issuance, identifiers,
bank accounts and audit queries. Coverage is still **not measured**; NFR-16's ≥80% target
should be checked now that those services exist. The figure will be recorded in `STATUS.md`.

---

## Database setup

Integration, security and concurrency tests need a **real MySQL 8**. §5 of the build brief
forbids substituting SQLite for dialect-specific behaviour, and this schema depends on MySQL
stored generated columns, `SELECT … FOR UPDATE` row locks and `READ-COMMITTED` isolation —
none of which SQLite can model. Tests that need a database are **skipped with a clear
reason** when none is reachable; they are never silently redirected to another engine.

```bash
python scripts/create_databases.py    # creates `ems` and `ems_test`
```

The harness finds its database in this order: `EMS_TEST_DATABASE_URL`, then `DATABASE_URL`
from the environment, then `DATABASE_URL` from `.env`.

### Two safety interlocks

1. **The schema name is forced to end in `_test`.** Whatever URL is found, the harness
   rewrites the schema to `<name>_test`. A misconfiguration therefore cannot point the
   destructive parts of the suite at a development database.
2. **The suite never reads the developer's secrets.** `build_test_settings` passes
   `_env_file=None`, so pydantic-settings does not read `.env`, and it generates fresh
   throwaway encryption keys per run. A test run cannot pick up production-shaped
   configuration, and no real key is ever used by a test.

---

## Test strategy

### Unit — no database

Domain rules in isolation: the §5.3 permission matrix, employee status transitions, employee
code rendering and sequence rules, half-open interval arithmetic, masking and serialisation,
identifier normalisation and blind-index behaviour, salary revision validation, the §21.1
rule dictionary, the §21.3 warning protocol, audit redaction.

Two of these deserve mention because they test a *property* rather than an example:

- **Verhoeff** — every single-digit substitution and every adjacent transposition in a valid
  Aadhaar is asserted to be detected. That is what the algorithm exists for, so an example-based
  test would not show it works.
- **AAD binding** — a ciphertext is asserted not to decrypt after being moved to another
  organization, another employee, another field or another entity type. That is the property
  that makes ciphertext tenant-safe.

### Integration — needs MySQL

FastAPI endpoints end to end, SQLAlchemy persistence, Alembic migrations applying cleanly,
authentication and authorization, organization isolation, composite foreign-key enforcement,
audit written in the same transaction, optimistic locking, and the MySQL session guarantees
themselves (isolation level, strict `sql_mode`, UTC timezone, bounded lock wait).

The continuation pass added integration suites for organizations/registration policies,
branches and masters, ID rules and preview, employee lifecycle, identifier encryption/reveal,
bank accounts, audit reads, profile sub-records, documents, salary search: `test_organization_config.py`,
`test_catalog_endpoints.py`, `test_id_rules.py`, `test_employees.py`, `test_identifiers.py`,
`test_bank_accounts.py`, `test_profile_records.py`, `test_documents.py`, `test_salary.py`,
`test_employee_search.py`. The §24.7 job functions are covered by `test_jobs.py`; privacy by `test_privacy.py`; transfer/age-exception by `test_transfers.py` and `test_employees.py`. Concurrency suites are under `tests/concurrency`.

### Security and adversarial — needs MySQL

Cross-tenant access by foreign path and by foreign id, IDOR, unauthorized sensitive-field
reveals, unauthorized field updates, role escalation and grant manipulation, expired and
revoked tokens, malformed input, invalid state transitions, duplicate identifier races, path
traversal and malicious uploads, and error-message information disclosure.

The isolation tests follow §23.6's instruction for MySQL deployments — *"automated tests
attempt cross-organization access for every endpoint"* — so they are generated from the route
table rather than written one at a time, and a new endpoint is covered the moment it is
registered.

### Concurrency — needs MySQL, multiple connections

Simultaneous employee creation, 50-way unique code generation (AC-16/NFR-04), competing
updates with the same ETag, concurrent salary revision creation and confirmation, and primary
bank account races. These use several real connections; they cannot be simulated in one
session, because the behaviour under test *is* the locking.

### Acceptance

One test per numbered criterion, named `test_ac01_*` … `test_ac28_*`, and one per §27 edge
case `E-01`..`E-36`, so the specification tables can be read straight down against test names.

`test_ac24_scope_guardrails.py` is the scope guard: it asserts the OpenAPI document exposes no
out-of-scope path and that the salary package contains no arithmetic over component value
columns — making §4.3 a test rather than a convention.

---

## Conventions

- **No realistic personal data in fixtures** (§9 of the brief, §25.2). Identifiers are
  structurally valid but fictitious, matching §1.4. Checksum-bearing values are *generated
  from the algorithm* rather than hardcoded, so no literal resembling a real Aadhaar or GSTIN
  appears anywhere in the repository.
- **Assertions name their specification clause.** A failure should say which requirement
  broke, not just which function.
- **Spec tables are transcribed into tests.** The §24.3/§24.5 error codes and the §5.3
  permission matrix are written out in the test files, so a regression fails against the
  specification rather than against the implementation's own idea of itself.
- **Fixtures that build an application are session- or class-scoped.** The first MySQL
  connection on the verification server costs ~2 s (DEC-044), which made a per-test
  application 20× slower for no benefit. Tests that mount throwaway routes use `isolated_app`
  so the shared instance is never mutated.








