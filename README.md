# Keka-style HR & Payroll Suite — FastAPI + SQLAlchemy Backend

Production-shaped REST API implementing the data model in `db_design.txt` (60 tables)
and the API contract in `hrms-buildproject.md` §9.

- **Framework:** FastAPI 0.141
- **ORM:** SQLAlchemy 2.0 (typed `Mapped` declarative models)
- **Validation:** Pydantic v2 (schemas are generated from the ORM models)
- **Auth:** JWT (OAuth2 bearer) + role/permission RBAC
- **DB:** MySQL 8 (`mysql+pymysql`), with SQLite supported as a fallback for tests
- **Migrations:** Alembic (initial schema included, verified up/down on MySQL)

> 📘 See [`WORKFLOWS.md`](WORKFLOWS.md) for the end-to-end workflow of every
> module (IAM, Organisation, HRIS, Time, Leave, Payroll, Tax, Expenses, Loans,
> Onboarding, Exit, Assets, Documents, Workflows, Helpdesk, Currency, Analytics,
> Audit), including state machines, endpoints and cross-module flows.

## Quick start

```powershell
python -m pip install -r requirements.txt

# 1) Start MySQL (skip if you already have a server on :3306)
docker compose up -d
# ...or point DATABASE_URL at your own MySQL in .env

# 2) configure
Copy-Item .env.example .env

# 3) create schema + demo data (org, legal entity, admin user, structure, ...)
python -m scripts.seed        # or: python -m app.seed / python seed.py

# 4) run
python -m uvicorn app.main:app --reload
```

`.env` is resolved relative to `backend/`, so commands behave the same no matter
which directory you launch them from.

The default connection string is
`mysql+pymysql://root:admin@127.0.0.1:3306/hrms?charset=utf8mb4`. The app calls
`CREATE DATABASE IF NOT EXISTS` and `create_all` at startup, so a fresh MySQL
instance needs no manual setup. For production, apply migrations instead:

```powershell
python -m alembic upgrade head
```

- Swagger UI: <http://127.0.0.1:8000/docs>
- OpenAPI JSON: <http://127.0.0.1:8000/openapi.json>
- Default login from `.env`: `admin@example.com` / `Admin@12345`

## Seeding

The seed is idempotent — run it as many times as you like; it only adds what is
missing. It creates baseline permissions, the global-admin user and a small demo
data set, and prints a per-table row-count summary.

All of these work:

```powershell
# from backend/
python -m app.seed
python -m scripts.seed
python seed.py

# from the repo root
python backend/seed.py
```

Start clean (drops every table first — destructive):

```powershell
python -m app.seed --reset
```

Auto-seed on startup: set `AUTO_SEED=true` in `.env` and the app seeds baseline +
demo data at boot whenever the database is empty. Handy for a brand-new database
so you can `uvicorn` and log in immediately. Leave it `false` in production.

> **Common gotcha:** `python -m scripts.seed` only resolves when your current
> directory is `backend/` (because `scripts` is a local package). From the repo
> root, either `cd backend` first or use `python backend/seed.py`. The seed will
> otherwise fail with `ModuleNotFoundError: No module named 'scripts'` — it is not
> a database problem.

## Tests

Runs on a throwaway SQLite file by default:

```powershell
python -m pytest -q
```

Run the identical suite against MySQL by setting `HRMS_TEST_DATABASE_URL`:

```powershell
$env:HRMS_TEST_DATABASE_URL="mysql+pymysql://root:admin@127.0.0.1:3306/hrms_test?charset=utf8mb4"
python -m pytest -q
```

The suite seeds a global admin and exercises auth, CRUD, employee profile, leave
approval, currency conversion, the full payroll run lifecycle and dashboard/reports.

## Project layout

```
app/
  core/            config, database, security, types, errors, deps, query helpers
  models/          SQLAlchemy models for all 60 tables, grouped by domain
  schemas/         Pydantic schemas + factory that generates them from models
  crud/            generic persistence helpers
  api/
    factory.py     generic REST router factory (list/get/create/patch/delete/bulk/export)
    registry.py    one ResourceSpec per table -> full REST surface
    router.py      aggregates custom + generic routers
    routes/        custom workflow endpoints (auth, payroll, finance, HR, ...)
  main.py          FastAPI app
alembic/           migration environment + initial schema
scripts/seed.py    baseline permissions + demo data
tests/             pytest suite
```

## API conventions

Every resource under `/api` follows `hrms-buildproject.md` §9:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/{resource}` | List (`page`, `pageSize`, `sort`, `search`, `filter[field]`) |
| `GET` | `/api/{resource}/{id}` | Fetch one |
| `POST` | `/api/{resource}` | Create |
| `PATCH` | `/api/{resource}/{id}` | Partial update |
| `DELETE` | `/api/{resource}/{id}` | Delete |
| `POST` | `/api/{resource}/bulk` | Bulk create (`{"items": [...]}`) |
| `POST` | `/api/{resource}/bulk-delete` | Bulk delete (`{"ids": [...]}`) |
| `GET` | `/api/{resource}/export?format=csv\|json` | Export current filter |

Filtering supports operators via suffix: `filter[amount__gte]=100`,
`filter[status__in]=active,probation`, `filter[deleted_at__isnull]=true`.

### Response envelope

```json
{ "data": { }, "meta": { "page": 1, "pageSize": 25, "total": 312, "totalPages": 13 }, "errors": null }
```

Errors:

```json
{ "data": null, "meta": null, "errors": [ { "code": "VALIDATION", "field": "email", "message": "..." } ] }
```

HTTP codes: 400 `VALIDATION`, 401 `UNAUTHENTICATED`, 403 `FORBIDDEN`,
404 `NOT_FOUND`, 409 `CONFLICT`, 422 `BUSINESS_RULE`, 429 `RATE_LIMITED`, 500 `INTERNAL`.

## Authentication & RBAC

1. `POST /api/auth/login` with `{"email", "password"}` → `data.access_token`.
2. Send `Authorization: Bearer <token>` on every request.

Permissions use the `module.action` convention (`employee.create`,
`payroll_run.approve`, ...). The `global_admin` role bypasses permission checks.
Manage access via:

- `GET|POST /api/users/{user_id}/roles`, `DELETE /api/users/{user_id}/roles/{role_id}`
- `GET|PUT /api/roles/{role_id}/permissions`

## Generic resources (full CRUD)

`GET/POST/PATCH/DELETE`, bulk and export are available for each of these paths:

**Organisation** `/api/organizations`, `/api/legal-entities`, `/api/locations`,
`/api/departments`, `/api/designations`, `/api/cost-centers`

**Access** `/api/users`, `/api/roles`, `/api/permissions`

**People** `/api/employees`, `/api/employee-job-history`,
`/api/employee-bank-details`, `/api/employee-statutory-details`,
`/api/document-types`, `/api/employee-documents`, `/api/document-templates`

**Payroll** `/api/pay-groups`, `/api/salary-structures`, `/api/salary-components`,
`/api/salary-structure-components`, `/api/employee-salaries`, `/api/salary-revisions`,
`/api/payroll-runs`, `/api/payroll-run-employees`, `/api/payslips`,
`/api/payslip-lines`, `/api/statutory-settings`

**Tax** `/api/tax-declarations`, `/api/tax-proofs`

**Finance** `/api/expense-policies`, `/api/expense-claims`, `/api/expense-claim-lines`,
`/api/loan-policies`, `/api/loans`, `/api/loan-repayments`

**Leave** `/api/leave-types`, `/api/leave-balances`, `/api/leave-requests`

**Time** `/api/shifts`, `/api/holidays`, `/api/attendance-records`

**Lifecycle** `/api/onboarding-templates`, `/api/onboarding-tasks`,
`/api/employee-onboarding`, `/api/exit-requests`, `/api/exit-tasks`, `/api/assets`,
`/api/asset-assignments`

**Support/Admin** `/api/helpdesk-tickets`, `/api/workflows`, `/api/workflow-steps`,
`/api/approvals`, `/api/notifications`, `/api/audit-logs` (read-only),
`/api/currency-rates`, `/api/display-currency-preferences`, `/api/integrations`

## Custom workflow endpoints

**Auth** `POST /api/auth/login`, `/register`, `/change-password`, `/logout`;
`GET /api/auth/me`

**Employees** `GET /api/employees/{id}/profile`, `/salaries`, `/payslips`,
`/documents`, `/leave-balances`; `PUT .../personal-details`, `.../statutory-details`;
`POST .../bank-details`, `.../job-change`, `.../onboarding`, `.../salary-revisions`;
`GET /api/employees/directory/list`

**Payroll** `POST /api/payroll-runs/{id}/process|validate|approve|finalize|rollback`;
`GET /api/payroll-runs/{id}/summary|employees|bank-file`;
`POST /api/payslips/{id}/release|hold`;
`POST /api/salary-revisions/{id}/approve|reject`

**Finance** Claims: `GET /api/expense-claims/{id}/detail`,
`POST .../submit|approve|reject|mark-paid`. Loans: `POST /api/loans/apply`,
`/loans/{id}/approve|foreclose|skip-emi`, `GET /loans/{id}/schedule`.
Tax: `GET /api/tax-declarations/{id}/detail`,
`POST .../submit|approve|reject`, `POST /api/tax-proofs/{id}/verify`

**HR** `POST /api/leave-requests/{id}/approve|reject|cancel`;
`POST /api/attendance-records/{id}/regularise`;
`POST /api/employee-onboarding/{id}/complete`, `/api/exit-requests/{id}/approve|complete`,
`/api/exit-tasks/{id}/complete`; `POST /api/assets/{id}/assign`,
`/api/asset-assignments/{id}/return`; `POST /api/helpdesk-tickets/{id}/assign|close`

**Workflows** `POST /api/workflows/{id}/start`, `GET /api/approvals/pending`,
`POST /api/approvals/{id}/approve|reject`, `GET /api/inbox`,
`GET /api/notifications/my`, `POST /api/notifications/{id}/read`

**Currency** `GET /api/currency/convert`, `GET|PUT /api/me/preferences/currency`,
`POST /api/currency/bulk-reset`

**Documents** `POST /api/documents/upload` (multipart), `GET /api/documents/{id}/download`

**Insights** `GET /api/dashboard`, `GET /api/reports`, `GET /api/reports/{code}`

## Audit logging

Mutations to audited entities (`employees`, `employee_salaries`, `salary_revisions`,
`payroll_runs`, `payslips`, `employee_bank_details`, `employee_statutory_details`,
`tax_declarations`, `expense_claims`, `loans`, `legal_entities`, `pay_groups`,
`salary_structures`, `salary_components`, `roles`, `workflows`, `currency_rates`,
`display_currency_preferences`) write an `audit_logs` row with old/new JSON values,
actor and IP.

## Design notes & extensions

- **Schema fidelity:** all 60 tables from `db_design.txt` are modelled. Tables whose
  primary key is not a surrogate `id` are handled outside the generic factory:
  `employee_personal_details` (via `/employees/{id}/personal-details`) and
  `role_permissions` (via `/roles/{id}/permissions`).
- **Portable types:** `app/core/types.py` uses native `JSON` on MySQL, `JSONB` on
  PostgreSQL and plain `JSON` on SQLite; UUIDs are stored as `CHAR(32)` and
  generated application-side. All models therefore run unchanged on MySQL or SQLite.
- **MySQL specifics:** connection pooling with `pool_recycle=280` (avoids
  `wait_timeout` drops), `utf8mb4` charset, and `ensure_database()` runs
  `CREATE DATABASE IF NOT EXISTS` before `create_all`. The Alembic initial
  migration's `downgrade` drops tables only (indexes/FKs are removed with the
  table and `FOREIGN_KEY_CHECKS` is toggled on MySQL) because MySQL refuses to
  drop an index that backs a foreign key.
- **Payroll engine:** `_structure_breakdown` in `app/api/routes/payroll.py` is a
  deliberately small illustrative component engine (fixed values + `CTC * n%`
  formulas). Extend it for full formula/rounding/cap semantics.
- **Document storage:** files are written to `./uploads`; swap for S3-compatible
  object storage by changing `app/api/routes/documents.py`.
- **Multi-tenancy:** rows carry `org_id`; the API exposes it as a filter. Add
  row-level security / tenant scoping in `app/core/deps.py` for a shared tenant model.
- **Async jobs:** the contract mentions `202 + jobId` for bulk/payroll/PDF. Current
  implementations are synchronous; wrap the long-running endpoints with a queue
  (`/api/jobs/{jobId}`) when scaling out.
