# EMPLOYEE_MANAGEMENT_SYSTEM.md
# Employee Management System (EMS): Product & Technical Specification

| Item | Value |
|---|---|
| Document | EMPLOYEE_MANAGEMENT_SYSTEM.md |
| Version | 1.0 (Draft for review) |
| Date | 2026-10-28 |
| Target market | Organizations operating in India (multi-organization deployment) |
| Status | Source of truth for development; items in §29 require business/legal sign-off |
| Amendments | 2026-11-01: salary template builder support (§16.7, §24.4.6), ISO 4217 currency validation (§16.4, §16.5), component usage counts (§16.3), first-administrator grant set (§5.3 note 5). See SYSTEM_LIMITATIONS.md for the review that prompted them. |

---

## 1. Document Purpose and Scope

### 1.1 Purpose
This document defines the features, business rules, field specifications, validation constraints, data relationships, workflows, APIs and acceptance criteria for an Employee Management System (EMS) that stores, manages, updates and retrieves employee information for multiple legally distinct organizations in India. It must be precise enough for a developer to implement the system without guessing business rules.

### 1.2 Audience
Product owners, business analysts, backend/frontend developers, database engineers, QA engineers, security reviewers, HR process owners and legal/compliance reviewers.

### 1.3 Scope Summary
The EMS is a **system of record for employee information**. It is **not** an HRMS. It stores salary structures, statutory identifiers and dates as **reference data only**. It performs **no** payroll, tax, statutory contribution, attendance, leave, recruitment, performance, training, expense, asset, exit-settlement or accounting computations or workflows (see §4.2).

### 1.4 Conventions and Legend

**Normative keywords:** MUST, MUST NOT, SHOULD, MAY as per RFC 2119.

**"Required?" column codes:**

| Code | Meaning |
|---|---|
| M | Mandatory. Must be non-null on every save, including Draft. |
| M-A | Mandatory for activation. May be null while the record is in DRAFT; must be present before the record leaves DRAFT. |
| O | Optional. |
| C | Conditionally mandatory. The condition is stated in the Validation column. |
| SYS | System-generated. Not accepted from clients; if supplied, the API rejects with `422 FIELD_READ_ONLY`. |
| DER | Derived/display-only. Computed from other stored data; not an independent source of truth. |

**Field flags (appended to the Required? code):**

| Flag | Meaning |
|---|---|
| [R] | Sensitive/restricted. Requires a specific permission to view; masked or omitted by default. |
| [E] | Encrypted at rest at application level (see §25.4). |
| [H] | Historical snapshot. Copied at the time of an event and never updated afterwards. |

**Logical data types** (portable across PostgreSQL 15+ and MySQL 8.0+):

| Logical type | PostgreSQL | MySQL | Notes |
|---|---|---|---|
| UUID | `uuid` | `BINARY(16)` | UUIDv7 recommended (time-ordered, index friendly). |
| VARCHAR(n) | `varchar(n)` | `VARCHAR(n)` utf8mb4 | Unicode; length counted in characters. |
| TEXT | `text` | `TEXT` | |
| DATE | `date` | `DATE` | Business dates (no time component). |
| TIMESTAMP | `timestamptz` | `DATETIME(6)` stored in UTC | All event timestamps in UTC. |
| BOOLEAN | `boolean` | `TINYINT(1)` | |
| SMALLINT / INTEGER / BIGINT | native | native | |
| DECIMAL(p,s) | `numeric(p,s)` | `DECIMAL(p,s)` | Money = DECIMAL(15,2); percentage = DECIMAL(7,4). |
| ENUM{…} | `varchar(40)` + CHECK | `VARCHAR(40)` + CHECK | Fixed system values; stored as upper-snake-case codes. |
| LOOKUP(list) | `varchar(50)` | `VARCHAR(50)` | Value code from `lookup_values` for the same organization (§9.3). |
| BYTES | `bytea` | `VARBINARY(n)` | Ciphertext or HMAC output. |
| JSON(schema) | `jsonb` | `JSON` | Only where explicitly allowed; strictly schema-validated; never used for repeated business data. |

**Date and time conventions:**
- Business dates are DATE values interpreted in the organization's configured `timezone` (default `Asia/Kolkata`).
- "Today" means the current date in the organization's timezone.
- **All effective-dated intervals in this system are half-open: `[effective_from, effective_to)`.** `effective_from` is inclusive, `effective_to` is exclusive, and `effective_to = NULL` means "no known end".
- **Exception:** `employment_end_date` is the **inclusive last day of employment** (a business attribute, not an interval end). Wherever it bounds an interval, the exclusive boundary is `employment_end_date + 1 day`.
- Timestamps are stored in UTC and displayed in the organization's timezone.

**Examples:** All identifier examples in this document are fictitious and illustrative.

### 1.5 Standard Technical Columns
Every business table includes the following columns unless explicitly stated otherwise. Field tables in later sections refer to them as "standard columns (§1.5)".

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | Immutable; primary key | 0192c3a4-7b1e-7cc0-9f00-1a2b3c4d5e6f | Never reused; never exposed as a business identifier. |
| created_at | TIMESTAMP | SYS | UTC | 2026-10-28T06:44:10Z | |
| created_by | UUID | SYS | FK users.id | | |
| updated_at | TIMESTAMP | SYS | UTC; ≥ created_at | | |
| updated_by | UUID | SYS | FK users.id | | |
| row_version | INTEGER | SYS | Starts at 1; +1 per update | 3 | Optimistic locking; exposed as HTTP ETag. |
| archived_at | TIMESTAMP | SYS | Null when not archived | | Soft archival; see §23.4. |
| archived_by | UUID | SYS | FK users.id; set with archived_at | | |
| archive_reason | VARCHAR(500) | C | Required (10–500 chars) when archived_at is set | Duplicate record created in error | |

Append-only tables (audit logs, history/event tables, code registry) have only `id`, `created_at`, `created_by` and are never updated.

### 1.6 Glossary

| Term | Definition |
|---|---|
| Organization | A legally distinct entity (company, LLP, firm, trust, etc.) configured in the EMS. Tenant boundary. |
| Organization group | Optional grouping of organizations (e.g., group companies) used for group-level roles and cross-organization duplicate detection. |
| Employee / employment record | One row in `employees` representing one person's employment with exactly one organization over one continuous engagement. Rehire or inter-organization transfer creates a new record. |
| Master | Organization-scoped reference list (departments, designations, etc.). |
| Lookup | Organization-scoped configurable option list (gender, relationship, etc.). |
| Salary component | Organization-defined named compensation field (e.g., Basic, HRA). |
| Salary template | Reusable, versioned set of salary components with defaults. |
| Salary assignment | Effective-dated salary structure for one employee, snapshotting component values. |
| Confirmed assignment | Assignment with status SCHEDULED, ACTIVE or EXPIRED; participates in the salary timeline. |
| ID generation rule | Effective-dated configuration that renders human-readable employee codes. |
| Sequence group | Counter shared by one or more ID rules. |
| Blind index | Keyed HMAC of a normalized sensitive value used for exact-match search and duplicate detection without decryption. |
| Basis date | The date used to select an ID rule and render year tokens (joining date by default). |

---

## 2. Product Objectives

| # | Objective | Measure of success |
|---|---|---|
| OBJ-1 | Provide a single, accurate system of record for employee information across multiple organizations. | 100% of employee records belong to exactly one organization; zero cross-organization leakage in security tests. |
| OBJ-2 | Capture India-relevant employee and statutory reference data without implementing statutory processing. | All fields in §10–§14 are storable; no computation endpoints exist (AC-24). |
| OBJ-3 | Preserve history (employment, status, salary, ID rules, documents) without overwriting. | Every change is reconstructable from history tables and audit logs. |
| OBJ-4 | Make salary structures configurable and reusable without hard-coding. | New components and templates configurable without code changes. |
| OBJ-5 | Generate unique, configurable employee codes safely under concurrency. | Zero duplicates in a 50-concurrent-creation test (AC-16). |
| OBJ-6 | Protect sensitive personal data with least-privilege access, encryption, masking and auditing. | Sensitive identifiers never appear in plaintext in list views, logs or audit records. |
| OBJ-7 | Support privacy-by-design aligned with the DPDP Act, 2023 framework. | Data minimization defaults (§9.3, §7.4) and retention configuration (§25.6) present; legal review items tracked (§25.8). |

---

## 3. Functional and Non-Functional Requirements

### 3.1 Functional Requirements

| ID | Requirement | Priority | Section |
|---|---|---|---|
| FR-ORG-01 | Create, view, edit, deactivate and reactivate organizations. | Must | §7 |
| FR-ORG-02 | Maintain multiple statutory registrations per organization, optionally per state and branch. | Must | §7.4 |
| FR-ORG-03 | Maintain organization settings controlling validation, ID generation, approvals and data collection. | Must | §7.5 |
| FR-ORG-04 | Optionally group organizations for group-level access and duplicate detection. | Should | §7.6 |
| FR-BR-01 | Create, edit, deactivate branches scoped to an organization. | Must | §8 |
| FR-MST-01 | Maintain organization-scoped masters (departments, sub-departments, designations, grades, business units, cost centers, employment types, work locations, employee categories). | Must | §9 |
| FR-MST-02 | Maintain organization-scoped configurable lookup lists. | Must | §9.3 |
| FR-EMP-01 | Create employee records (Draft), complete and activate them. | Must | §10 |
| FR-EMP-02 | Edit personal, contact and employment data with history preservation. | Must | §10 |
| FR-EMP-03 | Change employee status per the transition matrix with required dates and reasons. | Must | §10.3 |
| FR-EMP-04 | Record job changes (department, designation, branch, manager, etc.) as effective-dated history. | Must | §10.8 |
| FR-EMP-05 | Support rehire and inter-organization transfer as new linked employment records. | Must | §10.9 |
| FR-ID-01 | Store identity and statutory reference identifiers encrypted, masked, with duplicate detection. | Must | §11 |
| FR-EDU-01 | Maintain multiple education records. | Must | §12.1 |
| FR-PRV-01 | Maintain multiple previous employment records with overlap flagging. | Must | §12.2 |
| FR-FAM-01 | Maintain family members. | Must | §13.1 |
| FR-NOM-01 | Maintain nominations per nomination type with share validation and minor/guardian support. | Must | §13.2 |
| FR-BNK-01 | Maintain one or more bank accounts with a single primary account at a time. | Must | §14 |
| FR-DOC-01 | Upload, version, verify, download (time-limited) and archive employee documents by category. | Must | §15 |
| FR-SAL-01 | Define salary components and versioned salary templates. | Must | §16 |
| FR-SAL-02 | Assign salary structures to employees from templates or custom, with permitted overrides. | Must | §17 |
| FR-SAL-03 | Schedule future-dated revisions, cancel scheduled revisions, correct past revisions, with full history. | Must | §17, §18 |
| FR-SAL-04 | View current, historical and scheduled salary structures; compare any two revisions. | Must | §17.6 |
| FR-SAL-05 | Optional maker-checker approval for salary revisions. | Should | §17.4 |
| FR-IDG-01 | Configure effective-dated employee ID rules per organization and optionally per department. | Must | §19 |
| FR-IDG-02 | Generate employee codes atomically and concurrency-safely; preview without consuming sequences. | Must | §19 |
| FR-IDG-03 | Permission-controlled manual codes and code reassignment with history. | Should | §19.8 |
| FR-SEC-01 | Role-based access control scoped by organization with field-level restrictions. | Must | §5 |
| FR-AUD-01 | Append-only audit logging of all changes and sensitive accesses. | Must | §20 |
| FR-SRC-01 | Organization-scoped search, filter, sort and pagination. | Must | §26 |

### 3.2 Non-Functional Requirements
Targets marked **[ASSUMPTION]** must be confirmed by the product owner before go-live.

| ID | Category | Requirement | Target |
|---|---|---|---|
| NFR-01 | Performance | Employee list (25 rows, default filters) in an organization of up to 100,000 employee records | p95 ≤ 800 ms server time [ASSUMPTION] |
| NFR-02 | Performance | Employee search (name prefix/trigram, code, email) | p95 ≤ 1.5 s server time [ASSUMPTION] |
| NFR-03 | Performance | Employee detail view (without documents) | p95 ≤ 500 ms [ASSUMPTION] |
| NFR-04 | Concurrency | Simultaneous employee creations in the same ID sequence scope | 50 concurrent requests complete with zero duplicate codes and zero lost sequence values; each completes ≤ 5 s [ASSUMPTION] |
| NFR-05 | Data integrity | Referential integrity and organization consistency | Enforced by DB foreign keys/composite FKs; zero orphan rows in nightly integrity check |
| NFR-06 | Data integrity | Salary timeline overlaps | Zero overlapping confirmed assignments (DB exclusion constraint on PostgreSQL; locking + nightly checker on MySQL) |
| NFR-07 | Availability | Monthly availability of API and UI | ≥ 99.5% excluding announced maintenance [ASSUMPTION] |
| NFR-08 | Recovery | Recovery Point Objective / Recovery Time Objective | RPO ≤ 15 min; RTO ≤ 4 h [ASSUMPTION] |
| NFR-09 | Backup | Database backups | Daily full + continuous WAL/binlog for point-in-time recovery; retained 35 days; encrypted; restoration test quarterly [ASSUMPTION] |
| NFR-10 | Auditability | Audit record written for every create/update/archive/status/permission change | 100%, in the same DB transaction as the change |
| NFR-11 | Security | Transport encryption | TLS 1.2+ (TLS 1.3 preferred); HSTS enabled |
| NFR-12 | Security | Authentication | OIDC/SAML SSO or local accounts; MFA mandatory for System Super Admin, Organization Admin, HR Admin and Restricted Document Reviewer |
| NFR-13 | Security | Session | Access token TTL ≤ 15 min; idle web session timeout 30 min; absolute 12 h [ASSUMPTION] |
| NFR-14 | Accessibility | UI conformance | WCAG 2.2 Level AA |
| NFR-15 | Maintainability | Configuration over code | Components, templates, lookups, document categories, ID rules, identifier policies configurable without deployment |
| NFR-16 | Maintainability | Automated tests | ≥ 80% line coverage on domain services; 100% of acceptance criteria automated [ASSUMPTION] |
| NFR-17 | Scalability | Capacity | 500 organizations, 1,000,000 employee records, 10,000,000 documents without schema change [ASSUMPTION] |
| NFR-18 | Documents | Upload limits | Default max 10 MB per file (configurable 1–25 MB per category); photograph max 2 MB |
| NFR-19 | Documents | Formats | Default allowed: PDF, JPEG, PNG. Optional (per category): DOCX. Always blocked: see §15.5 |
| NFR-20 | Localization | Display | English UI; Unicode name support including Indic scripts; dates displayed DD-MM-YYYY by default (configurable) |
| NFR-21 | Data residency | Hosting region | Primary and backup storage in India [ASSUMPTION; see §29] |

---

## 4. In-Scope and Out-of-Scope Functionality

### 4.1 In Scope

| Area | Included capability |
|---|---|
| Organizations | Profile, registrations, settings, groups, activation/deactivation |
| Branches & masters | CRUD, deactivation, organization scoping |
| Employees | Personal, contact, identity, statutory reference, employment, job history, status history |
| Repeated data | Education, previous employment, family, nominees, bank accounts |
| Documents | Categories, upload, versioning, verification, secure download, expiry flags, archival |
| Salary (reference only) | Components, versioned templates, employee assignments, overrides, scheduled/backdated revisions, history, comparison |
| Employee codes | Effective-dated generation rules, sequences, preview, manual codes, reassignment |
| Platform | RBAC, organization isolation, audit logs, search, encryption, retention configuration |
| Self-service (limited, optional) | An employee user MAY view their own profile and submit edits to explicitly permitted fields (§5.6). No other self-service. |

### 4.2 Out of Scope (MUST NOT be built)

| Excluded module | What MAY be stored instead (reference only) |
|---|---|
| Payroll processing, salary payment calculation, net/take-home pay, payslip generation | Salary component values as entered (§17) |
| Income tax computation, TDS, tax regime processing, Form 16/24Q filing | PAN, organization TAN (identifiers only) |
| PF/EPF, ESIC, PT, LWF contribution calculation, challans, returns, statutory filing | UAN, PF member ID, ESIC IP number, PT state, LWF state, registration numbers, "employer contribution reference" component values as entered |
| Attendance, biometrics, shifts, timesheets | None |
| Leave applications, balances, approvals | None (INACTIVE status reason may say "Sabbatical" as a recorded fact) |
| Recruitment, job postings, ATS, interviews | `source_of_hire`, `referred_by_employee_id`, offer/appointment letter dates and documents |
| Performance reviews, appraisals, OKRs | None |
| Training management | None (certificates may be stored as documents) |
| Expense claims, reimbursements, payments | None |
| Asset allocation and inventory | None |
| Exit/offboarding workflows, F&F settlement, gratuity calculation | Status, `employment_end_date`, status reason, relieving-letter documents |
| Accounting, invoicing, ERP | Cost center and business unit masters (labels only) |
| Notifications/workflow engines (email/SMS reminders) | Expiry flags shown in UI only (§15.8) |

### 4.3 Computation Guardrails
The system MUST NOT compute, store as derived values, or display any of the following: net pay, gross pay totals, sums of components, deductions, tax, employer/employee statutory contributions, gratuity amounts, leave encashment, salary arrears, salary deltas or percentage increases, projected salary, or cost-to-company reconciliations. [CHG 2026-11-02] One calculation is permitted: a PERCENTAGE salary component is a percentage **of Basic**, and its own amount (`percentage x Basic`, rounded half-up to 2 dp) is resolved when a salary assignment is saved and stored in `computed_amount` (§16.3, §17.2). Percentages are never applied to any other base, and no component is ever added to, subtracted from or netted against another. Totals, gross, net and cost-to-company remain prohibited.

---

## 5. User Roles and Permissions

### 5.1 Roles

| Role | Code | Scope | Purpose |
|---|---|---|---|
| System Super Admin | SSA | Platform | Creates organizations and groups, manages platform users and role definitions, system-level validation configuration. **No employee data access by default.** |
| Organization Admin | OA | One or more explicitly granted organizations | Configures organization profile, registrations, settings, branches, masters, lookups, ID rules, salary components/templates, document categories, users/roles within the organization. Non-sensitive employee read access. |
| HR Admin | HRA | Granted organizations | Full HR data authority: all employee data including sensitive fields, salary revisions, corrections, backdating, status corrections, archival. |
| HR Executive | HRE | Granted organizations | Day-to-day data entry: create/edit employees and repeated records, upload documents; no reveal of full identifiers or bank numbers; salary only if explicitly granted. |
| Department Manager | DM | Scoped employees in granted organization | Read-only view of non-sensitive work profile of scoped employees (§5.4). |
| Read-only Viewer | RV | Granted organizations | Read-only non-sensitive work profile of all employees in the organization. |
| Restricted Document Reviewer | RDR | Granted organizations | View employee minimal profile; view, download and verify documents including restricted categories; masked identifiers. |
| Employee (self) | SELF | Own employee record only | Optional (§5.6). |

Roles are combinable; a user's effective permissions in an organization are the union of the roles granted for that organization. Grants are per organization (`user_organization_access`). A **group-level grant** is an explicit set of per-organization grants created by an SSA covering several organizations in one group; there is no implicit inheritance.

### 5.2 Permission Codes

| Code | Description |
|---|---|
| org.create / org.archive | Create or deactivate organizations and groups |
| org.manage | Edit organization profile, registrations, settings |
| master.manage | Manage branches, masters, lookups |
| employee.list | View employee lists and search |
| employee.view | View non-sensitive profile (work profile, official contact) |
| employee.view_personal | View personal contact, addresses, emergency contacts, DOB, family |
| employee.create / employee.edit | Create / edit employee records and repeated records |
| employee.status | Standard status transitions |
| employee.status_restricted | Transitions to TERMINATED/ABSCONDING; status corrections |
| employee.archive | Archive/discard records |
| employee.remarks | View/edit remarks field |
| sensitive.restricted_personal | View disability, religion, social category |
| identity.view_masked / identity.reveal / identity.edit | Masked view / full reveal / edit of identifiers |
| identity.duplicate_override | Save despite blocking duplicate identifier |
| bank.view_masked / bank.reveal / bank.edit / bank.verify | Bank details |
| bgv.view | View background verification and reference contacts |
| salary.view | View salary assignments and history |
| salary.template_manage | Manage components and templates |
| salary.revision_create | Create draft revisions |
| salary.revision_confirm | Confirm/schedule revisions |
| salary.revision_cancel | Cancel scheduled revisions |
| salary.revision_approve | Approve revisions when approval is enabled |
| salary.revision_backdate | Confirm revisions with past effective dates |
| salary.revision_correct | Correct ACTIVE/EXPIRED assignments |
| salary.custom_component | Add employee-specific custom components |
| idrule.manage | Manage ID rules |
| employee.code_manual / employee.code_reassign | Manual codes / reassignment |
| document.upload / document.download / document.download_restricted / document.verify / document.archive | Document operations |
| audit.view | View audit logs for the organization |
| user.manage | Manage users and organization access |

### 5.3 Permission Matrix
Legend: **Y** = granted; **N** = not granted; **S** = scoped subset (§5.4); **C** = not granted by default, may be enabled per organization by an OA.

| Capability (permission codes) | SSA | OA | HRA | HRE | DM | RV | RDR |
|---|---|---|---|---|---|---|---|
| View employee lists (employee.list) | N¹ | Y | Y | Y | S | Y | Y |
| View work profile (employee.view) | N¹ | Y | Y | Y | S | Y | Y |
| View personal contact/DOB/family (employee.view_personal) | N¹ | N | Y | Y | N | N | N |
| Create employee records (employee.create) | N | N | Y | Y | N | N | N |
| Edit employee records (employee.edit) | N | N | Y | Y | N | N | N |
| Standard status changes (employee.status) | N | N | Y | Y | N | N | N |
| Terminate / mark absconding / status correction (employee.status_restricted) | N | N | Y | N | N | N | N |
| Archive or discard records (employee.archive) | N | N | Y | N | N | N | N |
| View remarks (employee.remarks) | N | N | Y | C | N | N | N |
| View disability/religion/category (sensitive.restricted_personal) | N | N | Y | N | N | N | N |
| View identity documents' metadata and masked identifiers (identity.view_masked) | N | N | Y | Y | N | N | Y |
| Reveal full identifiers (identity.reveal) | N | N | Y | N | N | N | C |
| Edit identifiers (identity.edit) | N | N | Y | Y | N | N | N |
| Override blocking duplicate identifiers (identity.duplicate_override) | N | N | Y | N | N | N | N |
| View bank details masked (bank.view_masked) | N | N | Y | Y | N | N | N |
| Reveal full bank account number (bank.reveal) | N | N | Y | N | N | N | N |
| Edit bank details (bank.edit) | N | N | Y | Y | N | N | N |
| Verify bank details (bank.verify) | N | N | Y | C | N | N | N |
| View BGV/reference contacts (bgv.view) | N | N | Y | C | N | N | Y |
| View salary structures and history (salary.view) | N | N | Y | C | N | N | N |
| Edit salary components/templates (salary.template_manage) | N | Y | Y | N | N | N | N |
| Create salary revisions (salary.revision_create) | N | N | Y | C | N | N | N |
| Confirm/schedule salary changes (salary.revision_confirm) | N | N | Y | N | N | N | N |
| Cancel scheduled salary changes (salary.revision_cancel) | N | N | Y | N | N | N | N |
| Approve revisions, if enabled (salary.revision_approve) | N | C | Y² | N | N | N | N |
| Backdate / correct revisions | N | N | Y | N | N | N | N |
| Configure employee ID rules (idrule.manage) | N | Y | C | N | N | N | N |
| Manual employee code (employee.code_manual) | N | Y | C | N | N | N | N |
| Reassign employee code (employee.code_reassign) | N | Y | Y | N | N | N | N |
| Create/deactivate organizations (org.create/org.archive) | Y | N | N | N | N | N | N |
| Manage organization profile, registrations, settings (org.manage) | Y | Y | N | N | N | N | N |
| Manage branches, masters, lookups (master.manage) | N | Y | C | N | N | N | N |
| Upload documents (document.upload) | N | N | Y | Y | N | N | N |
| Download non-restricted documents (document.download) | N | N | Y | Y | N | N | Y |
| Download restricted documents (document.download_restricted) | N | N | Y | N | N | N | Y |
| Verify documents (document.verify) | N | N | Y | C | N | N | Y |
| Archive documents (document.archive) | N | N | Y | N | N | N | N |
| View audit logs (audit.view) | Y³ | Y | Y | N | N | N | N |
| Manage users and roles (user.manage) | Y | Y⁴ | N | N | N | N | N |

Notes:
1. SSA may obtain **break-glass** access to an organization's employee data only through a time-bound grant (maximum 4 hours) with a mandatory reason (≥ 20 characters). The grant and every access during it are audited and visible in that organization's audit log.
2. An approver MUST NOT approve a revision they created or submitted (maker-checker).
3. SSA sees platform-level audit events (organization creation, role definitions, break-glass grants); organization data events only during break-glass.
4. OA can grant only roles OA, HRA, HRE, DM, RV, RDR within organizations where the OA holds OA; OA cannot grant SSA and cannot grant access to other organizations.

### 5.4 Scoping Rules
- **Organization scope:** Every permission applies only to organizations listed in the user's active `user_organization_access` rows.
- **Department Manager scope:** A DM user linked to an employee record (`users.employee_id`) sees employees who (a) are direct or indirect reportees through `reporting_manager_employee_id` (maximum depth 20), or (b) belong to a department whose `head_employee_id` is the DM's employee record. Visible fields: employee_code, display name, photograph, designation, department, sub-department, branch, work location, work mode, official email, official phone (if any), joining date, status, reporting manager. Nothing else.
- **Read-only Viewer:** Same field set as DM, organization-wide.
- **Restricted Document Reviewer:** Minimal profile (code, name, department, designation, status) plus documents and masked identifiers.

### 5.5 Enforcement Requirements
- Permissions MUST be enforced in the backend service layer for every request; UI hiding is cosmetic only.
- Field-level filtering MUST be applied in API serialization: restricted fields are omitted (not nulled) when the caller lacks permission; masked fields return the mask format in §11.6.
- Object-level checks MUST verify that the target resource's `organization_id` equals the path organization and that the caller has access (§6.3).
- On PostgreSQL, Row-Level Security SHOULD be enabled on all organization-scoped tables as defense in depth (§23.6).
- Permission changes take effect within 60 seconds (permission cache TTL ≤ 60 s); user deactivation revokes refresh tokens immediately.

### 5.6 Optional Employee Self-View
If `organization_settings.enable_employee_self_view = true`, a user linked to an employee record may view their own profile (excluding remarks, BGV, salary unless `self_view_salary = true`) and submit change requests for: personal_mobile, alternate_mobile, personal_email, current address, emergency contact. Changes are stored as pending and applied only after HRA/HRE acceptance (audited). No other self-service functionality is in scope.

---

## 6. Multi-Organization Architecture

### 6.1 Tenancy Model
- Single application, single database, **shared schema with an `organization_id` discriminator** on every organization-scoped table.
- Organization isolation is enforced at four layers: (1) API path scoping and authorization, (2) service-layer object checks, (3) database composite foreign keys ensuring related rows share the same organization, (4) PostgreSQL Row-Level Security (optional on MySQL: mandatory data-access-layer filter plus automated tests).

### 6.2 Structure

```
organization_groups (optional)
└── organizations
    ├── organization_settings (1:1)
    ├── organization_registrations (1:N, optionally per branch/state)
    ├── branches
    ├── departments ── sub_departments
    ├── designations, grades, business_units, cost_centers,
    │   employment_types, work_locations, employee_categories
    ├── lookup_values
    ├── document_categories
    ├── salary_components
    ├── salary_templates ── salary_template_versions ── salary_template_components
    ├── employee_id_generation_rules / employee_id_sequences / employee_code_registry
    └── employees (each with exactly one organization_id)
        ├── employee_addresses, employee_identifiers
        ├── employee_job_history, employee_status_history
        ├── employee_education, employee_previous_employment
        ├── employee_family_members, employee_nominees
        ├── employee_bank_accounts, employee_documents
        └── employee_salary_assignments ── employee_salary_components
```

### 6.3 Organization Consistency Rules

| Relationship | Rule | Enforcement |
|---|---|---|
| Employee → branch, department, sub-department, designation, grade, business unit, cost center, employment type, work location, employee category | Referenced row MUST have the same organization_id | Composite FK `(organization_id, x_id) → x(organization_id, id)` |
| Employee → reporting manager, HR contact, referred-by | Referenced employee MUST be in the same organization | Composite FK to `employees(organization_id, id)` |
| Sub-department → department | Sub-department's department must equal employee's department | Service validation + composite FK `(department_id, sub_department_id) → sub_departments(department_id, id)` |
| Child records (education, family, bank, etc.) → employee | Child organization_id = employee organization_id | Composite FK `(organization_id, employee_id) → employees(organization_id, id)` |
| Document references from child records | Document must belong to the same employee | Composite FK `(employee_id, document_id) → employee_documents(employee_id, id)` |
| Salary assignment → template version | Same organization | Composite FK |
| ID rule → department | Same organization | Composite FK |
| Registration → branch | Same organization | Composite FK |

### 6.4 Permitted Cross-Organization References
Only the following references may cross organizations:
- `employees.previous_employment_record_id` (inter-organization transfer). The linked record is displayed only if the viewer has access to its organization; otherwise the UI shows "Linked record in another organization (restricted)".
- Duplicate identifier detection within an organization group (§11.5), visible only to users holding access to both organizations.
- `users` and `audit_logs` (platform tables keyed by organization where applicable).

Group-level shared masters are **not** supported in v1 (§29).

---

## 7. Organization Management

### 7.1 Address Group (reused)
Wherever a field is described as an "address group" with a prefix (e.g., `registered_address`), the following columns exist with that prefix:

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| {prefix}_line_1 | VARCHAR(200) | M within group | 3–200 chars; trimmed | Plot 12, GIDC Electronic Estate | |
| {prefix}_line_2 | VARCHAR(200) | O | ≤ 200 | Sector 25 | |
| {prefix}_city | VARCHAR(100) | M within group | 2–100 | Gandhinagar | |
| {prefix}_district | VARCHAR(100) | O | ≤ 100 | Gandhinagar | |
| {prefix}_state | VARCHAR(100) | M within group | If country = IN: must match `states` reference (name) | Gujarat | |
| {prefix}_state_code | CHAR(2) | C | Required if country = IN; derived from state using GST state code list | 24 | DER when country = IN |
| {prefix}_country | CHAR(2) | M within group | ISO 3166-1 alpha-2; default IN | IN | |
| {prefix}_postal_code | VARCHAR(10) | M within group | V-POSTAL (§21.1) by country | 382025 | |

"M within group" means mandatory if the address group as a whole is mandatory or if any field of the group is supplied.

### 7.2 Organization Fields (`organizations`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | Immutable PK | | |
| organization_code | VARCHAR(10) | M | Upper-cased; `^[A-Z][A-Z0-9]{1,9}$`; unique system-wide; immutable once any employee code has been issued | ABC | Used by `{ORG}` token |
| group_id | UUID | O | FK organization_groups.id | | §7.6 |
| legal_name | VARCHAR(200) | M | 2–200 chars; duplicate legal names across organizations → warning W-ORG-NAME | ABC Technologies Private Limited | |
| display_name | VARCHAR(100) | M | 2–100 chars | ABC Tech | |
| entity_type | ENUM | M | PRIVATE_LIMITED, PUBLIC_LIMITED, OPC, LLP, PARTNERSHIP, PROPRIETORSHIP, TRUST, SOCIETY, SECTION_8_COMPANY, OTHER | PRIVATE_LIMITED | Drives §7.3 |
| industry | LOOKUP(INDUSTRY) | O | Active lookup value | IT_SERVICES | |
| date_of_incorporation | DATE | O | 1850-01-01 ≤ date ≤ today | 2015-06-12 | |
| logo_document_id | UUID | O | FK organization_files.id (same organization); PNG/JPEG/SVG-sanitized not allowed → PNG/JPEG only; ≤ 1 MB | | Stored via the secure storage service |
| website | VARCHAR(255) | O | V-URL | https://abctech.example.in | |
| status | ENUM | M | ACTIVE, INACTIVE; default ACTIVE | ACTIVE | §7.7 |
| cin | VARCHAR(21) | C | Required when entity_type ∈ {PRIVATE_LIMITED, PUBLIC_LIMITED, OPC, SECTION_8_COMPANY}; MUST be null otherwise; V-CIN; unique system-wide | U72200GJ2015PTC083456 | §7.3 ownership-code warning |
| llpin | VARCHAR(8) | C | Required when entity_type = LLP; MUST be null otherwise; V-LLPIN; unique system-wide | AAB-1234 | |
| pan | VARCHAR(10) | C | Required when `organization_settings.org_pan_required = true` (default true); V-PAN; 4th-character check per §7.3 (warning); unique system-wide except PROPRIETORSHIP (warning only) | AABCA1234C | |
| tan | VARCHAR(10) | O | V-TAN | AHMA12345B | Primary TAN; additional TANs via registrations (type TAN) |
| udyam_registration_number | VARCHAR(19) | O | V-UDYAM | UDYAM-GJ-08-0012345 | |
| registered_address | address group | M | §7.1 | | Registered office |
| corporate_address | address group | O | §7.1 | | Corporate/head office |
| official_phone | VARCHAR(16) | O | V-PHONE; stored E.164 | +917926000000 | |
| official_email | VARCHAR(254) | O | V-EMAIL; stored lower-case | hr@abctech.example.in | |
| hr_contact_name | VARCHAR(150) | O | V-NAME | Priya Shah | |
| hr_contact_email | VARCHAR(254) | O | V-EMAIL | priya.shah@abctech.example.in | |
| hr_contact_phone | VARCHAR(16) | O | V-PHONE | +919876543210 | |
| financial_year_start_month | SMALLINT | M | 1–12; default 4 | 4 | Used by `{YEAR}` FY formats |
| default_currency | CHAR(3) | M | ISO 4217; default INR | INR | Default for salary templates |
| timezone | VARCHAR(50) | M | IANA name; default Asia/Kolkata | Asia/Kolkata | Defines "today" |
| employee_id_format_settings | JSON(schema) | O | Allowed keys only: `default_separator` (one of "-", "/", "_", ""), `default_sequence_padding_length` (1–10), `default_sequence_start` (≥ 0), `uppercase_codes` (boolean, default true). Unknown keys → 422 | {"default_separator":"-","default_sequence_padding_length":4} | Defaults used to pre-fill new ID rules only; never used directly for generation |
| created_at, created_by, updated_at, updated_by | per §1.5 | SYS | | | Standard columns also include row_version, archived_* |

### 7.3 Entity Type → Identifier Applicability

| entity_type | CIN | LLPIN | Expected PAN 4th character (warning W-PAN-TYPE if mismatch) | Expected CIN ownership segment (chars 13–15; warning W-CIN-TYPE) |
|---|---|---|---|---|
| PRIVATE_LIMITED | Required | Must be null | C | PTC |
| PUBLIC_LIMITED | Required | Must be null | C | PLC |
| OPC | Required | Must be null | C | OPC |
| SECTION_8_COMPANY | Required | Must be null | C | NPL |
| LLP | Must be null | Required | F | — |
| PARTNERSHIP | Must be null | Must be null | F | — |
| PROPRIETORSHIP | Must be null | Must be null | P (proprietor's PAN) | — |
| TRUST | Must be null | Must be null | T | — |
| SOCIETY | Must be null | Must be null | No check (configurable) | — |
| OTHER | Must be null | Must be null | No check | — |

Ownership-segment and PAN-type mismatches are **warnings** (government companies, subsidiaries of foreign companies and other cases legitimately differ). The authoritative validation patterns are configurable by the SSA in `identifier_type_configs` (§21.2).

### 7.4 Organization Registrations (`organization_registrations`)
Statutory and state-specific registrations, including multiple GSTINs, are separate rows. No organization is required to have any particular registration.

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | FK organizations.id | | |
| branch_id | UUID | O | Composite FK to branches (same organization) | | Null = organization- or state-level |
| registration_type | ENUM | M | See table below | GSTIN | |
| registration_label | VARCHAR(100) | C | Required when registration_type = OTHER | Trade licence | |
| registration_number | VARCHAR(50) | M | Normalized upper-case, spaces removed; validated per type | 24AABCA1234C1Z5 | |
| state_code | CHAR(2) | C | Required for state-scoped types (see table); must be a valid GST state code; for GSTIN must equal chars 1–2 of the number | 24 | |
| issuing_authority | VARCHAR(150) | O | ≤ 150 | Labour Department, Gujarat | |
| issue_date | DATE | O | ≤ today | 2019-04-01 | |
| effective_from | DATE | O | ≤ effective_to if both set | 2019-04-01 | |
| effective_to | DATE | O | > effective_from; exclusive | 2029-04-01 | Expiry; registration shown as "Expired" when ≤ today |
| status | ENUM | M | ACTIVE, INACTIVE | ACTIVE | |
| document_file_id | UUID | O | FK organization_files (same organization) | | Certificate copy |
| remarks | VARCHAR(500) | O | | | |
| standard columns | §1.5 | SYS | | | |

| registration_type | Scope | Validation | Uniqueness |
|---|---|---|---|
| GSTIN | State (optional branch) | V-GSTIN; chars 3–12 MUST equal organization PAN (error E-GSTIN-PAN) | Unique system-wide (a GSTIN belongs to one legal entity) |
| TAN | Organization or branch | V-TAN | Unique within organization |
| PF_ESTABLISHMENT | Organization or branch | V-PF-EST | Unique system-wide |
| ESIC_EMPLOYER | Organization | V-ESIC-EMP | Unique system-wide |
| ESIC_SUB_CODE | State (optional branch) | V-REGNO | Unique within organization + state |
| PT_REGISTRATION | State | V-REGNO; optional per-state pattern | Unique within organization + state |
| PT_ENROLMENT | State | V-REGNO | Unique within organization + state |
| LWF | State | V-REGNO | Unique within organization + state |
| SHOPS_ESTABLISHMENTS | Branch | V-REGNO | Unique within organization |
| FACTORY_LICENSE | Branch | V-REGNO | Unique within organization |
| CONTRACT_LABOUR_LICENSE | Branch | V-REGNO | Unique within organization |
| OTHER | Any | V-REGNO | Unique within organization + label |

Multiple rows of the same type and state are permitted only with non-overlapping effective intervals (re-registration history).

### 7.5 Organization Settings (`organization_settings`, 1:1 with organization)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| organization_id | UUID | M | PK and FK organizations.id | | |
| org_pan_required | BOOLEAN | M | Default true | true | §7.2 |
| employee_code_assignment_point | ENUM | M | ON_CREATE (default), ON_ACTIVATION | ON_CREATE | §19.6 |
| employee_id_date_basis | ENUM | M | JOINING_DATE (default), CREATION_DATE | JOINING_DATE | §19.5 |
| allow_manual_employee_code | BOOLEAN | M | Default false | false | §19.8 |
| official_email_uniqueness_scope | ENUM | M | ORGANIZATION (default), GROUP, NONE | ORGANIZATION | §10.5 |
| minimum_employment_age_years | SMALLINT | M | 14–21; default 18 | 18 | Below this → age exception (§10.4) |
| absolute_minimum_age_years | SMALLINT | M | 14–18; ≤ minimum_employment_age_years; default 14 | 14 | Below this → blocked. Legal review (§25.8) |
| require_father_name | BOOLEAN | M | Default false | false | |
| require_emergency_contact | BOOLEAN | M | Default false | false | |
| collect_disability_info | BOOLEAN | M | Default false | false | Fields rejected (422 FIELD_NOT_COLLECTED) when false |
| collect_religion | BOOLEAN | M | Default false | false | |
| religion_collection_purpose | TEXT | C | Required (≥ 30 chars) when collect_religion = true | | Documented lawful purpose |
| collect_social_category | BOOLEAN | M | Default false | false | |
| social_category_collection_purpose | TEXT | C | Required (≥ 30 chars) when collect_social_category = true | | |
| referral_requires_referrer | BOOLEAN | M | Default true | true | §10.6 |
| require_primary_bank_account | BOOLEAN | M | Default false | false | §14.2 |
| salary_revision_approval_required | BOOLEAN | M | Default false | false | §17.4 |
| allow_custom_salary_components | BOOLEAN | M | Default false | false | §17.3 |
| backdated_salary_max_days | SMALLINT | M | 0–3650; default 365; 0 disables backdating | 365 | §18.4 |
| status_correction_window_days | SMALLINT | M | 0–365; default 30 | 30 | §10.3 |
| document_expiry_warning_days | SMALLINT | M | 1–365; default 30 | 30 | §15.8 |
| allow_document_self_verification | BOOLEAN | M | Default false | false | §15.7 |
| audit_all_document_downloads | BOOLEAN | M | Default true; downloads of restricted categories are always audited | true | |
| enable_employee_self_view | BOOLEAN | M | Default false | false | §5.6 |
| self_view_salary | BOOLEAN | M | Default false | false | |
| date_display_format | ENUM | M | DD-MM-YYYY (default), YYYY-MM-DD | DD-MM-YYYY | Display only |

Related configuration tables (organization-scoped): `identifier_requirement_policies` (§11.3) and `nomination_type_configs` (§13.3).

### 7.6 Organization Groups (`organization_groups`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| group_code | VARCHAR(10) | M | `^[A-Z][A-Z0-9]{1,9}$`; unique | ABCGRP | |
| group_name | VARCHAR(150) | M | 2–150 | ABC Group | |
| status | ENUM | M | ACTIVE, INACTIVE | ACTIVE | |
| standard columns | §1.5 | SYS | | | |

Groups have no data of their own; they enable (a) multi-organization grants created by the SSA, (b) group-scoped duplicate identifier detection (§11.5), (c) GROUP official-email uniqueness scope.

### 7.7 Organization Lifecycle Rules
- Organizations are never hard-deleted once any employee, user grant or registration exists.
- Setting status INACTIVE: blocks creation of employees, rules, templates and documents; existing data remains readable to authorized users; edits are blocked except by HRA for corrections (audited) [ASSUMPTION].
- `organization_code` becomes immutable after the first employee code is issued (codes embed it).
- Changing `timezone` or `financial_year_start_month` after employee codes have been issued requires OA confirmation with a warning that future `{YEAR}` tokens and "today" boundaries change; historical data is not modified.

---

## 8. Branch / Location Management

### 8.1 Branch Fields (`branches`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | FK organizations.id; immutable | | |
| branch_code | VARCHAR(30) | M | V-CODE; unique within organization (case-insensitive); immutable once referenced by an employee | GNR01 | |
| branch_name | VARCHAR(150) | M | 2–150; unique within organization among ACTIVE branches (warning otherwise) | Gandhinagar Office | |
| address_line_1 | VARCHAR(200) | M | 3–200 | Plot 12, GIDC Electronic Estate | |
| address_line_2 | VARCHAR(200) | O | ≤ 200 | Sector 25 | |
| city | VARCHAR(100) | M | 2–100 | Gandhinagar | |
| district | VARCHAR(100) | O | ≤ 100 | Gandhinagar | |
| state | VARCHAR(100) | M | If country = IN: from states reference | Gujarat | Drives PT/LWF default state |
| state_code | CHAR(2) | C | Required when country = IN; DER from state | 24 | |
| country | CHAR(2) | M | ISO 3166-1; default IN | IN | |
| postal_code | VARCHAR(10) | M | V-POSTAL | 382025 | |
| official_phone | VARCHAR(16) | O | V-PHONE | +917926000000 | |
| official_email | VARCHAR(254) | O | V-EMAIL | gnr.office@abctech.example.in | |
| gstin | VARCHAR(15) | DER | Resolved: ACTIVE GSTIN registration with this branch_id; else ACTIVE organization-level GSTIN with matching state_code; else null | 24AABCA1234C1Z5 | Maintained via §7.4; avoids duplicate storage |
| shops_establishments_registration_number | VARCHAR(50) | DER | ACTIVE SHOPS_ESTABLISHMENTS registration with this branch_id | | Maintained via §7.4 |
| factory_license_number | VARCHAR(50) | DER | ACTIVE FACTORY_LICENSE registration with this branch_id | | Maintained via §7.4 |
| status | ENUM | M | ACTIVE, INACTIVE | ACTIVE | |
| created_at, created_by, updated_at, updated_by | §1.5 | SYS | | | |

**Design decision:** Branch GSTIN, Shops & Establishments and factory licence numbers are stored once in `organization_registrations` (with `branch_id`) and exposed on the branch API as derived read-only fields. The branch create/edit API accepts these three fields as a convenience; the service writes or updates the corresponding registration row in the same transaction.

### 8.2 Branch Constraints and Behaviour
- Branch code is unique within its organization: `UNIQUE (organization_id, upper(branch_code))`.
- Every branch belongs to exactly one organization; `organization_id` is immutable.
- An employee's branch MUST belong to the employee's organization (composite FK).
- Branches referenced by any employee, job-history row, registration or work location MUST NOT be deleted; they may be set INACTIVE.
- INACTIVE branches cannot be assigned to new employees or new job changes; existing assignments remain valid and display with an "Inactive" badge.
- A branch with ACTIVE employees currently assigned may be deactivated only after confirming a warning listing the count of affected employees (no automatic reassignment).
- Hard delete is permitted only if the branch has never been referenced (audited).

---

## 9. Organization-Specific Master Data

### 9.1 Common Master Fields
Applies to: `departments`, `sub_departments`, `designations`, `grades`, `business_units`, `cost_centers`, `employment_types`, `work_locations`, `employee_categories`.

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | FK organizations.id; immutable | | |
| code | VARCHAR(30) | M | V-CODE; unique within (organization, master type), case-insensitive; immutable once referenced | IT | Department code feeds `{DEPT}` token |
| name | VARCHAR(150) | M | 2–150; duplicate name within the same master and organization among ACTIVE rows → warning | Information Technology | |
| description | VARCHAR(500) | O | ≤ 500 | | |
| display_order | INTEGER | O | ≥ 0 | 10 | Sorting in dropdowns; ties by name |
| status | ENUM | M | ACTIVE, INACTIVE | ACTIVE | |
| standard columns | §1.5 | SYS | | | |

### 9.2 Master-Specific Fields

| Master | Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|---|
| departments | head_employee_id | UUID | O | Composite FK employees (same organization); employee status ACTIVE/ON_NOTICE at time of assignment | | Used for DM scope |
| sub_departments | department_id | UUID | M | Composite FK departments (same organization); immutable once referenced | | Code unique within organization (not just department) |
| designations | default_grade_id | UUID | O | Composite FK grades | | Pre-fills grade on employee form |
| grades | rank | SMALLINT | M | 1–999; unique within organization | 3 | Lower rank = more junior [ASSUMPTION] |
| business_units | head_employee_id | UUID | O | Composite FK employees | | |
| cost_centers | business_unit_id | UUID | O | Composite FK business_units | | |
| employment_types | base_type | ENUM | M | PERMANENT, PROBATION, FIXED_TERM, CONTRACT, TRAINEE, INTERN, CONSULTANT, OTHER; immutable once referenced | FIXED_TERM | Seeded: one row per base type |
| employment_types | requires_end_date | BOOLEAN | M | Default true for FIXED_TERM, false otherwise | true | §10.7 |
| work_locations | branch_id | UUID | O | Composite FK branches | | |
| work_locations | is_remote | BOOLEAN | M | Default false | false | |
| work_locations | address group (prefix `address`) | §7.1 | C | Required when is_remote = false and branch_id is null | | |
| employee_categories | (none) | | | | | E.g., Staff, Workman, Management |

### 9.3 Configurable Lookups (`lookup_values`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | FK organizations.id | | Seeded per organization on creation |
| list_code | ENUM | M | One of the lists below | RELATIONSHIP | |
| value_code | VARCHAR(50) | M | `^[A-Z0-9_]{1,50}$`; unique within (organization, list); immutable | SPOUSE | Stored on referencing rows |
| label | VARCHAR(100) | M | 1–100 | Spouse | Editable |
| display_order | INTEGER | M | ≥ 0 | 10 | |
| status | ENUM | M | ACTIVE, INACTIVE | ACTIVE | |
| is_system_default | BOOLEAN | SYS | | true | Seeded values; can be deactivated, not deleted |

Behaviour: inactive values cannot be selected for new or changed data; existing references remain valid and display their label. A value can be hard-deleted only if never referenced. Administrators (OA, or HRA when master.manage is enabled) add values, edit labels, reorder and deactivate.

| list_code | Default values (value_code) | Editable? |
|---|---|---|
| GENDER | MALE, FEMALE, TRANSGENDER, OTHER, UNDISCLOSED | Yes (UNDISCLOSED cannot be deactivated) |
| MARITAL_STATUS | SINGLE, MARRIED, DIVORCED, WIDOWED, SEPARATED, UNDISCLOSED | Yes |
| BLOOD_GROUP | A_POS, A_NEG, B_POS, B_NEG, AB_POS, AB_NEG, O_POS, O_NEG, UNKNOWN | No (system fixed) |
| RELATIONSHIP | SPOUSE, FATHER, MOTHER, SON, DAUGHTER, BROTHER, SISTER, FATHER_IN_LAW, MOTHER_IN_LAW, GRANDPARENT, GUARDIAN, FRIEND, OTHER | Yes |
| QUALIFICATION_LEVEL | SECONDARY_10, HIGHER_SECONDARY_12, ITI, DIPLOMA, GRADUATE, POST_GRADUATE, DOCTORATE, PROFESSIONAL_CERTIFICATION, OTHER | Yes |
| STUDY_MODE | FULL_TIME, PART_TIME, DISTANCE, ONLINE, OTHER | Yes |
| SOURCE_OF_HIRE | DIRECT, REFERRAL, JOB_PORTAL, CAMPUS, AGENCY, SOCIAL_MEDIA, INTERNAL_TRANSFER, REHIRE, OTHER | Yes |
| DISABILITY_TYPE | Seeded from the categories in the Rights of Persons with Disabilities Act, 2016 (to be confirmed by legal review) plus OTHER | Yes |
| BANK_ACCOUNT_TYPE | SAVINGS, SALARY, CURRENT, NRE, NRO, OTHER | Yes |
| INDUSTRY | IT_SERVICES, MANUFACTURING, BFSI, HEALTHCARE, RETAIL, EDUCATION, CONSTRUCTION, LOGISTICS, OTHER | Yes |
| BGV_STATUS | NOT_INITIATED, IN_PROGRESS, CLEAR, DISCREPANCY, UNABLE_TO_VERIFY | Yes |
| STATUS_REASON | RESIGNATION, TERMINATION_MISCONDUCT, TERMINATION_PERFORMANCE, CONTRACT_END, RETIREMENT_SUPERANNUATION, VOLUNTARY_RETIREMENT, DEATH, INTER_ORG_TRANSFER, SUSPENSION, SABBATICAL, ABSENCE_UNAUTHORIZED, RESIGNATION_WITHDRAWN, RETURN_TO_WORK, DATA_CORRECTION, OTHER | Yes |
| JOB_CHANGE_TYPE | JOINING, TRANSFER, PROMOTION, REDESIGNATION, MANAGER_CHANGE, RESTRUCTURE, CORRECTION, OTHER | Yes |
| SALARY_REVISION_REASON | NEW_JOINER, ANNUAL_REVISION, PROMOTION, MARKET_CORRECTION, ROLE_CHANGE, CONFIRMATION, STRUCTURE_CHANGE, DATA_CORRECTION, OTHER | Yes |
| DOCUMENT_TYPE | Per category sub-types (e.g., MARKSHEET, DEGREE_CERTIFICATE) | Yes |
| RELIGION | Empty by default; only usable when collect_religion = true | Yes |
| SOCIAL_CATEGORY | Empty by default; only usable when collect_social_category = true | Yes |

### 9.4 Master Rules
- Codes are unique within the organization and master type; comparison is case-insensitive after trimming.
- An employee's assigned masters MUST belong to the employee's organization (composite FKs, §6.3). Group-level shared masters are not supported in v1.
- Masters referenced by any employee, job-history row, rule or template MUST NOT be deleted; deactivate instead. Deactivated masters remain visible in history.
- Deactivating a department that is referenced by an ACTIVE ID rule requires the rule to be ended or archived first (error `MASTER_IN_USE_BY_ID_RULE`).

---

## 10. Employee Management and Complete Field Catalog

### 10.1 Employment Record Model
- `employees` holds one employment record: one person, one organization, one continuous engagement.
- The internal `id` is immutable and separate from the human-readable `employee_code`.
- Rehire (same organization) and inter-organization transfer create a **new** employment record linked via `previous_employment_record_id`; the previous record is never modified except for its own separation fields.
- Current job attributes are stored on `employees` for querying; every change is also written to `employee_job_history` in the same transaction.

### 10.2 Employee Statuses

| Status | Meaning | Required data when entering this status |
|---|---|---|
| DRAFT | Record being prepared; not yet an employee | organization_id, first_name; plus department_id and joining_date when employee_code_assignment_point = ON_CREATE |
| ACTIVE | Currently employed | All M-A fields; employee_code; status_effective_date (= joining_date on first activation; = return date on reactivation) |
| ON_NOTICE | Separation initiated; still employed | status_effective_date (notice start, ≥ joining_date); employment_end_date (expected last working day, ≥ status_effective_date); status_reason |
| INACTIVE | Employment continues but the person is temporarily not working (e.g., suspension, sabbatical) as a recorded fact | status_effective_date; status_reason |
| RESIGNED | Employment ended by resignation | employment_end_date ≤ today; status_effective_date = employment_end_date; status_reason |
| TERMINATED | Employment ended by employer | employment_end_date ≤ today; status_effective_date = employment_end_date; status_reason |
| RETIRED | Employment ended by retirement | employment_end_date ≤ today; status_effective_date = employment_end_date; status_reason |
| ABSCONDING | Unauthorized absence declared | status_effective_date (first day of absence, ≤ today); status_reason; employment_end_date optional |
| Custom (OTHER) | Organization-defined status (e.g., TRANSFERRED) | Inherits rules of its `base_behaviour`: ACTIVE_LIKE (as INACTIVE), SEPARATED (as RESIGNED) |

Custom statuses are defined in `employee_custom_statuses` (organization_id, code `^[A-Z_]{2,30}$`, label, base_behaviour ENUM{ACTIVE_LIKE, SEPARATED}, status). Separated-like statuses are terminal.

Business rule: separated statuses (RESIGNED, TERMINATED, RETIRED, separated custom) can be set only when `employment_end_date ≤ today`. Before that, use ON_NOTICE. This avoids future-dated status scheduling.

### 10.3 Status Transition Matrix
Y = allowed with employee.status; R = allowed with employee.status_restricted only; N = not allowed.

| From \ To | ACTIVE | ON_NOTICE | INACTIVE | RESIGNED | TERMINATED | RETIRED | ABSCONDING | Custom SEPARATED |
|---|---|---|---|---|---|---|---|---|
| DRAFT | Y (activation) | N | N | N | N | N | N | N |
| ACTIVE | — | Y | Y | Y | R | Y | R | Y |
| ON_NOTICE | Y (withdrawal: clears employment_end_date; reason RESIGNATION_WITHDRAWN) | — | N | Y | R | Y | R | Y |
| INACTIVE | Y | Y | — | Y | R | Y | R | Y |
| ABSCONDING | R (return; reason RETURN_TO_WORK) | N | N | Y | R | N | — | Y |
| RESIGNED / TERMINATED / RETIRED / Custom SEPARATED | Correction only (R) | Correction only (R) | N | Correction only (R) | Correction only (R) | Correction only (R) | N | Correction only (R) |

**Status correction:** A user with employee.status_restricted may revert or change a separated status within `status_correction_window_days` of the status change, with reason code DATA_CORRECTION and remarks ≥ 20 chars. The history row is marked `is_correction = true`. Outside the window, corrections are blocked (`STATUS_CORRECTION_WINDOW_EXPIRED`); rehire must be used instead.

**DRAFT discard:** Drafts are not deleted; they are archived (`archived_at`) with a reason. Any employee code issued to a discarded draft is retired, never reused (§19.7).

**Activation checks (DRAFT → ACTIVE):** all M-A fields present; employee code assigned (generated at this point if ON_ACTIVATION); age policy satisfied or exception APPROVED (§10.4); identifier requirement policies satisfied (§11.3); mandatory document categories present if configured (§15.2); primary bank account present if `require_primary_bank_account`.

### 10.4 Basic and Personal Information (`employees`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | Immutable PK | | Never use employee_code as PK |
| organization_id | UUID | M | FK organizations.id; immutable; organization must be ACTIVE for create | | |
| employee_code | VARCHAR(30) | SYS (C) | Generated per §19; unique `(organization_id, upper(employee_code))` across all records including archived; null only in DRAFT when assignment point = ON_ACTIVATION; immutable except via reassignment (§19.8) | ABC-IT-0001 | Manual entry only with employee.code_manual |
| first_name | VARCHAR(100) | M | V-NAME | Rajesh | |
| middle_name | VARCHAR(100) | O | V-NAME | Kumar | |
| last_name | VARCHAR(100) | O | V-NAME | Patel | Optional: many Indian names have no surname |
| display_name | VARCHAR(200) | O | V-NAME (≤ 200); default DER = first + middle + last joined by single spaces | Rajesh K. Patel | Used in lists |
| full_name_normalized | VARCHAR(300) | SYS | Lower-case, NFC-normalized, whitespace collapsed concatenation of name parts | rajesh kumar patel | Search only |
| father_name | VARCHAR(150) | C | V-NAME; M-A when organization_settings.require_father_name = true | Kumar Patel | |
| mother_name | VARCHAR(150) | O | V-NAME | Meena Patel | |
| spouse_name | VARCHAR(150) | O | V-NAME | Anita Patel | |
| date_of_birth | DATE | M-A | ≤ today; ≥ 1900-01-01; age rules below | 1992-08-15 | |
| age_exception_status | ENUM | SYS | NONE (default), PENDING, APPROVED, REJECTED | NONE | Set when age at joining < minimum_employment_age_years |
| age_exception_reason | VARCHAR(500) | C | Required (≥ 20 chars) when exception requested | Apprentice under approved programme | |
| age_exception_decided_by | UUID | SYS | FK users; must differ from requester | | HRA only |
| age_exception_decided_at | TIMESTAMP | SYS | | | |
| gender | LOOKUP(GENDER) | M-A | Active lookup value; UNDISCLOSED always available | FEMALE | |
| marital_status | LOOKUP(MARITAL_STATUS) | O | Active lookup value | MARRIED | |
| nationality | CHAR(2) | M-A | ISO 3166-1 alpha-2; default IN | IN | |
| blood_group | LOOKUP(BLOOD_GROUP) | O | System list | O_POS | |
| photograph_document_id | UUID | O | Composite FK employee_documents (same employee, category PHOTOGRAPH, AVAILABLE) | | |
| religion | LOOKUP(RELIGION) | C [R] | Accepted only when collect_religion = true; otherwise 422 FIELD_NOT_COLLECTED | | sensitive.restricted_personal |
| social_category | LOOKUP(SOCIAL_CATEGORY) | C [R] | Accepted only when collect_social_category = true | | sensitive.restricted_personal |
| disability_status | ENUM | O [R] | YES, NO, UNDISCLOSED; accepted only when collect_disability_info = true | NO | |
| disability_type | LOOKUP(DISABILITY_TYPE) | C [R] | Allowed only when disability_status = YES; otherwise must be null | | |
| disability_percentage | DECIMAL(5,2) | C [R] | Allowed only when disability_status = YES; 0.00–100.00 | 45.00 | |
| employment_transition_type | ENUM | M | NEW_HIRE (default), REHIRE, INTER_ORG_TRANSFER | NEW_HIRE | §10.9 |
| created_at, created_by, updated_at, updated_by | §1.5 | SYS | | | |
| archived_at | TIMESTAMP | SYS | §1.5 | | |

**Name rules:** Names support Unicode letters and combining marks (including Indic scripts), initials with periods, spaces, apostrophes and hyphens (V-NAME). Inputs are trimmed, NFC-normalized and internal whitespace collapsed. No minimum surname requirement.

**Age rules** (computed on joining_date; if joining_date null, on today):
- Age < absolute_minimum_age_years → blocked: `422 AGE_BELOW_ABSOLUTE_MINIMUM`.
- absolute_minimum_age_years ≤ age < minimum_employment_age_years → record may be saved in DRAFT with `age_exception_status = PENDING`; activation blocked until an HRA (not the requester) sets APPROVED with a reason. REJECTED keeps activation blocked.
- Age > 100 → warning W-AGE-HIGH (must be acknowledged).
- Under-18 employees trigger the children's-data review item (§25.8).

### 10.5 Contact Information

**Storage approach:** phone/email/emergency fields are columns on `employees`. Addresses are rows in `employee_addresses` with `address_type ∈ {CURRENT, PERMANENT}` (unique per employee and type). When `permanent_address_same_as_current = true`, **no PERMANENT row is stored**; the API returns `permanent_*` values copied from CURRENT and marked derived. Submitting any non-null `permanent_*` field together with `permanent_address_same_as_current = true` → `422 CONFLICTING_PERMANENT_ADDRESS`. Setting the flag to true deletes the PERMANENT row in the same transaction (audited with before values).

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| personal_mobile | VARCHAR(16) | M-A [R-lite] | V-PHONE; stored E.164; India default country | +919876543210 | Visible with employee.view_personal |
| alternate_mobile | VARCHAR(16) | O | V-PHONE; must differ from personal_mobile | +919812345678 | |
| personal_email | VARCHAR(254) | O | V-EMAIL; stored lower-case | rajesh.patel@example.com | |
| official_email | VARCHAR(254) | O | V-EMAIL; stored lower-case; uniqueness per official_email_uniqueness_scope among employees with status ∉ {RESIGNED, TERMINATED, RETIRED, custom SEPARATED} and not archived | rajesh.patel@abctech.example.in | ORGANIZATION: unique per org; GROUP: unique per group; NONE: not enforced |
| current_address_line_1 | VARCHAR(200) | M-A | 3–200 | 22, Shanti Nagar Society | employee_addresses (CURRENT) |
| current_address_line_2 | VARCHAR(200) | O | ≤ 200 | Near Sector 21 Market | |
| current_city | VARCHAR(100) | M-A | 2–100 | Gandhinagar | |
| current_district | VARCHAR(100) | O | ≤ 100 | Gandhinagar | |
| current_state | VARCHAR(100) | M-A | IN: states reference | Gujarat | |
| current_postal_code | VARCHAR(10) | M-A | V-POSTAL by country | 382021 | |
| current_country | CHAR(2) | M-A | ISO 3166-1; default IN | IN | |
| permanent_address_line_1 | VARCHAR(200) | C | Required (M-A) when permanent_address_same_as_current = false | Village Rampura | employee_addresses (PERMANENT) |
| permanent_address_line_2 | VARCHAR(200) | O | ≤ 200 | | |
| permanent_city | VARCHAR(100) | C | As line_1 | Mehsana | |
| permanent_district | VARCHAR(100) | O | | Mehsana | |
| permanent_state | VARCHAR(100) | C | As line_1; IN: states reference | Gujarat | |
| permanent_postal_code | VARCHAR(10) | C | V-POSTAL | 384001 | |
| permanent_country | CHAR(2) | C | ISO 3166-1 | IN | |
| permanent_address_same_as_current | BOOLEAN | M | Default false | true | |
| emergency_contact_name | VARCHAR(150) | C | V-NAME; if any emergency field supplied, name, relationship and phone are all required; M-A when require_emergency_contact = true | Anita Patel | Personal data |
| emergency_contact_relationship | LOOKUP(RELATIONSHIP) | C | As above | SPOUSE | |
| emergency_contact_phone | VARCHAR(16) | C | V-PHONE; must differ from personal_mobile (warning W-EMERGENCY-SAME) | +919898989898 | |
| emergency_contact_alternate_phone | VARCHAR(16) | O | V-PHONE | | |

`employee_addresses` columns: standard columns (§1.5), organization_id, employee_id, address_type, and the address group columns (line_1, line_2, city, district, state, state_code, country, postal_code).

### 10.6 Employment Information (`employees`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| organization_id | UUID | M | See §10.4 | | |
| employee_code | VARCHAR(30) | SYS (C) | See §10.4 | ABC-IT-0001 | |
| joining_date | DATE | M-A (M in DRAFT if ON_CREATE) | 1950-01-01 ≤ date ≤ today + 365 days; ≥ date_of_birth + absolute_minimum_age_years | 2026-11-02 | Date of joining this organization under this record |
| employment_type_id | UUID | M-A | Composite FK employment_types (ACTIVE at assignment) | | Base types: Permanent, Probation, Fixed-Term, Contract, Trainee, Intern, Consultant, Other |
| employment_start_date | DATE | M-A | Default = joining_date; MUST equal joining_date unless open question Q-03 resolves otherwise | 2026-11-02 | Start of this engagement |
| employment_end_date | DATE | C | Inclusive last day. ≥ employment_start_date. Required when status is ON_NOTICE or separated, and when employment type requires_end_date = true (contract end) | 2027-11-01 | §10.7 |
| probation_duration_months | SMALLINT | O | 0–24 | 6 | |
| probation_end_date | DATE | O | ≥ joining_date; default DER = joining_date + probation_duration_months − 1 day (editable) | 2027-05-01 | |
| confirmation_date | DATE | O | ≥ joining_date; < probation_end_date → warning W-CONFIRM-EARLY | 2027-05-02 | |
| designation_id | UUID | M-A | Composite FK designations (ACTIVE at assignment) | | |
| department_id | UUID | M-A (M in DRAFT if ON_CREATE) | Composite FK departments (ACTIVE at assignment) | | Drives ID rule selection |
| sub_department_id | UUID | O | Composite FK sub_departments; sub-department.department_id = department_id | | |
| grade_id | UUID | O | Composite FK grades | | |
| business_unit_id | UUID | O | Composite FK business_units | | |
| cost_center_id | UUID | O | Composite FK cost_centers | | |
| branch_id | UUID | M-A | Composite FK branches (ACTIVE at assignment) | | |
| work_location_id | UUID | O | Composite FK work_locations | | Distinct from branch where applicable |
| employee_category_id | UUID | O | Composite FK employee_categories | | |
| work_mode | ENUM | M-A | ON_SITE, HYBRID, REMOTE, OTHER; default ON_SITE | HYBRID | |
| reporting_manager_employee_id | UUID | O | Composite FK employees (same organization); ≠ id; no cycles (§10.7); manager status ∈ {ACTIVE, ON_NOTICE, INACTIVE} else warning | | |
| hr_contact_employee_id | UUID | O | Composite FK employees (same organization); ≠ id | | |
| notice_period_days | SMALLINT | O | 0–365 | 60 | |
| source_of_hire | LOOKUP(SOURCE_OF_HIRE) | O | Active value | REFERRAL | |
| referred_by_employee_id | UUID | C | Composite FK employees (same organization); ≠ id; required when source_of_hire = REFERRAL and referral_requires_referrer = true; must be null when source_of_hire ≠ REFERRAL | | |
| offer_letter_date | DATE | O | ≤ joining_date (warning if after) ; ≤ today | 2026-10-01 | |
| appointment_letter_date | DATE | O | ≤ today; < offer_letter_date → warning | 2026-11-02 | |
| status | ENUM / custom | SYS via transitions | Per §10.2; changed only via status endpoint | ACTIVE | |
| status_effective_date | DATE | SYS via transitions | Per §10.2 | 2026-11-02 | |
| status_reason | LOOKUP(STATUS_REASON) | C | Required for all statuses except DRAFT and first ACTIVE | | |
| previous_employment_record_id | UUID | C | FK employees.id (may be another organization); required when employment_transition_type ∈ {REHIRE, INTER_ORG_TRANSFER}; referenced record must be separated (or ON_NOTICE for transfers) and its employment_end_date < this joining_date | | §10.9 |
| remarks | TEXT | O [R] | ≤ 2,000 chars | | employee.remarks |

### 10.7 Employment Cross-Field Constraints

| Rule ID | Rule | Severity |
|---|---|---|
| EMP-01 | employment_end_date ≥ employment_start_date | Error |
| EMP-02 | confirmation_date ≥ joining_date | Error |
| EMP-03 | probation_end_date ≥ joining_date | Error |
| EMP-04 | If employment type requires_end_date = true, employment_end_date is M-A | Error |
| EMP-05 | reporting_manager_employee_id ≠ id; hr_contact_employee_id ≠ id; referred_by_employee_id ≠ id | Error |
| EMP-06 | No circular reporting: walking up the chain from the proposed manager (max depth 50) must not reach this employee; executed inside the write transaction with the manager chain rows locked (`SELECT … FOR UPDATE`) | Error `REPORTING_CYCLE` |
| EMP-07 | All master references belong to the employee's organization | Error `INVALID_ORGANIZATION_REFERENCE` |
| EMP-08 | Masters must be ACTIVE when newly assigned; previously assigned inactive masters may remain | Error `MASTER_INACTIVE` |
| EMP-09 | Changing joining_date after activation requires employee.status_restricted, a reason, and must keep all salary assignments ≥ joining_date | Error `JOINING_DATE_CONFLICT` |
| EMP-10 | Setting employment_end_date E: no confirmed salary assignment may start after E (§18.6) | Error `SALARY_AFTER_EMPLOYMENT_END` |
| EMP-11 | employee_code does not change when department, branch or any other attribute changes | System rule |
| EMP-12 | Job attribute changes are recorded with an effective_date (default today, ≤ today, ≥ joining_date, ≥ latest job-history effective_date unless change type CORRECTION by HRA) | Error `JOB_CHANGE_DATE_INVALID` |

### 10.8 Job History and Status History

**`employee_job_history`** (append-only snapshot of job attributes):

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | Composite FK with employee | | |
| employee_id | UUID | M | Composite FK employees | | |
| effective_date | DATE | M | EMP-12 | 2027-04-01 | |
| sequence_no | SMALLINT | SYS | Increments for multiple changes on the same date | 1 | |
| change_type | LOOKUP(JOB_CHANGE_TYPE) | M | | PROMOTION | |
| department_id, sub_department_id, designation_id, grade_id, business_unit_id, cost_center_id, branch_id, work_location_id, employment_type_id, employee_category_id, work_mode, reporting_manager_employee_id, hr_contact_employee_id | as in §10.6 | M/O as in §10.6 [H] | Full snapshot after the change | | |
| reason | VARCHAR(500) | O | Required for CORRECTION (≥ 10 chars) | | |
| created_at, created_by | TIMESTAMP, UUID | SYS | | | |

**`employee_status_history`** (append-only):

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id, employee_id | UUID | M | Composite FK | | |
| from_status | VARCHAR(30) | M | | ACTIVE | |
| to_status | VARCHAR(30) | M | Transition per §10.3 | ON_NOTICE | |
| status_effective_date | DATE | M | Per §10.2 | 2027-02-01 | |
| employment_end_date_snapshot | DATE | O [H] | | 2027-03-31 | |
| status_reason | LOOKUP(STATUS_REASON) | C | | RESIGNATION | |
| remarks | VARCHAR(1000) | O | Required (≥ 20) for corrections | | |
| is_correction | BOOLEAN | M | Default false | false | |
| created_at, created_by | | SYS | | | |

### 10.9 Rehire and Inter-Organization Transfer

**Rehire (same organization):**
1. User with employee.create starts a new record with `employment_transition_type = REHIRE` and `previous_employment_record_id`.
2. The previous record must be separated with employment_end_date < new joining_date.
3. The system offers to prefill personal, contact, identifiers, education, previous employment, family and nominees (user selects categories; copies are new rows). Bank accounts, salary assignments and documents are **not** copied by default; documents may be copied individually by users with document.upload (new rows referencing copied files).
4. A **new employee code** is always generated; previous codes are never reused (§19.7).
5. Identifier duplicate checks (§11.5) treat the linked previous record as a non-overlapping match (warning, not block).

**Inter-organization transfer:**
1. Requires employee.create in the target organization and employee.status in the source organization.
2. `POST /api/v1/orgs/{orgId}/employees/{id}/transfers` with target_organization_id, target joining_date, target department_id, data categories to copy, and source employment_end_date (default target joining_date − 1 day).
3. In one transaction: source record set to ON_NOTICE (if end date ≥ today) or to separated status RESIGNED with reason INTER_ORG_TRANSFER (or the organization's custom TRANSFERRED status) if end date < today; a new DRAFT record is created in the target organization with `INTER_ORG_TRANSFER` and the link; a new code is generated using target organization rules (if ON_CREATE).
4. Salary, bank and documents are not copied by default. Copying personal data between legal entities requires a lawful basis (§25.8); the transfer UI MUST display a confirmation that the copy has been authorized.

---

## 11. Employee Identity and Indian Statutory Reference Fields

### 11.1 Storage Model (`employee_identifiers`)
Identifiers are stored as rows (one current row per employee and type; history retained) rather than plaintext columns.

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id, employee_id | UUID | M | Composite FK employees | | |
| identifier_type | ENUM | M | PAN, AADHAAR, UAN, PF_MEMBER_ID, PREVIOUS_PF_MEMBER_ID, ESIC_IP, PASSPORT, VOTER_ID, DRIVING_LICENSE | PAN | |
| value_encrypted | BYTES | M [E][R] | AES-256-GCM ciphertext of normalized value | | Never logged |
| value_blind_index | BYTES(32) | SYS [R] | HMAC-SHA256(normalized value, blind-index key of the organization's group or of the organization if ungrouped) | | Exact-match search and duplicates |
| value_last_four | CHAR(4) | SYS | Last 4 characters of normalized value | 234F | Masked display |
| key_version | SMALLINT | SYS | | 3 | Key rotation |
| issuing_country | CHAR(2) | C | Required for PASSPORT; ISO 3166-1; default IN | IN | |
| issue_date | DATE | O | ≤ today | 2018-03-10 | Passport, DL |
| expiry_date | DATE | C | > issue_date; PASSPORT: required | 2028-03-09 | |
| issue_place | VARCHAR(100) | O | | Ahmedabad | Passport |
| is_current | BOOLEAN | SYS | Exactly one current row per (employee, type), except PREVIOUS_PF_MEMBER_ID (multiple current allowed) | true | Changing a value creates a new row; old row is_current = false |
| verification_status | ENUM | O | UNVERIFIED (default), VERIFIED, REJECTED | UNVERIFIED | Manual flag; no external verification in scope |
| verified_by, verified_at | UUID, TIMESTAMP | SYS | Set when VERIFIED/REJECTED | | |
| supporting_document_id | UUID | O | Composite FK employee_documents (same employee) | | |
| duplicate_override_reason | VARCHAR(500) | C | Required when saved despite a blocking duplicate (identity.duplicate_override) | | |
| standard columns | §1.5 | SYS | | | |

### 11.2 Logical Identifier Fields (API representation)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| pan | VARCHAR(10) | Per policy (default O) [E][R] | V-PAN; 4th character must be P (individual), configurable severity default Error | ABCPE1234F | Stored as identifier_type PAN |
| aadhaar_number | VARCHAR(12) | Per policy (default NOT_COLLECTED) [E][R] | V-AADHAAR (12 digits, first digit 2–9, Verhoeff checksum); input spaces/hyphens stripped | 234567890124 | Never shown in full in lists/details; never a key |
| aadhaar_last_four | CHAR(4) | DER | From value_last_four | 0124 | Masked display "XXXX XXXX 0124" |
| uan | VARCHAR(12) | Per policy (default O) [E][R] | V-UAN | 100123456789 | |
| pf_member_id | VARCHAR(22) | Per policy (default O) [E][R] | V-PFMID; first 15 chars should match an ACTIVE PF_ESTABLISHMENT registration of the organization (warning W-PF-EST-MISMATCH) | GJAHD00123450000012345 | |
| previous_pf_member_id | VARCHAR(22) | O [E][R] | V-PFMID; multiple allowed | MHBAN00987650000054321 | Reference for prior employers |
| esic_ip_number | VARCHAR(10) | Per policy (default O) [E][R] | V-ESIC-IP | 3112345678 | |
| passport_number | VARCHAR(20) | Per policy (default O) [E][R] | V-PASSPORT (India pattern when issuing_country = IN) | K1234567 | |
| passport_issue_date | DATE | C | Required when passport_number provided | 2018-03-10 | |
| passport_expiry_date | DATE | C | Required when passport_number provided; > passport_issue_date | 2028-03-09 | Expired passport → warning W-PASSPORT-EXPIRED |
| passport_issue_place | VARCHAR(100) | O | ≤ 100 | Ahmedabad | |
| voter_id | VARCHAR(10) | Per policy (default NOT_COLLECTED) [E][R] | V-EPIC (warning severity) | ABC1234567 | |
| driving_license_number | VARCHAR(20) | Per policy (default NOT_COLLECTED) [E][R] | V-DL-IN (warning severity) | GJ0120110012345 | |
| driving_license_expiry_date | DATE | O | > issue date if provided | 2031-07-14 | |

### 11.3 Identifier Requirement Policies (`identifier_requirement_policies`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | FK | | |
| identifier_type | ENUM | M | As §11.1; unique per organization | PAN | |
| requirement | ENUM | M | NOT_COLLECTED, OPTIONAL, REQUIRED_FOR_ACTIVATION | OPTIONAL | NOT_COLLECTED → API rejects values (422 FIELD_NOT_COLLECTED) |
| purpose_statement | VARCHAR(500) | C | Required (≥ 30 chars) when requirement ≠ NOT_COLLECTED for AADHAAR, VOTER_ID, DRIVING_LICENSE | Required for UAN KYC linkage | Data minimization record |
| standard columns | §1.5 | SYS | | | |

Defaults seeded: PAN OPTIONAL; AADHAAR NOT_COLLECTED; UAN OPTIONAL; PF_MEMBER_ID OPTIONAL; PREVIOUS_PF_MEMBER_ID OPTIONAL; ESIC_IP OPTIONAL; PASSPORT OPTIONAL; VOTER_ID NOT_COLLECTED; DRIVING_LICENSE NOT_COLLECTED. The OA decides requirements; the system does not infer statutory applicability.

### 11.4 Statutory Reference Profile (`employees` columns)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| international_worker_status | BOOLEAN | M | Default false; if true and nationality = IN → warning W-IW-NATIONALITY; if true, PASSPORT becomes M-A | false | Reference only |
| professional_tax_state | CHAR(2) | O | GST state code; default DER = branch state_code at creation (editable) | 24 | No PT computation |
| labour_welfare_fund_state | CHAR(2) | O | GST state code; default DER = branch state_code at creation (editable) | 24 | No LWF computation |

### 11.5 Duplicate Detection Rules
"Overlapping employment" means intervals `[joining_date, employment_end_date + 1)` overlap (null end = open), excluding archived drafts.

| Identifier | Same org, overlapping employment | Same org, non-overlapping (rehire) | Other org, same group | Other org, no common group |
|---|---|---|---|---|
| PAN, AADHAAR, UAN, ESIC_IP | Block `409 DUPLICATE_IDENTIFIER` (override with identity.duplicate_override + reason) | Warning W-DUP-REHIRE; UI offers linking as REHIRE | Warning W-DUP-GROUP (shown only to users with access to both organizations; others see no warning) | Not checked (different blind-index key) |
| PF_MEMBER_ID | Block (any record in org, including separated) | Block | Not checked | Not checked |
| PASSPORT | Warning | Warning | Warning (as above) | Not checked |
| VOTER_ID, DRIVING_LICENSE | Warning | Warning | Not checked | Not checked |
| PREVIOUS_PF_MEMBER_ID | Not checked | Not checked | Not checked | Not checked |

Duplicate checks use the blind index only; plaintext is never compared or decrypted for this purpose. Warning responses never reveal the matched person's identity to users lacking access; they return only the matched employee_code when the caller can access it.

### 11.6 Masking and Reveal

| Type | Masked display |
|---|---|
| PAN | `******234F` |
| AADHAAR | `XXXX XXXX 0124` |
| UAN | `********6789` |
| PF_MEMBER_ID | `******************2345` |
| ESIC_IP | `******5678` |
| PASSPORT | `****4567` |
| VOTER_ID / DL | Last 4 visible |

- Masked values are computed from `value_last_four`; no decryption occurs for masked display.
- Full value requires identity.reveal via `POST …/identifiers/{type}/reveal` with a reason (≥ 10 chars). The response has `Cache-Control: no-store`, and the event is audited (`IDENTIFIER_REVEALED`) without the value.
- Full Aadhaar MUST NOT appear in list views, detail views, exports, logs, audit records or error messages.
- Aadhaar MUST NOT be used as a key, search default, or employee code component.

---

## 12. Education and Previous Employment

### 12.1 Education Records (`employee_education`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id, employee_id | UUID | M | Composite FK employees | | |
| qualification_level | LOOKUP(QUALIFICATION_LEVEL) | M | Active value | GRADUATE | |
| degree_name | VARCHAR(150) | C | Required unless level ∈ {SECONDARY_10, HIGHER_SECONDARY_12} | B.E. | |
| specialization | VARCHAR(150) | O | ≤ 150 | Computer Engineering | |
| institution_name | VARCHAR(200) | M | 2–200 | L.D. College of Engineering | |
| board_or_university | VARCHAR(200) | O | ≤ 200 | Gujarat Technological University | |
| study_mode | LOOKUP(STUDY_MODE) | O | | FULL_TIME | |
| is_ongoing | BOOLEAN | M | Default false | false | |
| start_year | SMALLINT | O | 1940 ≤ year ≤ current year; ≤ passing_year | 2010 | |
| passing_year | SMALLINT | C | Required when is_ongoing = false: 1940 ≤ year ≤ current year. When is_ongoing = true: null or expected year ≥ current year (≤ current year + 10). passing_year < birth year + 10 → warning | 2014 | |
| result_type | ENUM | O | PERCENTAGE, CGPA, GRADE, OTHER | CGPA | |
| result_value | VARCHAR(20) | C | Required if result_type set. PERCENTAGE: decimal 0–100 (2 dp). CGPA: decimal > 0 and ≤ maximum_result_value. GRADE/OTHER: 1–20 chars text | 8.45 | |
| maximum_result_value | DECIMAL(5,2) | C | Required when CGPA (> 0, ≤ 100, e.g., 4, 5, 10); must be null or 100 for PERCENTAGE; null for GRADE/OTHER | 10.00 | No universal scale assumed |
| certificate_document_id | UUID | O | Composite FK employee_documents (same employee) | | Via document module |
| remarks | VARCHAR(500) | O | | | |
| standard columns | §1.5 | SYS | | | |

No employee is required to provide any particular education level. Duplicate rows (same level, institution and passing year) → warning W-EDU-DUP.

### 12.2 Previous Employment Records (`employee_previous_employment`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id, employee_id | UUID | M | Composite FK employees | | |
| employer_name | VARCHAR(200) | M | 2–200 | XYZ Infotech Pvt Ltd | |
| designation | VARCHAR(150) | O | ≤ 150 | Software Engineer | Free text |
| employment_start_date | DATE | M | ≤ today; ≥ date_of_birth + 14 years (warning otherwise) | 2018-07-01 | |
| employment_end_date | DATE | M | ≥ employment_start_date; ≤ today | 2026-09-30 | Inclusive |
| last_drawn_ctc | DECIMAL(15,2) | O [R] | ≥ 0 | 1450000.00 | Stored value only; never used in calculations; salary.view to see |
| last_drawn_ctc_currency | CHAR(3) | C | Required when last_drawn_ctc set; ISO 4217; default INR | INR | |
| reason_for_leaving | VARCHAR(500) | O | | Career growth | |
| reference_name | VARCHAR(150) | O [R] | V-NAME | Suresh Mehta | bgv.view |
| reference_contact | VARCHAR(254) | O [R] | V-PHONE or V-EMAIL | +919811112222 | bgv.view |
| experience_letter_document_id | UUID | O | Composite FK employee_documents (same employee) | | |
| relieving_letter_document_id | UUID | O | Composite FK employee_documents (same employee) | | |
| payslip_document_ids | link table | O | Rows in `employee_previous_employment_documents` (previous_employment_id, document_id, document_role = PAYSLIP); same employee | | No comma-separated storage |
| background_verification_status | LOOKUP(BGV_STATUS) | O [R] | | CLEAR | |
| background_verification_date | DATE | C [R] | Required when status ∈ {CLEAR, DISCREPANCY, UNABLE_TO_VERIFY}; ≤ today | 2026-10-20 | |
| overlap_flag | BOOLEAN | DER | True if the interval overlaps another previous-employment row of the same employee or the current record's joining_date | false | Review aid |
| overlap_review_status | ENUM | SYS | NOT_APPLICABLE, PENDING_REVIEW, ACCEPTED | NOT_APPLICABLE | Overlaps are flagged, not rejected |
| overlap_reviewed_by, overlap_reviewed_at | UUID, TIMESTAMP | SYS | | | |
| remarks | VARCHAR(500) | O | | | |
| standard columns | §1.5 | SYS | | | |

---

## 13. Family Members and Nominees

### 13.1 Family Members (`employee_family_members`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id, employee_id | UUID | M | Composite FK employees | | |
| full_name | VARCHAR(150) | M | V-NAME | Anita Patel | |
| relationship | LOOKUP(RELATIONSHIP) | M | Active value | SPOUSE | No restriction on count of any relationship (no assumptions about family structure) |
| date_of_birth | DATE | O | ≤ today; ≥ 1900-01-01 | 1994-01-20 | |
| occupation | VARCHAR(100) | O | | Teacher | |
| is_dependent | BOOLEAN | M | Default false | true | |
| insurance_coverage_reference | VARCHAR(100) | O | ≤ 100 | GMC-2026-000123-02 | Text reference only; no insurance module |
| remarks | VARCHAR(500) | O | | | |
| standard columns | §1.5 | SYS | | | |

Access: employee.view_personal. Family data is personal data of third parties (§25.8).

### 13.2 Nominees (`employee_nominees`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id, employee_id | UUID | M | Composite FK employees | | |
| nomination_type | ENUM | M | PF, GRATUITY, INSURANCE, OTHER | PF | Shares never mixed across types |
| nomination_label | VARCHAR(100) | C | Required when nomination_type = OTHER or INSURANCE (policy name) | Group Term Life 2026 | Distinguishes multiple "OTHER"/insurance schemes |
| family_member_id | UUID | O | Composite FK employee_family_members (same employee) | | Avoids duplicate entry |
| nominee_name | VARCHAR(150) | M | V-NAME | Anita Patel | |
| relationship | LOOKUP(RELATIONSHIP) | M | | SPOUSE | |
| date_of_birth | DATE | C | Required when nomination_type_configs.requires_dob = true; ≤ today | 1994-01-20 | |
| contact_number | VARCHAR(16) | O | V-PHONE | +919898989898 | |
| address | VARCHAR(500) | O | ≤ 500 | 22, Shanti Nagar Society, Gandhinagar 382021 | |
| share_percentage | DECIMAL(5,2) | M | > 0.00 and ≤ 100.00 | 60.00 | |
| is_minor | BOOLEAN | DER | Age < 18 on effective_from (requires DOB) | false | |
| guardian_name | VARCHAR(150) | C | Required when is_minor = true; V-NAME | | |
| guardian_relationship | VARCHAR(100) | C | Required when is_minor = true | Mother | Relationship of guardian to nominee |
| effective_from | DATE | M | ≥ joining_date (warning if earlier) ; ≤ today + 365 | 2026-11-02 | |
| effective_to | DATE | O | > effective_from; exclusive; set by system on set replacement | | |
| supporting_document_id | UUID | O | Composite FK employee_documents (same employee) | | E.g., signed nomination form |
| standard columns | §1.5 | SYS | | | |

### 13.3 Nomination Rules (`nomination_type_configs`, per organization)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| organization_id | UUID | M | Unique with nomination_type | | |
| nomination_type | ENUM | M | PF, GRATUITY, INSURANCE, OTHER | PF | |
| requires_full_allocation | BOOLEAN | M | Defaults: PF true, GRATUITY true, INSURANCE true, OTHER false | true | Active shares must total exactly 100.00 |
| requires_dob | BOOLEAN | M | Defaults: PF true, GRATUITY true, INSURANCE false, OTHER false | true | |
| max_nominees | SMALLINT | O | 1–20; null = unlimited | | |
| scheme_rule_notes | VARCHAR(1000) | O | Free text describing scheme-specific rules confirmed by legal | | Scheme-specific statutory eligibility rules are not hard-coded (§29) |

**Nomination set behaviour:**
- Nominees of one type (and label) are maintained as a **set**, replaced atomically via `PUT …/nominations/{type}` with a common `effective_from`.
- On replacement, current rows of that set get `effective_to = new effective_from`; new rows are inserted. History is retained.
- Validation on save: for the new set, if requires_full_allocation, Σ share_percentage = 100.00 exactly (error `NOMINEE_SHARE_TOTAL_INVALID` with the computed total); all shares > 0; minors have guardians; count ≤ max_nominees.
- Different nomination types are validated independently.

---

## 14. Bank Details

### 14.1 Bank Accounts (`employee_bank_accounts`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id, employee_id | UUID | M | Composite FK employees | | |
| account_holder_name | VARCHAR(150) | M | V-NAME; token mismatch with employee name → warning W-BANK-NAME | Rajesh Kumar Patel | |
| bank_name | VARCHAR(150) | M | 2–150; MAY be pre-filled from an IFSC reference dataset if one is loaded | State Bank of India | |
| branch_name | VARCHAR(150) | O | | Sector 16 Gandhinagar | |
| account_number | VARCHAR(18) | Input only [R] | V-BANKACC; accepted on create; never persisted in plaintext; never returned except via reveal | 30123456789 | |
| account_number_encrypted | BYTES | SYS [E][R] | AES-256-GCM of normalized account number | | |
| account_number_last_four | CHAR(4) | SYS | | 6789 | Masked display `XXXXXXX6789` |
| account_number_blind_index | BYTES(32) | SYS [R] | HMAC of (ifsc_code + account number) | | Duplicate detection: same account on another employee in the organization → warning W-BANK-DUP |
| key_version | SMALLINT | SYS | | | |
| ifsc_code | VARCHAR(11) | M | V-IFSC; upper-case | SBIN0001234 | |
| account_type | LOOKUP(BANK_ACCOUNT_TYPE) | M | | SAVINGS | |
| is_primary | BOOLEAN | M | Default false; see §14.2 | true | |
| verification_status | ENUM | M | UNVERIFIED (default), PENDING, VERIFIED, FAILED | UNVERIFIED | |
| verification_date | DATE | C | Required when VERIFIED or FAILED; ≤ today | 2026-11-05 | |
| verified_by | UUID | SYS | FK users; bank.verify | | |
| verification_remarks | VARCHAR(500) | C | Required when FAILED | | |
| bank_proof_document_id | UUID | C | Required to set VERIFIED; composite FK employee_documents (same employee, category BANK_PROOF) | | |
| effective_from | DATE | M | Default today | 2026-11-02 | |
| effective_to | DATE | O | > effective_from; exclusive | | Closing an account |
| created_at, created_by | TIMESTAMP, UUID | SYS | | | Plus other standard columns |

### 14.2 Bank Rules
- **Primary account policy (decision):** at most one primary account per employee at any date (overlapping primary intervals prohibited by DB exclusion constraint on PostgreSQL; locked check on MySQL). At least one primary account is required for activation only when `require_primary_bank_account = true`.
- Setting a new primary (`POST …/bank-accounts/{id}/set-primary`) atomically sets the previous primary's `is_primary = false` (history in audit).
- Changing account number or IFSC is not an edit: close the old row (`effective_to`) and create a new one; verification resets to UNVERIFIED.
- Online banking passwords, PINs, OTPs, card numbers and CVV MUST NOT be stored; there are no fields for them and free-text fields are not intended for them.
- Bank details are reference data; the system MUST NOT initiate payments or generate payment files.
- Masked views only by default; full number via bank.reveal (audited, reason required).

---

## 15. Employee Document Management

### 15.1 Design
Each employee has a document repository. Categories are configuration (metadata), not separate storage systems. File bytes live in private object storage; metadata lives in the database.

### 15.2 Document Categories (`document_categories`, per organization; seeded)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | FK | | |
| category_code | VARCHAR(40) | M | `^[A-Z0-9_]{2,40}$`; unique per organization; immutable | PAN_PROOF | |
| category_name | VARCHAR(100) | M | | PAN proof | |
| description | VARCHAR(500) | O | | | |
| is_restricted | BOOLEAN | M | | true | Requires document.download_restricted |
| requires_verification | BOOLEAN | M | | true | New uploads start Pending; otherwise Not Required |
| supports_expiry | BOOLEAN | M | | false | Enables expiry_date |
| allows_multiple_current | BOOLEAN | M | | false | If false, one current document per (employee, category, document_type) |
| max_file_size_bytes | INTEGER | M | 1 MB–25 MB; default 10 MB | 10485760 | |
| allowed MIME types | child table `document_category_mime_types` | M | ≥ 1 row; values from the allowlist in §15.5 | application/pdf | No comma-separated list |
| is_mandatory_for_activation | BOOLEAN | M | Default false | false | |
| retention_months_after_separation | SMALLINT | O | 0–600; null = organization default | 96 | §25.6 |
| status | ENUM | M | ACTIVE, INACTIVE | ACTIVE | |
| is_system_default | BOOLEAN | SYS | | true | |

Seeded categories:

| category_code | Restricted | Verification | Expiry | Multiple current |
|---|---|---|---|---|
| PHOTOGRAPH | No | No | No | No (JPEG/PNG only, ≤ 2 MB) |
| PAN_PROOF | Yes | Yes | No | No |
| AADHAAR_PROOF | Yes | Yes | No | No (only usable if AADHAAR not NOT_COLLECTED; redaction of first 8 digits recommended) |
| ADDRESS_PROOF | Yes | Yes | Yes | Yes |
| PASSPORT | Yes | Yes | Yes | No |
| VOTER_ID | Yes | Yes | No | No |
| DRIVING_LICENCE | Yes | Yes | Yes | No |
| RESUME | No | No | No | Yes |
| OFFER_LETTER | No | No | No | Yes |
| APPOINTMENT_LETTER | No | No | No | Yes |
| EMPLOYMENT_AGREEMENT | No | No | Yes | Yes |
| NDA | No | No | Yes | Yes |
| CODE_OF_CONDUCT_ACK | No | No | No | Yes |
| POSH_POLICY_ACK | No | No | No | Yes |
| EDUCATION_CERTIFICATE | No | Yes | No | Yes |
| EXPERIENCE_LETTER | No | Yes | No | Yes |
| RELIEVING_LETTER | No | Yes | No | Yes |
| PREVIOUS_PAYSLIP | Yes | No | No | Yes |
| BANK_PROOF | Yes | Yes | No | Yes |
| MEDICAL_FITNESS | Yes | No | Yes | Yes |
| BGV_DOCUMENT | Yes | No | No | Yes |
| NOMINATION_FORM | Yes | No | No | Yes |
| SALARY_REVISION_LETTER | Yes | No | No | Yes |
| SALARY_STRUCTURE_DOCUMENT | Yes | No | No | Yes |
| OTHER | No | No | Yes | Yes |

SALARY_* categories additionally require salary.view to list or download. Organizations may add, rename (name only) and deactivate categories.

### 15.3 Document Fields (`employee_documents`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | Composite FK with employee | | |
| employee_id | UUID | M | Composite FK employees | | |
| document_category_id | UUID | M | Composite FK document_categories (same org, ACTIVE) | | |
| document_type | LOOKUP(DOCUMENT_TYPE) | M | Default = category_code | DEGREE_CERTIFICATE | Sub-type |
| document_title | VARCHAR(200) | M | 2–200 | B.E. Degree Certificate | |
| document_description | VARCHAR(1000) | O | | | |
| original_file_name | VARCHAR(255) | M | Sanitized: path separators and control characters removed; ≤ 255 | degree_certificate.pdf | Display only |
| storage_key | VARCHAR(500) | SYS [R] | `org/{org_uuid}/emp/{emp_uuid}/{doc_uuid}`; contains no PII; never returned by API | | |
| mime_type | VARCHAR(100) | SYS | Detected by content sniffing; must match category allowlist and the declared type | application/pdf | |
| file_extension | VARCHAR(10) | SYS | Lower-case; must be consistent with mime_type | pdf | |
| file_size_bytes | BIGINT | SYS | 1 ≤ size ≤ category max | 482113 | |
| checksum | CHAR(64) | SYS | SHA-256 hex computed server-side | e3b0c442… | Integrity and duplicate detection (warning W-DOC-DUP) |
| document_number | VARCHAR(50) | O [E][R] | Encrypted; masked display last 4 | | Certificate/ID number |
| issue_date | DATE | O | ≤ today | 2014-06-30 | |
| expiry_date | DATE | C | Allowed only if category supports_expiry; > issue_date | 2028-03-09 | |
| issued_by | VARCHAR(200) | O | | Gujarat Technological University | |
| verification_status | ENUM | SYS via verify | PENDING, VERIFIED, REJECTED, NOT_REQUIRED; initial = PENDING if category requires_verification else NOT_REQUIRED | PENDING | |
| verified_by | UUID | SYS | FK users | | |
| verified_at | TIMESTAMP | SYS | | | |
| rejection_reason | VARCHAR(500) | C | Required (10–500 chars) when REJECTED | Image illegible | |
| upload_status | ENUM | SYS | PENDING_UPLOAD, SCANNING, AVAILABLE, QUARANTINED, FAILED | AVAILABLE | Only AVAILABLE is downloadable |
| version_number | INTEGER | SYS | 1 for first upload; +1 per replacement within the chain | 2 | |
| supersedes_document_id | UUID | SYS | Composite FK employee_documents (same employee) | | Previous version |
| is_current | BOOLEAN | SYS | Exactly one current version per chain | true | |
| uploaded_by | UUID | SYS | FK users | | |
| uploaded_at | TIMESTAMP | SYS | | | |
| updated_at | TIMESTAMP | SYS | | | |
| archived_at | TIMESTAMP | SYS | §1.5 (with archived_by, archive_reason) | | |

**Design decision:** versions are rows in `employee_documents` (no separate versions table). Each replacement is a new row linked by `supersedes_document_id`; this keeps every version independently addressable, verifiable and auditable with one access-control path.

### 15.4 Upload Flow
1. `POST /api/v1/orgs/{orgId}/employees/{employeeId}/documents` with metadata, declared MIME type and size → server validates permission, category, declared type/size; creates row (`PENDING_UPLOAD`); returns a pre-signed upload URL (PUT, expires in 15 minutes, size-limited, content-type bound).
2. Client uploads bytes directly to the quarantine bucket.
3. `POST …/documents/{docId}/complete` → server verifies object exists, size ≤ limit, magic-byte MIME detection matches the allowlist and declared type, computes SHA-256, runs malware scanning; status becomes AVAILABLE (object moved to the private bucket) or QUARANTINED/FAILED (object deleted within 24 h; row retained with reason).
4. Rows in PENDING_UPLOAD for more than 24 hours are marked FAILED and their objects purged.

### 15.5 File Validation
- Default allowlist: `application/pdf` (.pdf), `image/jpeg` (.jpg, .jpeg), `image/png` (.png). Optional per category: `application/vnd.openxmlformats-officedocument.wordprocessingml.document` (.docx).
- Always rejected (`415 FILE_TYPE_NOT_ALLOWED`): executables and installers (.exe, .msi, .dll, .bat, .cmd, .sh, .ps1, .jar, .apk), scripts and markup (.js, .html, .htm, .svg, .xml), archives (.zip, .rar, .7z, .tar, .gz), macro-enabled Office files (.docm, .xlsm, .pptm), and any file whose sniffed type differs from its extension.
- PDFs containing embedded JavaScript or embedded file attachments are QUARANTINED if the scanner detects them [ASSUMPTION: scanner capability].
- Images are re-encoded server-side to strip EXIF metadata (location data) [ASSUMPTION].

### 15.6 Access and Download
- Every document operation verifies: caller has access to the path organization; the document's organization_id and employee_id match the path; the caller has the operation permission; restricted categories require document.download_restricted; SALARY_* categories require salary.view.
- A document ID from another organization returns `404 RESOURCE_NOT_FOUND` (no existence disclosure).
- Download: `POST …/documents/{docId}/download-url` returns a pre-signed GET URL valid for 5 minutes, with `Content-Disposition: attachment` and `Cache-Control: no-store`. Storage paths and permanent public URLs are never exposed.
- Downloads are audited (always for restricted categories; others when `audit_all_document_downloads = true`).

### 15.7 Versioning, Verification, Archival
- **Replace:** `POST …/documents/{docId}/versions` creates version n+1 (same chain), sets the old row `is_current = false`; the old version is retained per retention policy and visible in the version history to authorized users.
- **Verify:** `POST …/documents/{docId}/verify` with `decision ∈ {VERIFIED, REJECTED}`; REJECTED requires a reason. When `allow_document_self_verification = false`, verifier ≠ uploader. New versions reset to PENDING if the category requires verification.
- **Archive:** soft-archive with reason (document.archive). Archived documents are hidden from default lists and retrievable with `include_archived=true` by HRA.
- **Hard deletion** occurs only through the retention purge job or an approved erasure request (§25.6); a tombstone audit record is kept (metadata without file bytes).

### 15.8 Expiry Indicators
`expiry_state` (DER): NO_EXPIRY (expiry_date null), VALID, EXPIRING_SOON (0 ≤ expiry_date − today ≤ document_expiry_warning_days), EXPIRED (expiry_date < today). Shown as a badge, filterable in document lists and an organization-level "Expiring documents" list. No emails, reminders or workflows are generated.

---

## 16. Dynamic Salary Structure Templates

Salary data is stored for record-keeping and historical reference. The system MUST NOT calculate payroll, tax, net pay, totals or statutory contributions (§4.3). The only calculation is a PERCENTAGE component's own amount as a percentage of Basic (§16.3).

[CHG 2026-11-02] **Structure in one paragraph.** A *salary component* is one step of a salary structure (Basic, HRA, PF …). Each has an internal `component_name`, an employee-facing `display_name`, a `component_type` (EARNING = receivable, DEDUCTION = deductible) and a calculation `value_type` (FIXED_AMOUNT, or PERCENTAGE = % of Basic). **Basic** is a system component: seeded with every organization, always present, never deletable. A *template* is a versioned selection of components with defaults; Basic is added automatically and the user supplies only its amount. A component such as HRA 15% can be created from the template screen and added to the template. An employee's salary can come from a template or from catalogue components picked directly; either way the values are **copied** into the assignment (§17.2), so later changes to a component or template never alter an employee's stored salary.

### 16.1 Model
`salary_components` (organization catalog) → `salary_templates` (header) → `salary_template_versions` (versioned, immutable once published) → `salary_template_components` (mapping rows per version) → employee assignments snapshot values (§17).

### 16.2 Typed Value Group (reused)
Wherever a field is described as a "typed value" (`default_value`, `value`), the following columns exist:

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| value_amount | DECIMAL(15,2) | C | Used when value_type = FIXED_AMOUNT; ≥ 0 | 600000.00 | |
| value_percentage | DECIMAL(7,4) | C | Used when value_type = PERCENTAGE; 0–100 | 40.0000 | [CHG 2026-11-02] Percentage **of Basic** |
| value_text | VARCHAR(500) | C | Used when value_type = TEXT or OTHER_CONFIGURED; OTHER_CONFIGURED validated by component validation_regex | As per policy | |
| value_boolean | BOOLEAN | C | Used when value_type = BOOLEAN | true | |

CHECK: at most the single column matching value_type is non-null; all others null.

### 16.3 Salary Component Definitions (`salary_components`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | FK | | |
| component_code | VARCHAR(30) | M | V-CODE; unique per organization; immutable; `BASIC` is reserved for the system Basic component | HRA | |
| component_name | VARCHAR(100) | M | 2–100 | House Rent Allowance (Metro) | [CHG 2026-11-02] Internal name that tells components apart. Renames do not affect snapshots |
| display_name | VARCHAR(100) | M | 2–100; defaults to component_name when omitted on create | HRA | [CHG 2026-11-02] What the employee sees. Renames do not affect snapshots |
| component_type | ENUM | M | EARNING (receivable), DEDUCTION (deductible); default EARNING; immutable | EARNING | [CHG 2026-11-02] Grouping label; nothing totals or nets the groups |
| is_system | BOOLEAN | SYS | True only for Basic | false | [CHG 2026-11-02] A system component cannot be deactivated, retyped or have its calculation changed; only names, description and display order are editable |
| description | VARCHAR(500) | O | | | |
| component_category | ENUM | M | EARNINGS, ALLOWANCE, EMPLOYER_BENEFIT, EMPLOYEE_DEDUCTION_REFERENCE, OTHER | EARNINGS | |
| value_type | ENUM | M | FIXED_AMOUNT, PERCENTAGE, TEXT, BOOLEAN, OTHER_CONFIGURED; immutable once used in any version or assignment | FIXED_AMOUNT | |
| amount_period | ENUM | C | Required when value_type = FIXED_AMOUNT: ANNUAL, MONTHLY, ONE_TIME; else NOT_APPLICABLE | ANNUAL | Prevents monthly/annual ambiguity |
| percentage_reference_label | VARCHAR(100) | O | Allowed only for PERCENTAGE; defaults to `% of Basic` | % of Basic | Display label; the base is always Basic |
| validation_regex | VARCHAR(200) | C | Required for OTHER_CONFIGURED; must compile; max 200 chars | `^[A-Z]{2}$` | |
| default_value | typed value (§16.2) | O | Must match value_type | 0.00 | |
| currency | CHAR(3) | C | Required for FIXED_AMOUNT; ISO 4217; default organization currency | INR | |
| is_required | BOOLEAN | M | Default false | true | Default for new template mappings |
| is_editable_per_employee | BOOLEAN | M | Default true | true | If false, no employee override in any template |
| display_order | INTEGER | M | ≥ 0 | 20 | |
| status | ENUM | M | ACTIVE, INACTIVE | ACTIVE | Inactive components cannot be added to new versions/assignments |
| created_at, updated_at | | SYS | Plus other standard columns | | |

[CHG 2026-11-02] **Basic.** Organization creation (API, bootstrap and demo seed) and the Alembic migration seed one component per organization: code `BASIC`, name/display name "Basic", EARNING, FIXED_AMOUNT, MONTHLY, organization currency, `is_required = true`, `is_system = true`. It is the base of every PERCENTAGE component. Attempts to deactivate it, create another `BASIC`, or change anything beyond its names/description/order are rejected (422 / 409).

Other seed suggestions (not hard-coded; OA may delete before use): ANNUAL_CTC, DA, HRA, SPECIAL_ALLOWANCE, CONVEYANCE, LTA, MEDICAL_ALLOWANCE, OTHER_ALLOWANCE, VARIABLE_PAY_REF, EMPLOYER_PF_REF, EMPLOYER_ESIC_REF, GRATUITY_REF.

### 16.4 Salary Templates (`salary_templates`, header)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | FK | | A template belongs to exactly one organization |
| template_code | VARCHAR(30) | M | V-CODE; unique per organization; immutable | STD-STAFF | |
| template_name | VARCHAR(150) | M | 2–150 | Standard Staff Structure | |
| description | VARCHAR(1000) | O | | | |
| is_archived | BOOLEAN | SYS | Set via archive; archives all versions | false | |
| created_by, created_at, updated_by, updated_at | | SYS | | | |

### 16.5 Template Versions (`salary_template_versions`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | Composite FK with template | | |
| template_id | UUID | M | FK salary_templates | | |
| version_number | INTEGER | SYS | 1, 2, 3… per template | 2 | |
| currency | CHAR(3) | M | ISO 4217; all FIXED_AMOUNT mappings must use it | INR | |
| status | ENUM | M | DRAFT, ACTIVE, ARCHIVED | ACTIVE | |
| effective_from | DATE | M | Required to publish | 2027-04-01 | |
| effective_to | DATE | O | > effective_from; exclusive; set automatically when a later version is published | | |
| change_note | VARCHAR(1000) | C | Required when publishing version ≥ 2 | Added medical allowance | |
| published_by, published_at | UUID, TIMESTAMP | SYS | | | |
| created_by, created_at, updated_by, updated_at | | SYS | | | |

### 16.6 Template Component Mapping (`salary_template_components`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| template_id | UUID | M | FK salary_templates | | Spec field; denormalized for querying |
| template_version_id | UUID | M | FK salary_template_versions (same template) | | |
| component_id | UUID | M | Composite FK salary_components (same organization, ACTIVE when added); unique per version | | |
| default_value | typed value | O | Matches component value_type; FIXED_AMOUNT currency = version currency | 600000.00 | |
| is_required | BOOLEAN | M | Default = component.is_required | true | Assignment must have a value |
| is_overridable | BOOLEAN | M | Default true; forced false when component.is_editable_per_employee = false | true | |
| display_order | INTEGER | M | ≥ 0; ties broken by component_code | 10 | |
| effective_from | DATE | O | Within the version interval | | Mapping applies only to assignments whose effective_from is in [mapping from, mapping to) |
| effective_to | DATE | O | > effective_from; exclusive; ≤ version effective_to | | |

### 16.7 Template Rules
- [CHG 2026-11-02] Every version contains Basic. It is added automatically when a template is created, when mappings are replaced without it, and when a new version is copied from one that lacks it. Its `default_value_amount` may be left empty and supplied per employee. A PERCENTAGE mapping's default is a percentage of Basic.
- [CHG 2026-11-02] A new component (for example HRA, PERCENTAGE 15) is created with `POST …/salary-components` from the template builder and then included in the version's mappings; there is no separate inline-create endpoint.
- DRAFT versions are fully editable. ACTIVE versions are immutable except `effective_to` (set by publishing a later version) and archival.
- "Editing" an ACTIVE template creates a new DRAFT version copying all mappings (`POST …/salary-templates/{id}/versions`).
- Publishing version n with effective_from F: F must be ≥ previous version's effective_from + 1 day; the previous ACTIVE version's effective_to becomes F. Versions of one template never overlap (DB exclusion constraint).
- A template change never alters existing employee assignments; it affects only new assignments and explicitly initiated revisions.
- [CHG 2026-11-01] Every version of a template can be listed and read back, including DRAFT and ARCHIVED ones, so a client never has to hold a draft only in memory (`GET …/salary-templates/{id}/versions`, `GET …/versions/{vid}`).
- [CHG 2026-11-01] Publish may carry the builder's unsaved state (`currency`, `effective_from`, `change_note`, `mappings`). It is applied to the DRAFT and published in one transaction; if publishing fails (for example the version overlaps an ACTIVE one), the edits are rolled back with it. An omitted field keeps its stored value; `mappings: []` clears the mappings and an omitted `mappings` keeps them.
- [CHG 2026-11-01] Currency codes on components, template versions and templates must be active ISO 4217 codes. A well-formed but unknown code (for example `XXX`) returns `422`. Other modules' currency fields are unchanged.
- [CHG 2026-11-01] `GET …/salary-components/{id}/usage` returns `template_version_count` and `assignment_count` so an administrator can see the impact before deactivating a component. Deactivation itself is unchanged.
- Archived templates (or ARCHIVED versions) cannot be newly assigned (`422 TEMPLATE_ARCHIVED`) but remain readable in history.
- Duplicate component mappings within one version are prohibited (unique constraint).
- No component list is hard-coded; required components are defined per template.

**Illustrative template (values are examples, not computed):**

| Component | value_type | amount_period | Default | Required | Overridable |
|---|---|---|---|---|---|
| ANNUAL_CTC | FIXED_AMOUNT | ANNUAL | — | Yes | Yes |
| BASIC | FIXED_AMOUNT | ANNUAL | — | Yes | Yes |
| HRA | FIXED_AMOUNT | ANNUAL | — | No | Yes |
| SPECIAL_ALLOWANCE | FIXED_AMOUNT | ANNUAL | — | No | Yes |
| EMPLOYER_PF_REF | FIXED_AMOUNT | ANNUAL | 21600.00 | No | Yes |
| VARIABLE_PAY_ELIGIBLE | BOOLEAN | — | false | No | Yes |

---

## 17. Employee Salary Assignments, Overrides and Revisions

### 17.1 Salary Assignment Fields (`employee_salary_assignments`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | Composite FK with employee | | |
| employee_id | UUID | M | Composite FK employees; employee status ∉ {DRAFT when confirming? see note} | | Drafts allowed for DRAFT employees; confirmation requires employee not archived |
| template_id | UUID | O | Composite FK salary_templates (not archived) | | Null = fully custom (requires salary.custom_component and allow_custom_salary_components) |
| template_version_id | UUID | C | Required when template_id set; version ACTIVE and effective on assignment effective_from | | |
| template_version_number | INTEGER | C [H] | Snapshot | 2 | |
| template_code_snapshot | VARCHAR(30) | C [H] | Snapshot | STD-STAFF | |
| template_name_snapshot | VARCHAR(150) | C [H] | Snapshot | Standard Staff Structure | |
| currency | CHAR(3) | M [H] | = template version currency, or chosen for custom | INR | |
| assignment_status | ENUM | SYS | DRAFT, SCHEDULED, ACTIVE, EXPIRED, CANCELLED (§18.2) | SCHEDULED | |
| approval_state | ENUM | SYS | NOT_REQUIRED, PENDING, APPROVED, REJECTED | NOT_REQUIRED | §17.4 |
| effective_from | DATE | M | ≥ employee joining_date (or employment_start_date); ≤ employment_end_date if set | 2027-04-01 | Inclusive |
| effective_to | DATE | O | > effective_from; exclusive; ≤ employment_end_date + 1 | | System-managed boundary (§18) or user-set |
| effective_to_source | ENUM | SYS | NONE, USER, SUCCESSOR, EMPLOYMENT_END | SUCCESSOR | Drives boundary restoration |
| revision_reason | LOOKUP(SALARY_REVISION_REASON) | M | | ANNUAL_REVISION | |
| revision_remarks | VARCHAR(1000) | O | | | |
| revision_letter_document_id | UUID | O | Composite FK employee_documents (same employee, category SALARY_REVISION_LETTER) | | |
| is_backdated | BOOLEAN | SYS | True if effective_from < today at confirmation | false | |
| backdate_reason | VARCHAR(1000) | C | Required (≥ 10 chars) when is_backdated | | |
| predecessor_assignment_id | UUID | SYS | Assignment truncated by this one | | |
| predecessor_original_effective_to | DATE | SYS [H] | Predecessor's effective_to before truncation | | Audit/explanation |
| replaces_assignment_id | UUID | SYS | Scheduled assignment replaced via edit (§17.3) | | |
| superseded_by_assignment_id | UUID | SYS | Set on corrected assignment | | |
| cancellation_type | ENUM | SYS | USER_CANCEL, REPLACED, CORRECTION | | |
| cancelled_by, cancelled_at | UUID, TIMESTAMP | SYS | | | |
| cancellation_reason | VARCHAR(1000) | C | Required on cancel | | |
| submitted_by, submitted_at | UUID, TIMESTAMP | SYS | When approval required | | |
| approved_by | UUID | SYS | ≠ created_by and ≠ submitted_by | | |
| approved_at | TIMESTAMP | SYS | | | |
| rejection_reason | VARCHAR(1000) | C | Required on reject | | |
| confirmed_by, confirmed_at | UUID, TIMESTAMP | SYS | Moment it entered the timeline | | |
| created_by, created_at, updated_by, updated_at | | SYS | | | |

### 17.2 Assignment Component Fields (`employee_salary_components`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| salary_assignment_id | UUID | M | FK employee_salary_assignments | | |
| component_id | UUID | O | Composite FK salary_components (same org); null for custom components | | |
| component_code_snapshot | VARCHAR(30) | M [H] | For custom: V-CODE, unique within assignment, must not equal an existing org component code | BASIC | |
| component_name_snapshot | VARCHAR(100) | M [H] | | Basic Salary | |
| display_name_snapshot | VARCHAR(100) | M [H] | [CHG 2026-11-02] Custom components default to their name | Basic | The employee-facing label as it was at assignment time |
| component_type_snapshot | ENUM | M [H] | EARNING, DEDUCTION [CHG 2026-11-02] | EARNING | |
| component_category_snapshot | ENUM | M [H] | As §16.3 | EARNINGS | |
| value_type | ENUM | M [H] | As §16.3 | FIXED_AMOUNT | |
| amount_period_snapshot | ENUM | C [H] | Required for FIXED_AMOUNT | ANNUAL | |
| value | typed value | C | Required if mapping is_required; type must match value_type | 720000.00 | |
| computed_amount | DECIMAL(15,2) | C [H] | [CHG 2026-11-02] Set only for PERCENTAGE: `value_percentage x Basic value`, rounded half-up to 2 dp, frozen at save time; null otherwise | 6000.00 | Basic's own `value_amount` is required in every assignment |
| template_default_snapshot | typed value | O [H] | Template default at assignment time | 600000.00 | For override display |
| currency | CHAR(3) | C | Required for FIXED_AMOUNT; = assignment currency | INR | |
| is_overridden | BOOLEAN | SYS | True when value ≠ template default (or no template mapping) | true | |
| override_reason | VARCHAR(500) | C | Required (≥ 5 chars) when is_overridden on a component with a template default [ASSUMPTION] | Retention adjustment | |
| is_custom | BOOLEAN | SYS | component_id null | false | |
| display_order_snapshot | INTEGER | M [H] | | 20 | |
| created_at, updated_at | | SYS | | | |

Unique: `(salary_assignment_id, component_code_snapshot)`.

### 17.3 Assignment Operations

| Operation | Rules |
|---|---|
| Create from template | Select template; system resolves the version effective on effective_from (error `TEMPLATE_VERSION_NOT_EFFECTIVE` if none) and pre-fills mappings applicable on that date with defaults. |
| Override | Allowed only if mapping.is_overridable and component.is_editable_per_employee; otherwise value must equal the template default (error `COMPONENT_NOT_OVERRIDABLE`). |
| Add custom component | Requires salary.custom_component and organization allow_custom_salary_components = true. [CHG 2026-11-02] Applies only to ad-hoc components with no catalogue entry; picking catalogue components directly (no template) needs only `salary.revision_create`. Every assignment must include Basic with an amount (`BASIC_REQUIRED`); `BASIC` cannot be used as a custom code. |
| Remove template component | Allowed only for mappings with is_required = false (component omitted from assignment). |
| Edit DRAFT | Fully editable by creator or HRA. |
| Confirm | Places the assignment in the timeline per §18.3 (requires salary.revision_confirm; and APPROVED if approval required). |
| Edit SCHEDULED | Not edited in place. "Edit" performs, in one transaction, cancel (cancellation_type = REPLACED) + confirm of a new assignment with `replaces_assignment_id`. |
| Correct ACTIVE/EXPIRED | §18.5; requires salary.revision_correct. |
| Cancel SCHEDULED | §18.4. ACTIVE/EXPIRED cannot be cancelled. |
| Delete | Only DRAFT assignments that were never submitted may be hard-deleted (audited). |

### 17.4 Optional Approval (maker-checker)
When `salary_revision_approval_required = true`:
1. Creator saves DRAFT → `POST …/submit` sets approval_state PENDING.
2. Approver (salary.revision_approve, not creator/submitter) approves or rejects (reason required).
3. APPROVED drafts are confirmed by `POST …/confirm` (may be done by approver in the same call with `confirm=true`).
4. Timeline validation (§18.3) runs at confirmation; if the timeline changed since approval and the assignment now overlaps, confirmation fails and the draft returns to PENDING.

### 17.5 Salary History Behaviour (required)
- Every confirmed revision is preserved as an immutable record (values and snapshots never updated).
- A new revision never overwrites previous values.
- History displays chronologically by effective_from, then confirmed_at.
- Template and component snapshots ensure later template/component changes cannot alter history.
- Every lifecycle event is recorded in `salary_revision_history`:

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | Append-only |
| organization_id, employee_id | UUID | M | Composite FK | | |
| salary_assignment_id | UUID | M | FK | | |
| related_assignment_id | UUID | O | E.g., predecessor or replacement | | |
| event_type | ENUM | M | CREATED_DRAFT, UPDATED_DRAFT, SUBMITTED, APPROVED, REJECTED, CONFIRMED, BOUNDARY_ADJUSTED, ACTIVATED, EXPIRED, CANCELLED, REPLACED, CORRECTED | CONFIRMED | |
| effective_from_snapshot | DATE | M [H] | | 2027-04-01 | |
| effective_to_before | DATE | O [H] | | | Boundary events |
| effective_to_after | DATE | O [H] | | | |
| reason | VARCHAR(1000) | O | | | |
| component_changes | JSON(schema) | O [H] | Array of {component_code, change_type ∈ ADDED/REMOVED/CHANGED/UNCHANGED, previous_value, new_value, value_type} compared with the predecessor | | Diff for display; no arithmetic |
| actor_user_id | UUID | M | FK users (system user for jobs) | | |
| occurred_at | TIMESTAMP | M | | | |

### 17.6 Salary Views and Comparison
The UI and API MUST support:
- Current structure (as-of today).
- Structure as-of any date (`as_of` parameter).
- Historical structures (EXPIRED), scheduled future structures (SCHEDULED), and optionally cancelled ones (`include_cancelled=true`).
- Comparison of any two assignments of the same employee: per component code, values side by side with change_type (ADDED, REMOVED, CHANGED, UNCHANGED) and override flags. **No totals, differences or take-home values are calculated; the only computed figure is a PERCENTAGE component's own `computed_amount`** (§4.3).
- Display of template default vs employee-specific value for each component.
- Filtering history by effective_from range and status.
- Opening the revision letter document (subject to document permissions).

---

## 18. Salary Effective-Date and End-Date Rules

### 18.1 Convention
- Interval `I(a) = [effective_from, effective_to)`; `effective_to = NULL` = open-ended.
- **Confirmed** assignments (SCHEDULED, ACTIVE, EXPIRED) form the employee's timeline. DRAFT and CANCELLED do not.
- "Assignment as of date D" = the confirmed assignment with `effective_from ≤ D AND (effective_to IS NULL OR D < effective_to)`.
- Adjacent intervals are valid: A `[2026-04-01, 2027-01-01)` and B `[2027-01-01, NULL)` do not overlap.

**Invariants:**

| ID | Invariant |
|---|---|
| INV-1 | Confirmed intervals of one employee are pairwise disjoint. |
| INV-2 | effective_to > effective_from when not null. |
| INV-3 | effective_from ≥ employee joining_date. |
| INV-4 | If employment_end_date E is set: every confirmed interval satisfies effective_from ≤ E and effective_to ≤ E + 1 (not null). |
| INV-5 | Component values and snapshots of confirmed assignments are immutable. |
| INV-6 | Only effective_to, effective_to_source, assignment_status and cancellation fields of confirmed assignments can change, and only through the operations below. |

Gaps between assignments are permitted (periods without a recorded salary) and shown in the UI as gaps.

### 18.2 Status Derivation
For a confirmed assignment and today T (organization timezone): SCHEDULED if effective_from > T; ACTIVE if effective_from ≤ T and (effective_to null or T < effective_to); EXPIRED if effective_to ≤ T. DRAFT and CANCELLED are explicit.

- A status-refresh job runs per organization at 00:05 local time (idempotent), updates stored statuses and writes ACTIVATED/EXPIRED events.
- **All "current salary" reads use the date predicate in §18.1, never the stored status.** Therefore a scheduled revision becomes current at 00:00 on its effective date even if the job is delayed.

### 18.3 Operation O1: Confirm a Revision R (effective_from F, optional explicit end Te)
Executed in one database transaction:

1. Lock the employee row: `SELECT … FROM employees WHERE id = :employee_id FOR UPDATE`. This serializes all salary-timeline writes for that employee.
2. Validate: permissions; template version effective on F; component rules; F ≥ joining_date; if F < today → requires salary.revision_backdate, backdate_reason, and F ≥ today − backdated_salary_max_days (error `BACKDATE_NOT_ALLOWED`); if employment_end_date E set → F ≤ E.
3. If a confirmed assignment exists with effective_from = F → reject `409 SALARY_ASSIGNMENT_OVERLAP` (resolution: cancel it if SCHEDULED, or use correction O3).
4. P = confirmed assignment with effective_from < F < coalesce(effective_to, ∞) (the assignment containing F).
5. N = confirmed assignment with the smallest effective_from > F.
6. Determine R's end:
   - If Te supplied: require Te > F; if N exists require Te ≤ N.from (else `409 SALARY_ASSIGNMENT_OVERLAP`; other revisions are never shrunk automatically); if P exists require Te ≤ coalesce(P.effective_to, ∞) **or** accept Te up to N.from when P.effective_to source is SUCCESSOR (the gap is P's). R.effective_to_source = USER.
   - Else if P exists: R.effective_to = P.effective_to and R.effective_to_source = P.effective_to_source (R inherits P's remaining span).
   - Else if N exists: R.effective_to = N.from, source SUCCESSOR.
   - Else if E set: R.effective_to = E + 1, source EMPLOYMENT_END.
   - Else: null, source NONE.
7. If P exists: record R.predecessor_assignment_id = P.id and R.predecessor_original_effective_to = P.effective_to; set P.effective_to = F, P.effective_to_source = SUCCESSOR; write BOUNDARY_ADJUSTED event. (If P is EXPIRED this is a backdated change already authorized in step 2.)
8. Insert R (status derived per §18.2), its components, a CONFIRMED event with component_changes vs. P (or vs. the latest earlier assignment), and an audit record.
9. Database constraint (§23.5) re-verifies INV-1; on violation the transaction rolls back with `409 SALARY_ASSIGNMENT_OVERLAP`.
10. Commit.

Scheduling a future revision therefore changes only the current assignment's **system-managed end boundary**; its values, snapshots, effective_from, reason and approvals are untouched, and no EXPIRED record is affected.

### 18.4 Operation O2: Cancel a Scheduled Revision R
Allowed only when R is SCHEDULED (effective_from > today). In one transaction:
1. Lock the employee row.
2. Set R.assignment_status = CANCELLED with cancellation_type, reason, actor, timestamp.
3. Let P = confirmed assignment with effective_to = R.effective_from. If P exists and P.effective_to_source = SUCCESSOR: set P.effective_to = R.effective_to and P.effective_to_source = R.effective_to_source (P resumes R's span). If P.effective_to_source is USER or EMPLOYMENT_END, P is not changed.
4. Write CANCELLED and BOUNDARY_ADJUSTED events; audit.

No component value, snapshot or effective_from of any other assignment changes; EXPIRED records are never modified by a cancellation.

### 18.5 Operation O3: Correct an ACTIVE or EXPIRED Assignment A
For data-entry errors in values. Requires salary.revision_correct and a reason (≥ 20 chars). In one transaction: lock employee; set A to CANCELLED (cancellation_type CORRECTION); insert A′ with identical effective_from, effective_to, effective_to_source, predecessor links and corrected values; set A.superseded_by_assignment_id = A′.id; write CORRECTED event. Changing the effective_from of an ACTIVE/EXPIRED assignment is not supported in v1 (use a backdated revision O1 or see §29 Q-08).

### 18.6 Operation O4: Employment End Date Changes
When employment_end_date E is set or changed (same transaction as the employee update, employee row locked):
1. If any confirmed assignment has effective_from > E → reject `422 SALARY_AFTER_EMPLOYMENT_END` listing the assignment IDs (user must cancel them first).
2. The assignment containing E: if effective_to is null or > E + 1, set effective_to = E + 1, source EMPLOYMENT_END (BOUNDARY_ADJUSTED event).
3. If E is cleared (e.g., resignation withdrawn): the assignment whose effective_to_source = EMPLOYMENT_END gets effective_to = null, source NONE.

### 18.7 Worked Example (today = 2026-10-28)

| Step | Action | Resulting timeline |
|---|---|---|
| 1 | A1 confirmed from 2025-04-01 | A1 [2025-04-01, ∞) ACTIVE |
| 2 | Schedule R1 from 2027-04-01 | A1 [2025-04-01, 2027-04-01) ACTIVE (source SUCCESSOR); R1 [2027-04-01, ∞) SCHEDULED |
| 3 | Schedule R2 from 2027-01-01 | P = A1, N = R1. R2 inherits A1's end: R2 [2027-01-01, 2027-04-01) SCHEDULED (source SUCCESSOR); A1 [2025-04-01, 2027-01-01) |
| 4 | Cancel R1 | P = R2 (to = 2027-04-01, SUCCESSOR) → R2 [2027-01-01, ∞). A1 unchanged |
| 5 | Try to confirm R3 from 2027-01-01 | Rejected: R2 starts on the same date |
| 6 | On 2027-01-01 | As-of query returns R2; job marks A1 EXPIRED, R2 ACTIVE |
| 7 | Backdated R4 from 2026-09-01 (HRA, reason) | P = A1; A1 [2025-04-01, 2026-09-01); R4 [2026-09-01, 2027-01-01) inherits A1's former end |

### 18.8 Transaction and Constraint Requirements
- Isolation level READ COMMITTED with the explicit employee-row lock is sufficient; all timeline operations MUST acquire that lock first to avoid deadlocks (lock order: employee → assignments).
- PostgreSQL: exclusion constraint (§23.5) guarantees INV-1 at the database level.
- MySQL: no exclusion constraints; use the employee row lock, an in-transaction overlap check query, a unique constraint on a generated column `(employee_id, confirmed_from)` preventing same-start duplicates, and a nightly integrity job that reports any overlap as a P1 defect.
- Application validation alone is insufficient and MUST NOT be the only safeguard.

---

## 19. Employee ID Generation Configuration

### 19.1 Rule Fields (`employee_id_generation_rules`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | FK; every rule belongs to exactly one organization | | |
| department_id | UUID | O | Composite FK departments (same organization); null = organization-wide | | |
| rule_name | VARCHAR(100) | M | 2–100 | IT department codes | Editable anytime |
| rule_code | VARCHAR(30) | M | V-CODE; unique per organization; immutable | IT-2027 | |
| prefix | VARCHAR(10) | O | `^[A-Z0-9]{1,10}$` | EMP | `{PREFIX}` |
| suffix | VARCHAR(10) | O | `^[A-Z0-9]{1,10}$` | X | `{SUFFIX}` |
| format_pattern | VARCHAR(100) | M | Tokens: {ORG}, {DEPT}, {PREFIX}, {SUFFIX}, {YEAR}, {SEQ}, {SEP}; literals A–Z, 0–9, "-", "/", "_". {SEQ} exactly once. {PREFIX}/{SUFFIX} only if those fields are set. {DEPT} allowed in org-wide rules (uses the employee's department) | {ORG}{SEP}{DEPT}{SEP}{SEQ} | Authoritative format |
| separator | VARCHAR(1) | O | One of "-", "/", "_" ; required if pattern contains {SEP} | - | |
| include_organization_code | BOOLEAN | DER | = pattern contains {ORG} | true | Filter/display |
| include_department_code | BOOLEAN | DER | = pattern contains {DEPT} | true | |
| include_year | BOOLEAN | DER | = pattern contains {YEAR} | false | |
| year_format | ENUM | C | Required iff pattern contains {YEAR}: YYYY, YY, FY_SHORT (e.g., 2627), FY_LONG (e.g., 2026-27) | YYYY | FY uses organization financial_year_start_month |
| sequence_reset_policy | ENUM | M | NEVER (default), CALENDAR_YEAR, FINANCIAL_YEAR. CALENDAR_YEAR requires {YEAR} with YYYY/YY; FINANCIAL_YEAR requires FY_SHORT/FY_LONG (prevents post-reset collisions) | NEVER | |
| sequence_group_code | VARCHAR(30) | M | V-CODE; default = rule_code; rules sharing a group share the counter and must have identical increment and reset policy | IT-SERIES | Continue numbering across rule versions |
| sequence_start | BIGINT | M | ≥ 0; default 1; used only when a sequence row is first created | 1 | |
| sequence_increment | SMALLINT | M | 1–100; default 1 | 1 | |
| sequence_padding_length | SMALLINT | M | 1–10; default 4 | 4 | Left zero-pad; longer numbers are not truncated |
| last_issued_sequence | BIGINT | DER | Mirror of the current scope's sequence row | 37 | Authoritative value in employee_id_sequences |
| effective_from | DATE | M | | 2027-04-01 | Inclusive |
| effective_to | DATE | O | > effective_from; exclusive | | |
| priority | SMALLINT | M | 1–999; 1 = highest; default 100 | 100 | |
| status | ENUM | SYS/M | DRAFT (editable), then derived on publish: SCHEDULED (from > today), ACTIVE, EXPIRED (to ≤ today); ARCHIVED (manual) | ACTIVE | |
| created_by, created_at, updated_by, updated_at | | SYS | | | |

Rendered code constraints: total length ≤ 30; matches `^[A-Z0-9][A-Z0-9/_-]{0,29}$`; upper-cased.

### 19.2 Format Examples (illustrative, not mandatory)

| Pattern | Settings | Output |
|---|---|---|
| `{ORG}{SEP}{SEQ}` | ORG=ABC, SEP="-", padding 4 | ABC-0001 |
| `{ORG}{SEP}{DEPT}{SEP}{SEQ}` | DEPT=IT | ABC-IT-0001 |
| `{DEPT}{SEP}{YEAR}{SEP}{SEQ}` | year_format YYYY, padding 5, reset CALENDAR_YEAR | IT-2027-00001 |
| `{ORG}{SEP}{DEPT}{SEP}{YEAR}{SEP}{SEQ}` | year_format FY_SHORT, reset FINANCIAL_YEAR | ABC-IT-2627-0001 |
| `{PREFIX}{SEQ}{SUFFIX}` | PREFIX=EMP, SUFFIX=C, padding 6 | EMP000001C |

### 19.3 Rule Lifecycle and Editing
- DRAFT: fully editable; not used for generation.
- Publish (`POST …/publish`): validates pattern and conflicts; status becomes SCHEDULED/ACTIVE/EXPIRED by date.
- After publish, if the rule has issued any code **or** its effective_from ≤ today: format_pattern, prefix, suffix, separator, year_format, sequence settings, department_id, priority and effective_from are immutable. Only rule_name, effective_to (≥ today and > effective_from) and archival may change.
- A published rule with no issued codes and effective_from > today is fully editable.
- Changing a rule never changes codes already issued.
- ARCHIVED rules are excluded from selection; their history and issued codes remain readable.

### 19.4 Conflict Prevention
Two rules conflict if they share organization_id, department scope (both the same department, or both organization-wide), priority, both are not DRAFT/ARCHIVED, and their effective intervals overlap. Conflicts are rejected on publish/edit with `409 ID_RULE_CONFLICT` and enforced by a DB exclusion constraint (§23.5). Rules with different priorities may overlap; precedence is deterministic (§19.5).

### 19.5 Rule Selection Algorithm
Input: organization O, employee department D, basis date B (`employee_id_date_basis`: JOINING_DATE = employee joining_date (default); CREATION_DATE = today at creation).

1. Candidates = rules where organization_id = O, status ∉ {DRAFT, ARCHIVED}, effective_from ≤ B < coalesce(effective_to, ∞), and (department_id = D or department_id IS NULL). EXPIRED rules remain candidates when B falls within their interval (backdated joiners).
2. Order by: department-specific first; priority ascending; effective_from descending; rule_code ascending.
3. Select the first. If none: `422 NO_APPLICABLE_ID_RULE` (message includes organization code, department code and basis date). The system never falls back to an unrelated rule.

Department-specific rules apply only to employees of that department and organization.

**Transition example:** Rule A `[2026-01-01, 2027-04-01)`, Rule B `[2027-04-01, ∞)`, both organization-wide. Employee joining 2027-03-31 (created on 2027-04-05) → A. Employee joining 2027-04-01 → B. Exactly at the transition date the new rule applies (inclusive start, exclusive end).

### 19.6 Generation Algorithm
Triggered at employee creation (ON_CREATE) or activation (ON_ACTIVATION), inside the same transaction that inserts/updates the employee:

1. Resolve rule (§19.5).
2. Compute scope_key: NEVER → `ALL`; CALENDAR_YEAR → `CY{yyyy}` of B; FINANCIAL_YEAR → `FY{start yyyy}` of B.
3. Lock the sequence row: `SELECT … FROM employee_id_sequences WHERE organization_id = O AND sequence_group_code = G AND scope_key = K FOR UPDATE`. If absent: `INSERT … (last_issued_value = sequence_start − increment) ON CONFLICT DO NOTHING` (MySQL: `INSERT IGNORE`), then select FOR UPDATE again.
4. candidate = last_issued_value + increment; render code with tokens, padding and upper-casing.
5. If the rendered code exists in `employee_code_registry` for O (any status, any rule, including expired rules and manual codes) → candidate += increment; repeat. After 1,000 consecutive collisions → `409 ID_SEQUENCE_COLLISION_LIMIT`.
6. Update the sequence row (`last_issued_value = candidate`), insert the registry row, set `employees.employee_code`, update the rule's display mirror.
7. Commit with the employee transaction.

The next sequence is **never** derived by counting employees or reading MAX(code).

### 19.7 Concurrency, Gaps and Retries
- The sequence row lock serializes code issuance per (organization, group, scope); the employee-create transaction MUST NOT perform file uploads or external calls while holding it (target lock hold < 200 ms).
- Failed transactions roll back the sequence update, so failures do not create gaps.
- Gaps arise only when committed codes are later retired (discarded drafts, reassignment). Retired codes stay in the registry and are never reissued.
- `POST /employees` accepts an `Idempotency-Key` header (UUID). Retries with the same key within 24 hours return the original response and do not consume another sequence value.
- Employee codes are unique within an organization across all records, including archived records and previous employment records; rehires always receive new codes.

### 19.8 Preview, Manual Codes and Reassignment
- **Preview** (`POST …/employee-id-rules/preview` with department_id and basis date): returns the selected rule and the indicative next code computed from the current sequence value without locking or writing. Response field `indicative = true`. Preview never consumes sequence numbers.
- **Manual code** (when allow_manual_employee_code = true and caller has employee.code_manual): validated against the rendered-code pattern and registry uniqueness; registered with `source = MANUAL`, `rule_id = null`. Does not advance any sequence; generators skip it via collision checks.
- **Reassignment** (`POST …/employees/{id}/code-reassignment`, employee.code_reassign): body `{ "mode": "REGENERATE" | "MANUAL", "new_code"?: "...", "reason": "≥ 20 chars" }`. In one transaction: old registry row status → RETIRED (reserved forever), new code issued/registered, employee updated, audit event `EMPLOYEE_CODE_REASSIGNED` with old and new codes. Codes never change automatically (e.g., on department change).

### 19.9 Sequence and Registry Tables

**`employee_id_sequences`**

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | FK | | |
| sequence_group_code | VARCHAR(30) | M | | IT-SERIES | |
| scope_key | VARCHAR(20) | M | ALL, CYyyyy, FYyyyy | FY2026 | |
| last_issued_value | BIGINT | M | ≥ sequence_start − increment | 37 | |
| updated_at | TIMESTAMP | SYS | | | |

Unique: (organization_id, sequence_group_code, scope_key).

**`employee_code_registry`** (append-mostly)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id | UUID | M | FK | | |
| employee_code_normalized | VARCHAR(30) | M | Upper-case; unique per organization | ABC-IT-0038 | |
| employee_id | UUID | M | FK employees | | |
| source | ENUM | M | GENERATED, MANUAL | GENERATED | |
| rule_id | UUID | C | Required when GENERATED | | |
| sequence_group_code, scope_key, sequence_value | VARCHAR, VARCHAR, BIGINT | C | Required when GENERATED | | |
| status | ENUM | M | ASSIGNED, RETIRED | ASSIGNED | Only change allowed: ASSIGNED → RETIRED |
| retired_reason | VARCHAR(500) | C | Required when RETIRED | Draft discarded | |
| issued_at, issued_by | TIMESTAMP, UUID | SYS | | | |
| retired_at, retired_by | TIMESTAMP, UUID | SYS | | | |

---

## 20. Audit History and Record Versioning

### 20.1 Audit Log (`audit_logs`, append-only)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| occurred_at | TIMESTAMP | SYS | UTC | 2026-10-28T07:01:22Z | |
| organization_id | UUID | C | Null only for platform events | | Organization scope |
| actor_user_id | UUID | M | FK users; system user for jobs | | |
| actor_role_codes | VARCHAR(100) | M [H] | Roles active at time of action, sorted, e.g., "HRA" | HRA | |
| action | VARCHAR(60) | M | Controlled vocabulary (§20.2) | EMPLOYEE_UPDATED | |
| entity_type | VARCHAR(60) | M | Table/aggregate name | employee | |
| entity_id | UUID | M | | | |
| employee_id | UUID | O | For employee-related events | | Fast per-employee audit view |
| changed_fields | JSON(schema) | O | Array of field names | ["designation_id","grade_id"] | |
| before_values | JSON(schema) | O | Non-sensitive values only; sensitive fields recorded as `"***changed***"` or masked | | |
| after_values | JSON(schema) | O | Same rules | | |
| reason | VARCHAR(1000) | C | Required for backdated, correction, reveal, override, break-glass, reassignment | | |
| request_id | VARCHAR(64) | O | X-Request-Id / correlation ID | | |
| ip_address | VARCHAR(45) | O | | | Personal data; retention §25.6 |
| user_agent | VARCHAR(300) | O | | | |
| integrity_hash | CHAR(64) | SYS | SHA-256 of (previous hash + record) per organization chain [optional] | | Tamper evidence |

The application database role has INSERT and SELECT only on `audit_logs`; UPDATE/DELETE are revoked. Audit records are written in the same transaction as the change.

### 20.2 Audited Events

| Area | Actions |
|---|---|
| Employees | EMPLOYEE_CREATED, EMPLOYEE_UPDATED, EMPLOYEE_ACTIVATED, EMPLOYEE_STATUS_CHANGED, EMPLOYEE_STATUS_CORRECTED, EMPLOYEE_ARCHIVED, EMPLOYEE_JOB_CHANGED, EMPLOYEE_ORG_LINKED (rehire/transfer), AGE_EXCEPTION_DECIDED |
| Sensitive access | IDENTIFIER_REVEALED, BANK_ACCOUNT_REVEALED, RESTRICTED_PERSONAL_VIEWED (disability/religion/category detail view), BREAK_GLASS_GRANTED, BREAK_GLASS_ACCESS |
| Identifiers & bank | IDENTIFIER_ADDED, IDENTIFIER_CHANGED, DUPLICATE_OVERRIDE, BANK_ACCOUNT_ADDED, BANK_ACCOUNT_CLOSED, BANK_PRIMARY_CHANGED, BANK_VERIFIED |
| Repeated records | EDUCATION_*, PREVIOUS_EMPLOYMENT_*, FAMILY_*, NOMINATION_SET_REPLACED |
| Documents | DOCUMENT_UPLOADED, DOCUMENT_UPLOAD_FAILED, DOCUMENT_QUARANTINED, DOCUMENT_DOWNLOADED, DOCUMENT_VERSION_ADDED, DOCUMENT_VERIFIED, DOCUMENT_REJECTED, DOCUMENT_ARCHIVED, DOCUMENT_PURGED |
| Salary | SALARY_COMPONENT_*, SALARY_TEMPLATE_CREATED, SALARY_TEMPLATE_VERSION_PUBLISHED, SALARY_TEMPLATE_ARCHIVED, SALARY_REVISION_CREATED, SALARY_REVISION_UPDATED, SALARY_REVISION_SUBMITTED, SALARY_REVISION_APPROVED, SALARY_REVISION_REJECTED, SALARY_REVISION_CONFIRMED, SALARY_REVISION_CANCELLED, SALARY_REVISION_CORRECTED, SALARY_BOUNDARY_ADJUSTED |
| ID rules | ID_RULE_CREATED, ID_RULE_UPDATED, ID_RULE_PUBLISHED, ID_RULE_ARCHIVED, EMPLOYEE_CODE_ISSUED, EMPLOYEE_CODE_MANUAL, EMPLOYEE_CODE_REASSIGNED, EMPLOYEE_CODE_RETIRED |
| Organization | ORG_CREATED, ORG_UPDATED, ORG_STATUS_CHANGED, REGISTRATION_*, SETTINGS_UPDATED, BRANCH_*, MASTER_*, LOOKUP_*, DOCUMENT_CATEGORY_*, IDENTIFIER_POLICY_UPDATED |
| Access | USER_CREATED, USER_DEACTIVATED, ORG_ACCESS_GRANTED, ORG_ACCESS_REVOKED, ROLE_PERMISSIONS_CHANGED, LOGIN_SUCCESS, LOGIN_FAILURE, MFA_RESET |

### 20.3 Sensitive Data in Audit
Audit records MUST NOT contain: passwords, tokens, secrets, encryption keys, full Aadhaar, full PAN, full bank account numbers, full passport/UAN/ESIC/PF identifiers, file contents. Identifier changes are recorded as field name + masked before/after (last 4). Salary values MAY be stored in before/after values; audit views of salary events require salary.view.

### 20.4 Record Versioning Approach
- **History tables** (authoritative business history): employee_job_history, employee_status_history, salary assignments (immutable confirmed rows) and salary_revision_history, salary_template_versions, employee_identifiers (non-current rows), employee_nominees (effective-dated sets), employee_bank_accounts (effective-dated), employee_documents (version chains), employee_code_registry, ID rules (immutable after use).
- **Audit logs** cover all other changes (field-level before/after).
- Optimistic locking via row_version prevents lost updates (`412 PRECONDITION_FAILED` on stale ETag).
- Historical salary and ID rule records remain readable to authorized users indefinitely within retention policy.

---

## 21. Data Validation and Business Constraints

### 21.1 Validation Rule Dictionary
Normalization is applied before validation: trim; Unicode NFC; collapse internal whitespace; identifiers upper-cased and stripped of spaces/hyphens unless stated.

| Rule ID | Applies to | Normalization | Pattern / check | Default severity |
|---|---|---|---|---|
| V-NAME | Person names | Trim, NFC, collapse spaces | `^[\p{L}\p{M}][\p{L}\p{M} .'\-]{0,n−1}$`; at least one letter; no digits; no consecutive punctuation | Error |
| V-CODE | Master/rule/component codes | Upper-case, trim | `^[A-Z0-9][A-Z0-9_./-]{0,29}$` | Error |
| V-EMAIL | Emails | Trim, lower-case | WHATWG "valid e-mail address"; ≤ 254 chars; domain contains a dot | Error |
| V-PHONE | Phones | Strip spaces, hyphens, parentheses; 10-digit Indian input or leading 0 → +91 | India: `^\+91[6-9][0-9]{9}$`; other countries: E.164 `^\+[1-9][0-9]{7,14}$` | Error |
| V-POSTAL | Postal codes | Trim, upper-case | IN: `^[1-9][0-9]{5}$`; other: `^[A-Z0-9][A-Z0-9 -]{1,9}$` | Error |
| V-URL | Websites | Trim | `http://` or `https://` scheme; valid host; ≤ 255 | Error |
| V-PAN | PAN | Upper, strip spaces | `^[A-Z]{5}[0-9]{4}[A-Z]$`; employee PAN 4th char = P (configurable) | Error |
| V-TAN | TAN | Upper | `^[A-Z]{4}[0-9]{5}[A-Z]$` | Error |
| V-CIN | CIN | Upper | `^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$` | Error |
| V-LLPIN | LLPIN | Upper | `^[A-Z]{3}-[0-9]{4}$` | Error |
| V-GSTIN | GSTIN | Upper | `^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$`; chars 1–2 valid state code; check character per GSTN checksum (configurable) | Error |
| V-UDYAM | Udyam | Upper | `^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$` | Error |
| V-AADHAAR | Aadhaar | Digits only | `^[2-9][0-9]{11}$` + Verhoeff checksum | Error |
| V-UAN | UAN | Digits only | `^[0-9]{12}$` | Error |
| V-PFMID | PF member ID | Upper; strip "/", spaces | `^[A-Z]{5}[0-9]{7}[0-9]{3}[0-9]{7}$` (22 chars) | Error |
| V-PF-EST | PF establishment code | Upper; strip "/", spaces | `^[A-Z]{5}[0-9]{7}([0-9]{3})?$` | Error |
| V-ESIC-IP | ESIC IP number | Digits only | `^[0-9]{10}$` | Error |
| V-ESIC-EMP | ESIC employer code | Digits only | `^[0-9]{17}$` | Error |
| V-PASSPORT | Passport | Upper | issuing_country IN: `^[A-Z][0-9]{7}$`; other: `^[A-Z0-9]{5,20}$` | Error |
| V-EPIC | Voter ID | Upper | `^[A-Z]{3}[0-9]{7}$` (older formats exist) | Warning |
| V-DL-IN | Driving licence | Upper; strip spaces, hyphens | `^[A-Z]{2}[0-9]{2}[0-9]{4}[0-9]{7}$` (older formats exist) | Warning |
| V-IFSC | IFSC | Upper | `^[A-Z]{4}0[A-Z0-9]{6}$` | Error |
| V-BANKACC | Bank account | Strip spaces | `^[0-9]{9,18}$` (configurable) | Error |
| V-REGNO | Other registration numbers | Upper; trim | `^[A-Z0-9][A-Z0-9 /._-]{0,49}$`; optional per-state pattern | Error |

### 21.2 Configurable Identifier Validation (`identifier_type_configs`, platform-level, SSA)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| identifier_type | VARCHAR(30) | M | PK; includes employee and organization identifier types | PAN | |
| pattern | VARCHAR(200) | M | Must compile | ^[A-Z]{5}[0-9]{4}[A-Z]$ | |
| normalization | ENUM | M | UPPER_STRIP, DIGITS_ONLY, UPPER_STRIP_SLASH | UPPER_STRIP | |
| checksum_algorithm | ENUM | M | NONE, VERHOEFF, GSTIN_MOD36 | NONE | |
| severity | ENUM | M | ERROR, WARNING | ERROR | |
| mask_visible_chars | SMALLINT | M | 0–4 | 4 | |
| updated_by, updated_at | | SYS | | | Changes audited; apply to new writes only |

### 21.3 Warnings Protocol
Warnings never block silently and are never ignored silently. Write requests that trigger warnings return `422 WARNINGS_REQUIRE_ACKNOWLEDGEMENT` with `warnings[]` (code, field, message). The client resubmits with `acknowledged_warnings: ["W-…", …]`; acknowledged warnings are stored in the audit record.

### 21.4 Consolidated Cross-Entity Constraints

| ID | Constraint | Enforcement |
|---|---|---|
| C-01 | Employee belongs to exactly one organization; organization_id immutable | NOT NULL + no update path |
| C-02 | Employee code unique per organization across all records | Unique index on registry and employees |
| C-03 | Master references same organization | Composite FKs |
| C-04 | Manager/HR contact/referrer same organization; no self; no cycles | Composite FK + service check |
| C-05 | Confirmed salary intervals non-overlapping | Exclusion constraint / lock + checks |
| C-06 | One current identifier per type per employee (except previous PF) | Partial unique index |
| C-07 | At most one primary bank account at a time | Exclusion constraint / lock + checks |
| C-08 | Nomination shares total 100.00 where required | Service validation on set replacement |
| C-09 | One current document version per chain; single-current categories enforced | Partial unique index |
| C-10 | Template versions non-overlapping per template | Exclusion constraint |
| C-11 | ID rules non-conflicting (same scope, priority, overlapping) | Exclusion constraint |
| C-12 | Sequence values atomic | Row lock on sequence row |
| C-13 | Child records' document references belong to same employee | Composite FKs |
| C-14 | Dates: end ≥ start (inclusive fields), to > from (exclusive fields) | CHECK constraints |
| C-15 | Monetary values ≥ 0 | CHECK constraints |

---

## 22. Database Entities and Relationships

| Parent | Child | Cardinality | FK (child) | Organization consistency | On delete |
|---|---|---|---|---|---|
| organization_groups | organizations | 1:N | group_id | — | RESTRICT |
| organizations | organization_settings | 1:1 | organization_id | — | RESTRICT |
| organizations | organization_registrations | 1:N | organization_id | branch via composite FK | RESTRICT |
| organizations | organization_files | 1:N | organization_id | — | RESTRICT |
| organizations | branches | 1:N | organization_id | — | RESTRICT |
| organizations | departments, designations, grades, business_units, cost_centers, employment_types, work_locations, employee_categories | 1:N | organization_id | — | RESTRICT |
| departments | sub_departments | 1:N | (organization_id, department_id) | composite | RESTRICT |
| organizations | lookup_values, document_categories, salary_components, identifier_requirement_policies, nomination_type_configs, employee_custom_statuses | 1:N | organization_id | — | RESTRICT |
| organizations | employees | 1:N | organization_id | — | RESTRICT |
| masters (each) | employees | 1:N | (organization_id, x_id) | composite | RESTRICT |
| employees | employees (manager, HR contact, referrer) | 1:N | (organization_id, …_employee_id) | composite | RESTRICT |
| employees | employees (previous record) | 1:1 optional | previous_employment_record_id | cross-org allowed | RESTRICT |
| employees | employee_addresses, employee_identifiers, employee_job_history, employee_status_history, employee_education, employee_previous_employment, employee_family_members, employee_nominees, employee_bank_accounts, employee_documents, employee_salary_assignments | 1:N | (organization_id, employee_id) | composite | RESTRICT |
| employee_previous_employment | employee_previous_employment_documents | 1:N | previous_employment_id | document same employee | CASCADE (link rows only) |
| employee_documents | employee_documents (versions) | 1:N | supersedes_document_id | same employee | RESTRICT |
| document_categories | document_category_mime_types | 1:N | document_category_id | — | CASCADE |
| salary_templates | salary_template_versions | 1:N | (organization_id, template_id) | composite | RESTRICT |
| salary_template_versions | salary_template_components | 1:N | template_version_id | component same org | CASCADE only while DRAFT; RESTRICT after publish |
| salary_template_versions | employee_salary_assignments | 1:N | (organization_id, template_version_id) | composite | RESTRICT |
| employee_salary_assignments | employee_salary_components | 1:N | salary_assignment_id | — | RESTRICT |
| employee_salary_assignments | salary_revision_history | 1:N | salary_assignment_id | — | RESTRICT |
| organizations | employee_id_generation_rules, employee_id_sequences, employee_code_registry | 1:N | organization_id | department composite | RESTRICT |
| users | user_organization_access | 1:N | user_id | — | RESTRICT |
| roles | role_permissions ↔ permissions | M:N | role_id, permission_id | — | CASCADE (link rows) |
| organizations | user_organization_access | 1:N | organization_id | — | RESTRICT |
| users | employees (optional link) | 1:1 optional | users.employee_id | — | SET NULL |

No hard cascades delete business data; only pure link rows cascade.

---

## 23. Recommended Database Schema

### 23.1 General Conventions
- PostgreSQL 15+ recommended (exclusion constraints, RLS, partial indexes, pg_trgm). MySQL 8.0+ supported with the compensating controls stated.
- All business tables include standard columns (§1.5). Columns for each table are exactly those in the field catalogs referenced below.
- Every organization-scoped table has `UNIQUE (organization_id, id)` to enable composite foreign keys.
- No comma-separated multi-value columns. JSON only for: `employee_id_format_settings`, audit changed/before/after values, `salary_revision_history.component_changes` (all schema-validated).
- Money DECIMAL(15,2); percentages DECIMAL(7,4) (component) / DECIMAL(5,2) (shares, disability).
- Case-insensitive uniqueness via functional indexes on `upper(code)` / `lower(email)` (MySQL: generated normalized columns).

### 23.2 Entity Catalog

| Table | Purpose | Field catalog | Organization isolation | Archival policy |
|---|---|---|---|---|
| organization_groups | Group companies | §7.6 | Platform | Deactivate only |
| organizations | Legal entities | §7.2 | Tenant root | Deactivate; never delete once used |
| organization_settings | Per-org configuration | §7.5 | organization_id PK | Not archived |
| organization_registrations | Statutory registrations | §7.4 | organization_id | Status INACTIVE; history retained |
| organization_files | Org-level files (logo, certificates) | id, organization_id, purpose, storage_key, mime_type, file_size_bytes, checksum, uploaded_by/at | organization_id | Archive |
| branches | Locations | §8.1 | organization_id | Deactivate |
| departments … employee_categories | Masters | §9.1–9.2 | organization_id | Deactivate |
| lookup_values | Option lists | §9.3 | organization_id | Deactivate |
| employee_custom_statuses | Custom statuses | §10.2 | organization_id | Deactivate |
| employees | Employment records | §10.4–10.6, §11.4 | organization_id | Archive (drafts/erroneous only); separated records retained |
| employee_addresses | Current/permanent addresses | §10.5 | composite FK | Replaced in place (audited) |
| employee_job_history | Job snapshots | §10.8 | composite FK | Append-only |
| employee_status_history | Status changes | §10.8 | composite FK | Append-only |
| identifier_type_configs | Validation config | §21.2 | Platform | Not archived |
| identifier_requirement_policies | Collection policy | §11.3 | organization_id | Not archived |
| employee_identifiers | Encrypted identifiers | §11.1 | composite FK | Non-current rows retained |
| employee_education | Education | §12.1 | composite FK | Archive |
| employee_previous_employment | Prior jobs | §12.2 | composite FK | Archive |
| employee_previous_employment_documents | Payslip links | §12.2 | via parent | Delete with link |
| employee_family_members | Family | §13.1 | composite FK | Archive |
| nomination_type_configs | Nomination rules | §13.3 | organization_id | Not archived |
| employee_nominees | Nominees | §13.2 | composite FK | Effective-dated; retained |
| employee_bank_accounts | Bank accounts | §14.1 | composite FK | Effective-dated; retained |
| document_categories / document_category_mime_types | Document configuration | §15.2 | organization_id | Deactivate |
| employee_documents | Document metadata and versions | §15.3 | composite FK | Archive; purge per retention |
| salary_components | Component catalog | §16.3 | organization_id | Deactivate |
| salary_templates | Template header | §16.4 | organization_id | Archive |
| salary_template_versions | Versions | §16.5 | composite FK | ARCHIVED status |
| salary_template_components | Version mappings | §16.6 | via version | Immutable after publish |
| employee_salary_assignments | Salary timeline | §17.1 | composite FK | Never deleted after confirmation |
| employee_salary_components | Assignment values | §17.2 | via assignment | Immutable after confirmation |
| salary_revision_history | Revision events | §17.5 | composite FK | Append-only |
| employee_id_generation_rules | ID rules | §19.1 | organization_id | ARCHIVED status |
| employee_id_sequences | Counters | §19.9 | organization_id | Never deleted |
| employee_code_registry | All issued codes | §19.9 | organization_id | Never deleted |
| users | Accounts | id, email (unique), display_name, employee_id (O), auth_provider, mfa_enabled, status, last_login_at, standard columns | Platform | Deactivate |
| roles | Role definitions | id, role_code (unique), name, is_system | Platform | Not deleted if used |
| permissions | Permission codes | id, permission_code (unique), description | Platform | Static |
| role_permissions | Role ↔ permission | role_id, permission_id | Platform | Link |
| user_organization_access | Grants | id, user_id, organization_id, role_id, granted_by, granted_at, expires_at (O; required for break-glass), revoked_at, reason | organization_id | Revoked rows retained |
| audit_logs | Audit trail | §20.1 | organization_id (nullable for platform) | Append-only; retention §25.6 |
| states (reference) | Indian states/UTs with GST state codes | code CHAR(2) PK, name, status | Platform | Static |

### 23.3 Keys, Constraints and Indexes

| Table | Unique constraints | Check constraints | Key indexes |
|---|---|---|---|
| organizations | upper(organization_code); cin; llpin; pan (partial: entity_type ≠ PROPRIETORSHIP) | entity_type in list; CIN/LLPIN applicability per §7.3; financial_year_start_month 1–12 | status |
| organization_registrations | (registration_type, number) for system-wide types; (organization_id, registration_type, state_code, number) others | state_code required for state-scoped types; effective_to > effective_from | (organization_id, registration_type, state_code) |
| branches | (organization_id, upper(branch_code)); (organization_id, id) | status in list | (organization_id, status) |
| masters | (organization_id, upper(code)); (organization_id, id); grades: (organization_id, rank) | status in list | (organization_id, status, display_order) |
| employees | (organization_id, upper(employee_code)) where code not null; (organization_id, id); official email partial unique per scope | date_of_birth ≤ created date; employment_end_date ≥ employment_start_date; confirmation_date ≥ joining_date; probation_end_date ≥ joining_date; reporting_manager_employee_id ≠ id; disability_percentage 0–100 | (organization_id, status); (organization_id, department_id); (organization_id, designation_id); (organization_id, branch_id); (organization_id, joining_date); (organization_id, reporting_manager_employee_id); (organization_id, lower(official_email)); trigram GIN on full_name_normalized; (organization_id, employment_type_id) |
| employee_addresses | (employee_id, address_type) | address_type in (CURRENT, PERMANENT) | employee_id |
| employee_identifiers | partial unique (employee_id, identifier_type) where is_current and type ≠ PREVIOUS_PF_MEMBER_ID | expiry_date > issue_date | (organization_id, identifier_type, value_blind_index); group-level index on (identifier_type, value_blind_index) |
| employee_education | — | result rules §12.1; start_year ≤ passing_year | employee_id |
| employee_previous_employment | — | end ≥ start; last_drawn_ctc ≥ 0 | employee_id |
| employee_nominees | — | share 0 < x ≤ 100; effective_to > effective_from | (employee_id, nomination_type, effective_from) |
| employee_bank_accounts | Exclusion: one primary per employee per date | effective_to > effective_from | (organization_id, account_number_blind_index); employee_id |
| employee_documents | partial unique (supersedes_document_id) where not null; partial unique current per (employee_id, document_category_id, document_type) for single-current categories (enforced by service + trigger) | file_size_bytes > 0; expiry_date > issue_date; version_number ≥ 1 | (organization_id, employee_id, document_category_id, is_current); (organization_id, expiry_date) where is_current; checksum |
| salary_components | (organization_id, upper(component_code)) | value_type/amount_period consistency; typed value check | (organization_id, status) |
| salary_templates | (organization_id, upper(template_code)) | — | organization_id |
| salary_template_versions | (template_id, version_number); exclusion: no overlapping ACTIVE intervals per template | effective_to > effective_from | (template_id, status, effective_from) |
| salary_template_components | (template_version_id, component_id) | typed value check | template_version_id |
| employee_salary_assignments | Exclusion on confirmed intervals (§23.5); MySQL: (employee_id, confirmed_from) | effective_to > effective_from; backdate_reason when is_backdated | (organization_id, employee_id, effective_from); (organization_id, assignment_status, effective_from) |
| employee_salary_components | (salary_assignment_id, component_code_snapshot) | typed value check; amounts ≥ 0 | salary_assignment_id |
| employee_id_generation_rules | (organization_id, upper(rule_code)); exclusion: conflict rule §19.4 | effective_to > effective_from; padding 1–10; increment 1–100 | (organization_id, department_id, status, effective_from) |
| employee_id_sequences | (organization_id, sequence_group_code, scope_key) | last_issued_value ≥ −100 | — |
| employee_code_registry | (organization_id, employee_code_normalized) | status transitions | employee_id |
| user_organization_access | partial unique (user_id, organization_id, role_id) where revoked_at is null | expires_at > granted_at | (organization_id), (user_id) |
| audit_logs | — | — | (organization_id, occurred_at DESC); (organization_id, entity_type, entity_id); (organization_id, employee_id, occurred_at DESC); (actor_user_id, occurred_at) |

### 23.4 Soft Delete and Uniqueness with Archived Records
- Archival sets `archived_at`; archived rows are excluded from default queries but remain in uniqueness checks **unless stated**:
  - Employee codes: unique across all records including archived (never reused).
  - Master codes: unique including inactive/archived rows (codes are not reused).
  - Official email: unique only among non-separated, non-archived records (allows reuse of an address after an employee leaves).
  - Identifier duplicates: evaluated per §11.5 (archived drafts excluded).
- Hard deletes are limited to never-referenced configuration rows, never-submitted salary drafts, PENDING_UPLOAD documents, and retention purges.

### 23.5 Illustrative Constraint Definitions (PostgreSQL)
These snippets illustrate required constraints; they are not migrations.

```sql
-- Organization consistency via composite FK
ALTER TABLE employees
  ADD CONSTRAINT fk_emp_department
  FOREIGN KEY (organization_id, department_id)
  REFERENCES departments (organization_id, id);

-- Non-overlapping confirmed salary assignments (requires btree_gist)
ALTER TABLE employee_salary_assignments
  ADD CONSTRAINT ex_salary_no_overlap
  EXCLUDE USING gist (
    employee_id WITH =,
    daterange(effective_from, effective_to, '[)') WITH &&
  ) WHERE (assignment_status IN ('SCHEDULED','ACTIVE','EXPIRED'));

-- Non-conflicting ID rules (same scope, same priority, overlapping dates)
ALTER TABLE employee_id_generation_rules
  ADD CONSTRAINT ex_idrule_conflict
  EXCLUDE USING gist (
    organization_id WITH =,
    (COALESCE(department_id, '00000000-0000-0000-0000-000000000000'::uuid)) WITH =,
    priority WITH =,
    daterange(effective_from, effective_to, '[)') WITH &&
  ) WHERE (status NOT IN ('DRAFT','ARCHIVED'));

-- One primary bank account at a time
ALTER TABLE employee_bank_accounts
  ADD CONSTRAINT ex_bank_one_primary
  EXCLUDE USING gist (
    employee_id WITH =,
    daterange(effective_from, effective_to, '[)') WITH &&
  ) WHERE (is_primary AND archived_at IS NULL);
```

### 23.6 Row-Level Security (PostgreSQL, defense in depth)
- The application sets `SET LOCAL app.org_ids = '{…}'` per transaction from the authenticated user's grants.
- Policy on each organization-scoped table: `USING (organization_id = ANY (current_setting('app.org_ids')::uuid[]))`.
- Platform jobs use a separate role with explicit, audited bypass.
- MySQL: equivalent filter enforced in a mandatory repository layer; automated tests attempt cross-organization access for every endpoint.

### 23.7 Sensitive Field Encryption
- Envelope encryption: AES-256-GCM; a data encryption key (DEK) per organization, wrapped by a customer master key in a managed KMS/HSM; ciphertext stores key_version.
- Blind indexes: HMAC-SHA256 with keys separate from encryption keys; one key per organization group (or per organization if ungrouped) so cross-group matching is impossible by design.
- Key rotation: rotate wrapping keys annually [ASSUMPTION]; rotate DEKs on compromise or annually with lazy re-encryption; blind-index key rotation requires full recomputation (planned maintenance).
- Decryption occurs only in the service layer for reveal operations; keys never reside in the database or source code.

---

## 24. API and Application Behaviour Requirements

### 24.1 Conventions
- Base path `/api/v1`; JSON (UTF-8); dates `YYYY-MM-DD`; timestamps RFC 3339 UTC.
- Organization-scoped resources live under `/api/v1/orgs/{orgId}/…`. Every request checks (1) authentication, (2) caller has access to `{orgId}`, (3) resource belongs to `{orgId}`, (4) permission.
- Authentication: OAuth 2.0 / OIDC bearer tokens.
- Concurrency: GET returns `ETag` (= row_version); PATCH/PUT/POST-actions on existing resources require `If-Match` → `412 PRECONDITION_FAILED` if stale.
- Idempotency: `Idempotency-Key` required on POST create endpoints for employees, salary assignments and documents; replay returns the original response for 24 hours.
- Correlation: `X-Request-Id` accepted/returned and stored in audit logs.
- Restricted fields are omitted from responses when the caller lacks permission; masked fields use §11.6 formats.
- Lists use cursor pagination (§26).

### 24.2 Error Envelope

```json
{
  "error": {
    "code": "SALARY_ASSIGNMENT_OVERLAP",
    "message": "The salary revision overlaps an existing confirmed assignment.",
    "details": [
      { "field": "effective_from", "code": "OVERLAP", "message": "Assignment 0192… starts on 2027-01-01." }
    ],
    "warnings": [],
    "correlation_id": "c1f9b2…"
  }
}
```

### 24.3 Error Codes

| HTTP | Code | When |
|---|---|---|
| 400 | MALFORMED_REQUEST | Invalid JSON or types |
| 401 | UNAUTHENTICATED | Missing/invalid token |
| 403 | PERMISSION_DENIED | Lacks permission in an organization the caller can access |
| 403 | ORG_ACCESS_DENIED | Caller has no grant for `{orgId}` (cross-organization attempt) |
| 404 | RESOURCE_NOT_FOUND | Resource missing **or** belonging to another organization (no existence disclosure) |
| 403 | DOCUMENT_ACCESS_DENIED | Document in accessible org but restricted category without permission |
| 409 | DUPLICATE_EMPLOYEE_CODE | Manual/reassigned code already in registry |
| 409 | DUPLICATE_IDENTIFIER | Blocking duplicate per §11.5 |
| 409 | SALARY_ASSIGNMENT_OVERLAP | Timeline overlap or same-start conflict |
| 409 | ID_RULE_CONFLICT | Conflicting rule (§19.4) |
| 409 | ID_SEQUENCE_COLLISION_LIMIT | 1,000 consecutive collisions |
| 409 | CONCURRENT_MODIFICATION | Lock timeout or serialization failure (retryable) |
| 412 | PRECONDITION_FAILED | Stale If-Match |
| 413 | FILE_TOO_LARGE | Above category limit |
| 415 | FILE_TYPE_NOT_ALLOWED | Disallowed or mismatched type |
| 422 | VALIDATION_FAILED | Field-level validation errors (details[]) |
| 422 | WARNINGS_REQUIRE_ACKNOWLEDGEMENT | Unacknowledged warnings (§21.3) |
| 422 | INVALID_ORGANIZATION_REFERENCE | Referenced master/employee in another organization |
| 422 | NO_APPLICABLE_ID_RULE | No rule for organization/department/basis date |
| 422 | TEMPLATE_ARCHIVED | Assigning archived template/version |
| 422 | TEMPLATE_VERSION_NOT_EFFECTIVE | No ACTIVE version on effective_from |
| 422 | COMPONENT_NOT_OVERRIDABLE | Override of locked component |
| 422 | INVALID_STATUS_TRANSITION | Transition not in §10.3 |
| 422 | STATUS_CORRECTION_WINDOW_EXPIRED | Correction beyond window |
| 422 | BACKDATE_NOT_ALLOWED | Missing permission/reason or beyond limit |
| 422 | SALARY_AFTER_EMPLOYMENT_END | Assignment beyond employment end |
| 422 | REPORTING_CYCLE | Circular reporting |
| 422 | NOMINEE_SHARE_TOTAL_INVALID | Shares ≠ 100.00 |
| 422 | AGE_BELOW_ABSOLUTE_MINIMUM | Under hard minimum |
| 422 | FIELD_NOT_COLLECTED | Field disabled by policy |
| 422 | FIELD_READ_ONLY | SYS field supplied |
| 422 | CONFLICTING_PERMANENT_ADDRESS | §10.5 |
| 422 | MASTER_INACTIVE / MASTER_IN_USE_BY_ID_RULE | §9.4 |
| 429 | RATE_LIMITED | Over rate limit (reveal endpoints: 30/hour/user [ASSUMPTION]) |

### 24.4 Endpoint Specification
Paths below are relative to `/api/v1`. "Org" = `/orgs/{orgId}`. Request/response fields refer to the field catalogs; SYS fields are response-only.

**24.4.1 Organizations, settings, registrations**

| Method | Path | Purpose | Permission | Request | Response | Key validations / errors |
|---|---|---|---|---|---|---|
| GET | /orgs | List organizations accessible to caller | any authenticated | filters: status, group_id, q | id, organization_code, display_name, status | — |
| POST | /orgs | Create organization (seeds settings, lookups, categories, employment types, policies) | org.create | §7.2 fields | Organization | V-* rules; §7.3 applicability; 409 on duplicate code/CIN |
| GET | /orgs/{orgId} | Get organization | employee.list or org.manage | — | Organization + derived registrations summary | 404/403 |
| PATCH | /orgs/{orgId} | Edit organization | org.manage | Partial §7.2 | Organization | organization_code immutability; If-Match |
| POST | /orgs/{orgId}/deactivate · /reactivate | Change status | org.archive | reason | Organization | §7.7 |
| GET / PUT | /orgs/{orgId}/settings | Read / replace settings | org.manage | §7.5 | Settings | Range checks; purpose texts |
| GET / POST | /orgs/{orgId}/registrations | List / create registrations | org.manage (GET: employee.list) | §7.4 | Registration | Type-specific validation; uniqueness |
| PATCH | /orgs/{orgId}/registrations/{id} | Edit registration | org.manage | §7.4 | Registration | Number immutable once ACTIVE (create new row instead) |
| GET / PUT | /orgs/{orgId}/identifier-policies | Identifier requirements | org.manage | list of §11.3 rows | Policies | Purpose statement rules |
| GET / PUT | /orgs/{orgId}/nomination-configs | Nomination rules | org.manage | §13.3 | Configs | |
| GET / POST / PATCH | /org-groups, /org-groups/{id} | Manage groups | org.create | §7.6 | Group | |

**24.4.2 Branches, masters, lookups**

| Method | Path | Purpose | Permission | Request | Response | Key validations / errors |
|---|---|---|---|---|---|---|
| GET | Org/branches | List branches | employee.list | status, q, cursor | Branch list incl. derived registration numbers | |
| POST | Org/branches | Create branch | master.manage | §8.1 (+ optional gstin/S&E/factory convenience fields) | Branch | Unique code; GSTIN state = branch state |
| GET / PATCH | Org/branches/{id} | Read / edit | employee.list / master.manage | §8.1 | Branch | Code immutable once referenced |
| POST | Org/branches/{id}/deactivate · /reactivate | Status | master.manage | reason | Branch | Warning if active employees assigned |
| GET / POST | Org/masters/{type} | List / create master (`type` ∈ departments, sub-departments, designations, grades, business-units, cost-centers, employment-types, work-locations, employee-categories) | employee.list / master.manage | §9.1–9.2 | Master | Unique code; same-org references |
| GET / PATCH | Org/masters/{type}/{id} | Read / edit | as above | | Master | |
| POST | Org/masters/{type}/{id}/deactivate · /reactivate | Status | master.manage | reason | Master | MASTER_IN_USE_BY_ID_RULE |
| GET / POST / PATCH | Org/lookups/{listCode}[/{valueCode}] | Manage lookups | employee.list / master.manage | §9.3 | Lookup values | value_code immutable |
| GET / POST / PATCH | Org/custom-statuses[/{code}] | Custom statuses | master.manage | §10.2 | Status | |

**24.4.3 Employees**

| Method | Path | Purpose | Permission | Request | Response | Key validations / errors |
|---|---|---|---|---|---|---|
| GET | Org/employees | Search/list (§26) | employee.list | filters, sort, cursor | Employee summaries (field set per role) | Sensitive filters restricted |
| POST | Org/employees | Create (DRAFT) | employee.create | §10.4–10.6, §11.4, contact; Idempotency-Key | Employee incl. employee_code (if ON_CREATE), warnings | NO_APPLICABLE_ID_RULE; INVALID_ORGANIZATION_REFERENCE; age rules; duplicates |
| GET | Org/employees/{id} | Detail | employee.view (+ field permissions) | include = addresses, identifiers, … | Employee | 404 cross-org |
| PATCH | Org/employees/{id} | Edit personal/contact/statutory profile | employee.edit | Partial fields; If-Match | Employee | Job fields rejected here (use job-changes) |
| POST | Org/employees/{id}/job-changes | Effective-dated job change | employee.edit | effective_date, change_type, changed job fields, reason | Employee + history row | EMP-06/07/08/12 |
| GET | Org/employees/{id}/job-history | Job history | employee.view | cursor | Rows | |
| POST | Org/employees/{id}/activate | DRAFT → ACTIVE | employee.status | — | Employee | Activation checks §10.3 |
| POST | Org/employees/{id}/status-transitions | Status change | employee.status / status_restricted | to_status, status_effective_date, employment_end_date, status_reason, remarks, is_correction | Employee + status history | INVALID_STATUS_TRANSITION; SALARY_AFTER_EMPLOYMENT_END |
| GET | Org/employees/{id}/status-history | History | employee.view | | Rows | |
| POST | Org/employees/{id}/age-exception | Request/decide exception | employee.edit (request) / employee.status_restricted (decide) | action, reason | Employee | Decider ≠ requester |
| POST | Org/employees/{id}/archive | Discard draft / archive erroneous record | employee.archive | reason | Employee | Non-draft records with salary/docs require HRA and reason ≥ 20 |
| POST | Org/employees/{id}/transfers | Inter-organization transfer | employee.status (source) + employee.create (target) | §10.9 | Source + new target employee | Both-org access; lawful-copy confirmation flag required |
| POST | Org/employees/{id}/code-reassignment | Reassign code | employee.code_reassign | §19.8 | Employee + code history | DUPLICATE_EMPLOYEE_CODE |
| GET | Org/employees/{id}/code-history | Codes issued to record | employee.view | | Registry rows | |

**24.4.4 Identifiers, education, previous employment, family, nominees, bank**

| Method | Path | Purpose | Permission | Request | Response | Key validations / errors |
|---|---|---|---|---|---|---|
| GET | Org/employees/{id}/identifiers | Masked identifiers | identity.view_masked | — | type, masked value, dates, verification | |
| PUT | Org/employees/{id}/identifiers/{type} | Add/replace identifier | identity.edit | value, issue/expiry, issuing_country, duplicate_override_reason, acknowledged_warnings | Masked identifier | V-*; FIELD_NOT_COLLECTED; DUPLICATE_IDENTIFIER |
| POST | Org/employees/{id}/identifiers/{type}/reveal | Full value | identity.reveal | reason | value (no-store) | Audited; rate-limited |
| POST | Org/identifier-duplicate-check | Pre-check value | employee.create | identifier_type, value | matches (permission-filtered) | Never returns plaintext |
| GET / POST | Org/employees/{id}/education | List / add | employee.view_personal / employee.edit | §12.1 | Rows | Result rules |
| PATCH / POST archive | Org/employees/{id}/education/{eid} | Edit / archive | employee.edit | §12.1 | Row | |
| GET / POST / PATCH / archive | Org/employees/{id}/previous-employment[/{pid}] | Manage previous jobs | employee.view_personal / employee.edit (bgv fields: bgv.view) | §12.2 incl. payslip_document_ids array (stored in link table) | Rows with overlap_flag | End ≥ start; documents same employee |
| POST | Org/employees/{id}/previous-employment/{pid}/overlap-review | Accept overlap | employee.edit | decision | Row | |
| GET / POST / PATCH / archive | Org/employees/{id}/family-members[/{fid}] | Manage family | employee.view_personal / employee.edit | §13.1 | Rows | |
| GET | Org/employees/{id}/nominations?type=&as_of= | Nomination sets | employee.view_personal | | Sets with totals | |
| PUT | Org/employees/{id}/nominations/{type} | Replace set atomically | employee.edit | effective_from, nomination_label, nominees[] | New set | NOMINEE_SHARE_TOTAL_INVALID; guardian rules |
| GET / POST | Org/employees/{id}/bank-accounts | List (masked) / add | bank.view_masked / bank.edit | §14.1 | Masked rows | V-IFSC, V-BANKACC; W-BANK-DUP |
| PATCH | Org/employees/{id}/bank-accounts/{bid} | Edit non-key fields / close (effective_to) | bank.edit | branch_name, account_type, effective_to | Row | Number/IFSC immutable |
| POST | …/bank-accounts/{bid}/set-primary | Set primary | bank.edit | effective_from | Rows | Exclusion constraint |
| POST | …/bank-accounts/{bid}/verify | Verify | bank.verify | status, verification_date, remarks, bank_proof_document_id | Row | VERIFIED requires proof |
| POST | …/bank-accounts/{bid}/reveal | Full number | bank.reveal | reason | account_number (no-store) | Audited |

**24.4.5 Documents**

| Method | Path | Purpose | Permission | Request | Response | Key validations / errors |
|---|---|---|---|---|---|---|
| GET / POST / PATCH | Org/document-categories[/{id}] | Manage categories | employee.list / master.manage | §15.2 | Categories | Code immutable |
| POST | Org/employees/{id}/documents | Initiate upload | document.upload | category_id, document_type, title, description, original_file_name, declared mime_type, file_size_bytes, document_number, issue/expiry dates, issued_by; Idempotency-Key | Document (PENDING_UPLOAD) + upload_url, upload_expires_at | Category active; type allowlist; size |
| POST | Org/employees/{id}/documents/{docId}/complete | Finalize | document.upload | — | Document (AVAILABLE / QUARANTINED / FAILED) | 415/413; scan result |
| GET | Org/employees/{id}/documents | List | document.download or identity.view_masked or employee.edit | category, verification_status, expiry_state, include_archived, include_versions | Metadata (restricted categories filtered) | |
| GET | Org/employees/{id}/documents/{docId} | Metadata + version chain | as list | | Document | 404 cross-org |
| POST | …/documents/{docId}/download-url | Time-limited URL | document.download (+ download_restricted) | — | url, expires_at (5 min) | DOCUMENT_ACCESS_DENIED; AVAILABLE only |
| POST | …/documents/{docId}/versions | Replace | document.upload | as initiate | New version (PENDING_UPLOAD) + upload_url | Category allows; chain head only |
| POST | …/documents/{docId}/verify | Verify / reject | document.verify | decision, rejection_reason | Document | Self-verification rule |
| POST | …/documents/{docId}/archive | Archive | document.archive | reason | Document | |
| GET | Org/documents/expiring?within_days= | Expiry list | document.download | within_days ≤ 365 | Documents | No notifications |

**24.4.6 Salary**

| Method | Path | Purpose | Permission | Request | Response | Key validations / errors |
|---|---|---|---|---|---|---|
| GET / POST / PATCH | Org/salary-components[/{id}] | Component catalog | salary.view / salary.template_manage | §16.3 [CHG 2026-11-02: `display_name`, `component_type` on create; `display_name` on patch; responses add `display_name`, `component_type`, `is_system`] | Components | value_type and component_type immutable; Basic (`is_system`) editable by name only; `BASIC` code reserved |
| GET | Org/salary-components/{id}/usage | [NEW] Template-version and assignment counts | salary.view / salary.template_manage | — | template_version_count, assignment_count | 404 unknown component |
| POST | Org/salary-components/{id}/deactivate | Deactivate | salary.template_manage | reason | Component | 422 for the system Basic component |
| GET / POST | Org/salary-templates | List / create template with draft v1 | salary.view / salary.template_manage | §16.4 + §16.5 + mappings | Template + version | Unique code; mapping rules |
| GET / PATCH | Org/salary-templates/{tid} | Read / edit header | salary.view / salary.template_manage | name, description | Template | |
| POST | Org/salary-templates/{tid}/versions | New draft version (copy latest) | salary.template_manage | — | Version (DRAFT) | One open draft per template |
| PATCH | Org/salary-templates/{tid}/versions/{vid} | Edit draft | salary.template_manage | currency, effective_from, change_note | Version | DRAFT only |
| PUT | …/versions/{vid}/components | Replace draft mappings | salary.template_manage | mappings[] (§16.6) | Mappings | Duplicates; type/currency checks |
| GET | Org/salary-templates/{tid}/versions | [NEW] List all versions (mappings omitted) | salary.view / salary.template_manage | — | Version[] | 404 unknown template |
| GET | Org/salary-templates/{tid}/versions/{vid} | [NEW] Read one version with mappings; `ETag` | salary.view / salary.template_manage | — | Version | 404 if the version belongs to another template |
| POST | …/versions/{vid}/publish | Publish; [CHG] optional body saves unsaved edits first | salary.template_manage | optional: currency, effective_from, change_note, mappings[] | Version (ACTIVE) | Non-overlap; effective_from rules; edits roll back on failure |
| POST | Org/salary-templates/{tid}/archive | Archive template | salary.template_manage | reason | Template | |
| GET | Org/employees/{id}/salary-assignments | Timeline | salary.view | as_of, status[], from, to, include_cancelled | Assignments with components | |
| GET | Org/employees/{id}/salary-assignments/current | Current as of today | salary.view | | Assignment or null | Date predicate |
| POST | Org/employees/{id}/salary-assignments | Create draft (optionally confirm) | salary.revision_create (+ confirm/backdate as needed) | template_id, effective_from, effective_
| POST | Org/employees/{id}/salary-assignments | Create a draft, or create and confirm in one call | salary.revision_create (+ salary.revision_confirm when `confirm=true`; + salary.revision_backdate when effective_from < today) | template_id, effective_from, effective_to (O), revision_reason, revision_remarks, revision_letter_document_id, backdate_reason, components[] {component_id, or custom code/name/category/value_type/amount_period; value; override_reason}, confirm (boolean); Idempotency-Key | Assignment + list of boundary adjustments applied | TEMPLATE_ARCHIVED; TEMPLATE_VERSION_NOT_EFFECTIVE; COMPONENT_NOT_OVERRIDABLE; SALARY_ASSIGNMENT_OVERLAP; BACKDATE_NOT_ALLOWED; SALARY_AFTER_EMPLOYMENT_END; APPROVAL_REQUIRED |
| POST | Org/employees/{id}/salary-assignments/preview-timeline | Dry run of a create/confirm | salary.revision_create | Same as create | Proposed timeline with boundary adjustments and validation errors | No writes; no locks |
| PATCH | Org/employees/{id}/salary-assignments/{aid} | Edit a draft | salary.revision_create | Draft fields and components; If-Match | Assignment | DRAFT only; otherwise SALARY_NOT_EDITABLE |
| DELETE | Org/employees/{id}/salary-assignments/{aid} | Delete a never-submitted draft | salary.revision_create | — | 204 | DRAFT and approval_state NOT_REQUIRED only |
| POST | …/salary-assignments/{aid}/submit | Submit for approval | salary.revision_create | — | Assignment (approval PENDING) | Only when approval is required |
| POST | …/salary-assignments/{aid}/approve | Approve (optionally confirm) | salary.revision_approve | confirm (boolean) | Assignment | SELF_APPROVAL_NOT_ALLOWED |
| POST | …/salary-assignments/{aid}/reject | Reject | salary.revision_approve | rejection_reason | Assignment | Reason 10–1,000 chars |
| POST | …/salary-assignments/{aid}/confirm | Confirm into the timeline (O1) | salary.revision_confirm | backdate_reason (C) | Assignment + boundary adjustments | §18.3 errors |
| POST | …/salary-assignments/{aid}/cancel | Cancel a scheduled revision (O2) | salary.revision_cancel | cancellation_reason | Assignment + boundary adjustments | SCHEDULED only; else SALARY_NOT_CANCELLABLE |
| POST | …/salary-assignments/{aid}/replace | Edit a scheduled revision (cancel + confirm, atomic) | salary.revision_cancel + salary.revision_confirm | Full new assignment body | New assignment | SCHEDULED only |
| POST | …/salary-assignments/{aid}/correct | Correct values of an ACTIVE/EXPIRED assignment (O3) | salary.revision_correct | components[], reason (≥ 20 chars) | New assignment A′ | ACTIVE/EXPIRED only; else SALARY_NOT_CORRECTABLE |
| GET | Org/employees/{id}/salary-history | Revision events | salary.view | from, to, event_type[], cursor | salary_revision_history rows | — |
| GET | Org/employees/{id}/salary-assignments/compare?left={aid}&right={aid} | Compare two assignments | salary.view | — | Per component: code, name, left value, right value, change_type, override flags | Same employee; no arithmetic (§4.3) |

**24.4.7 Employee ID rules**

| Method | Path | Purpose | Permission | Request | Response | Key validations / errors |
|---|---|---|---|---|---|---|
| GET | Org/employee-id-rules | List rules | idrule.manage or employee.create | department_id, status[], as_of | Rules with derived flags and last_issued_sequence | — |
| POST | Org/employee-id-rules | Create a DRAFT rule | idrule.manage | §19.1 fields | Rule (DRAFT) | Pattern and token validation |
| GET / PATCH | Org/employee-id-rules/{rid} | Read / edit | idrule.manage | §19.1 fields; If-Match | Rule | ID_RULE_IMMUTABLE when §19.3 applies |
| POST | Org/employee-id-rules/{rid}/publish | Publish | idrule.manage | — | Rule (SCHEDULED/ACTIVE/EXPIRED) | ID_RULE_CONFLICT; department active |
| POST | Org/employee-id-rules/{rid}/archive | Archive | idrule.manage | reason | Rule | Warning if this leaves a department/date range with no applicable rule |
| POST | Org/employee-id-rules/preview | Preview the next code | idrule.manage or employee.create | department_id, basis_date (or joining_date) | selected_rule, indicative_code, indicative = true | NO_APPLICABLE_ID_RULE; never consumes sequences |
| GET | Org/employee-id-sequences | View counters | idrule.manage | — | Sequence rows | Read-only |
| GET | Org/employee-code-registry | Look up issued/retired codes | idrule.manage | code, employee_id, status | Registry rows | — |

**24.4.8 Users, access and audit**

| Method | Path | Purpose | Permission | Request | Response | Key validations / errors |
|---|---|---|---|---|---|---|
| GET / POST | /users | List / create users | user.manage | email, display_name, employee_id (O), auth_provider | User | Unique email; OA sees only users with grants in their organizations |
| PATCH | /users/{uid} | Edit user | user.manage | display_name, employee_id | User | employee_id must be in an organization the editor manages |
| POST | /users/{uid}/deactivate | Deactivate user | user.manage | reason | User | Revokes sessions and refresh tokens immediately |
| GET / POST | /users/{uid}/org-access | List / grant roles per organization | user.manage | organization_id, role_code, expires_at (O) | Grants | OA cannot grant SSA or other organizations |
| DELETE | /users/{uid}/org-access/{grantId} | Revoke grant | user.manage | reason | 204 | Effective within 60 seconds |
| POST | /break-glass | Time-bound SSA access | org.create (SSA) | organization_id, reason (≥ 20), duration_minutes (≤ 240) | Grant | Audited in the organization's log |
| GET | /roles · /permissions | Role and permission catalog | user.manage | — | Lists | — |
| PUT | /roles/{rid}/permissions | Change role permissions | SSA only | permission_codes[] | Role | System roles: changes audited; SSA role immutable |
| GET | Org/audit-logs | Organization audit log | audit.view | from, to, actor, action[], entity_type, entity_id, employee_id, cursor | Audit records (salary events require salary.view) | Max range 366 days per query |
| GET | Org/employees/{id}/audit | Per-employee audit | audit.view | from, to, action[] | Audit records | — |

### 24.5 Additional Error Codes

| HTTP | Code | When |
|---|---|---|
| 409 | IDEMPOTENCY_KEY_REUSED | Same Idempotency-Key reused with a different payload |
| 422 | SALARY_NOT_EDITABLE | Editing a non-draft assignment |
| 422 | SALARY_NOT_CANCELLABLE | Cancelling an ACTIVE/EXPIRED/CANCELLED assignment |
| 422 | SALARY_NOT_CORRECTABLE | Correcting a DRAFT/SCHEDULED/CANCELLED assignment |
| 422 | APPROVAL_REQUIRED | Confirming without approval when approval is required |
| 403 | SELF_APPROVAL_NOT_ALLOWED | Approver is creator or submitter |
| 422 | ID_RULE_IMMUTABLE | Editing locked fields of a used/started rule |
| 409 | DOCUMENT_NOT_AVAILABLE | Download of a document not in AVAILABLE state |
| 422 | TEMPLATE_DRAFT_EXISTS | Creating a second open draft version for a template |

### 24.6 Example Payloads

**Create employee (request):**
```json
{
  "first_name": "Rajesh",
  "middle_name": "Kumar",
  "last_name": "Patel",
  "date_of_birth": "1992-08-15",
  "gender": "MALE",
  "nationality": "IN",
  "personal_mobile": "9876543210",
  "joining_date": "2026-11-02",
  "department_id": "0192c3a4-0000-7000-8000-000000000101",
  "designation_id": "0192c3a4-0000-7000-8000-000000000201",
  "branch_id": "0192c3a4-0000-7000-8000-000000000301",
  "employment_type_id": "0192c3a4-0000-7000-8000-000000000401",
  "work_mode": "HYBRID",
  "current_address_line_1": "22, Shanti Nagar Society",
  "current_city": "Gandhinagar",
  "current_state": "Gujarat",
  "current_postal_code": "382021",
  "current_country": "IN",
  "permanent_address_same_as_current": true,
  "acknowledged_warnings": []
}
```

**Create employee (response, 201):**
```json
{
  "id": "0192c3a4-7b1e-7cc0-9f00-1a2b3c4d5e6f",
  "organization_id": "0192c3a4-0000-7000-8000-000000000001",
  "employee_code": "ABC-IT-0038",
  "status": "DRAFT",
  "personal_mobile": "+919876543210",
  "id_generation": { "rule_code": "IT-2026", "source": "GENERATED" },
  "warnings": [],
  "row_version": 1
}
```

**Confirm a future salary revision (request):**
```json
{
  "template_id": "0192c3a4-0000-7000-8000-000000000501",
  "effective_from": "2027-04-01",
  "revision_reason": "ANNUAL_REVISION",
  "components": [
    { "component_id": "0192c3a4-0000-7000-8000-000000000601", "value_amount": 1800000.00 },
    { "component_id": "0192c3a4-0000-7000-8000-000000000602", "value_amount": 720000.00,
      "override_reason": "Approved by compensation committee" }
  ],
  "confirm": true
}
```

**Confirm a future salary revision (response, 201, abridged):**
```json
{
  "assignment": { "id": "…", "assignment_status": "SCHEDULED",
                  "effective_from": "2027-04-01", "effective_to": null,
                  "effective_to_source": "NONE" },
  "boundary_adjustments": [
    { "assignment_id": "…", "effective_to_before": null,
      "effective_to_after": "2027-04-01", "source": "SUCCESSOR" }
  ]
}
```

### 24.7 Transaction Boundaries and Background Jobs

| Operation | Single transaction contains |
|---|---|
| Employee create (ON_CREATE) | Employee insert, addresses, identifiers, job-history row, status-history row, sequence update, registry insert, audit |
| Activation (ON_ACTIVATION) | Status change, code generation (as above), audit |
| Status change with end date | Employee update, status history, salary boundary adjustment (§18.6), audit |
| Salary O1/O2/O3/replace | Employee row lock, assignment rows, components, revision events, audit |
| Nomination set replacement | End-dating old set, inserting new set, validation, audit |
| Set primary bank account | Previous primary update, new primary update, audit |
| Document complete | Metadata update, version chain update, audit (file scanning happens before the transaction) |
| Code reassignment | Registry retire/insert, employee update, audit |

No transaction may call external services (storage, scanner, KMS network calls are permitted only before the transaction begins, except local cryptographic operations with cached data keys). Clients SHOULD retry `409 CONCURRENT_MODIFICATION` up to 3 times with jittered backoff.

| Job | Schedule | Behaviour |
|---|---|---|
| Salary status refresh | 00:05 organization local time | Updates stored statuses; writes ACTIVATED/EXPIRED events; idempotent |
| ID rule status refresh | 00:05 organization local time | Updates SCHEDULED/ACTIVE/EXPIRED labels; idempotent |
| Pending upload cleanup | Hourly | Marks PENDING_UPLOAD > 24 h as FAILED; purges objects |
| Quarantine purge | Hourly | Deletes quarantined objects older than 24 h |
| Retention purge | Daily 02:00 IST | Executes approved retention rules (§25.6) with tombstone audit |
| Integrity checker | Nightly | Detects orphan rows, overlap violations (MySQL), cross-organization mismatches; reports as P1 defects |

---

## 25. Data Security, Privacy and Retention

### 25.1 Legal Framework (for review, not legal advice)
The following frameworks are relevant to the design. Their applicability, commencement status and the organization's obligations must be confirmed by qualified Indian counsel; implementing these controls does not by itself establish legal compliance.

| Framework | Design relevance |
|---|---|
| Digital Personal Data Protection Act, 2023 | Each organization is expected to act as a data fiduciary for its employees' personal data. The Act provides for processing on the basis of consent or certain legitimate uses, including employment-related purposes, so consent is not assumed to be the only basis. Obligations include notice, purpose limitation, reasonable security safeguards, breach intimation, erasure when the purpose is served (subject to legal retention), and data principal rights. |
| Digital Personal Data Protection Rules, 2025 | Notified in November 2025 with phased commencement. The current commencement status of each obligation must be confirmed at go-live. |
| Aadhaar (Targeted Delivery of Financial and Other Subsidies, Benefits and Services) Act, 2016 and UIDAI regulations | Restrictions on collection, storage and display of Aadhaar numbers; drives the NOT_COLLECTED default, masking and restricted reveal. |
| Information Technology Act, 2000 (Section 43A) and SPDI Rules, 2011 | Security practices for sensitive personal data; relationship with the DPDP regime to be confirmed by counsel. |
| Labour codes, EPF/ESIC schemes, income-tax and state laws | Determine which identifiers and records must be kept and for how long; the system stores references only and does not decide statutory applicability. |
| Rights of Persons with Disabilities Act, 2016 | Informs disability categories and the sensitivity of disability data. |

### 25.2 Data Minimization and Purpose Limitation
- Collection defaults are minimal: Aadhaar, voter ID, driving licence, religion, social category and disability data are **not collected** unless an OA enables them with a recorded purpose statement (§7.5, §11.3).
- Each field group is mapped to a purpose in a **processing register** maintained by the organization (outside the application, referenced in go-live checklist). The application stores purpose statements for optional sensitive categories.
- Fields disabled by policy are rejected by the API (`FIELD_NOT_COLLECTED`), not merely hidden.

### 25.3 Notice and Consent Records (`employee_privacy_notices`)

| Field | Data Type | Required? | Validation / Constraints | Example | Notes |
|---|---|---|---|---|---|
| id | UUID | SYS | PK | | |
| organization_id, employee_id | UUID | M | Composite FK employees | | |
| notice_version | VARCHAR(20) | M | Matches an organization-published notice version | 2026.1 | Notice text stored as an organization file |
| basis | ENUM | M | NOTICE_ONLY, CONSENT | NOTICE_ONLY | Determined by legal review per data category |
| consent_scope | VARCHAR(200) | C | Required when basis = CONSENT | Optional blood group and photograph | |
| delivered_at | TIMESTAMP | M | | | |
| acknowledged_at | TIMESTAMP | O | ≥ delivered_at | | |
| withdrawn_at | TIMESTAMP | O | Only when basis = CONSENT | | Withdrawal triggers review of consent-based fields; does not auto-delete records required by law |
| method | ENUM | M | ELECTRONIC, PAPER_UPLOADED | ELECTRONIC | PAPER_UPLOADED links a document |

### 25.4 Encryption
- In transit: TLS 1.2+ (1.3 preferred) for all external and internal service traffic; HSTS; secure cookies (HttpOnly, Secure, SameSite=Lax).
- At rest: encrypted database storage and backups; object storage with KMS-managed server-side encryption; application-level encryption for all [E] fields (§23.7).
- Keys in a managed KMS/HSM; access to decrypt limited to the application service identity; key usage logged.

### 25.5 Access Control, Isolation and Revocation
- RBAC per §5; organization isolation per §6 and §23.6; field-level filtering and masking per §11.6 and §14.2.
- MFA for privileged roles (NFR-12).
- Quarterly access review report per organization: users, roles, last login, break-glass events [ASSUMPTION].
- Revocation: user deactivation revokes refresh tokens immediately; access tokens expire within 15 minutes; permission caches expire within 60 seconds; pre-signed document URLs expire within 5 minutes.
- Linked-employee separation: when an employee linked to a user becomes separated, the user's grants in that organization are flagged for review and auto-revoked on employment_end_date + 1 day (configurable) [ASSUMPTION].

### 25.6 Retention and Deletion
All values below are **placeholders** that must be confirmed by counsel per organization; they are configurable.

| Data | Default retention (placeholder) | Action at end |
|---|---|---|
| Employee record and child records of separated employees | 8 years after employment_end_date | Anonymize personal fields; keep employee_code, organization, dates, status (tombstone) |
| Salary assignments and history | 8 years after employment_end_date | Delete component values; keep tombstone event |
| Documents | Per category `retention_months_after_separation`; default 96 months | Purge file and metadata; tombstone audit |
| Superseded document versions | Same as category | Purge |
| Discarded drafts | 90 days after archival | Hard delete (code stays RETIRED in registry) |
| Failed/quarantined uploads | 24 hours | Purge objects |
| Audit logs | 8 years | Delete beyond period |
| Application logs (no personal data by design) | 90 days | Delete |
| Backups | 35 days | Rolling deletion |

- Retention purges run only after an OA approves the purge batch listing affected records (two-person rule with HRA) [ASSUMPTION].
- Legal hold flag (`employees.legal_hold`, BOOLEAN, default false, HRA only) blocks purge and erasure for that record.

### 25.7 Data Principal Requests and Exports
- **Access/export:** HRA can generate an export of one employee's data (JSON + documents archive) via `POST Org/employees/{id}/data-export`, masked by default, unmasked only with reveal permissions; exports are encrypted with a one-time password delivered separately, expire after 72 hours, and are audited.
- **Correction:** handled by normal edit flows with audit.
- **Erasure:** `POST Org/employees/{id}/erasure-requests` records the request; HRA reviews against retention obligations and legal hold; approved erasure anonymizes personal fields and purges eligible documents; refused requests store the reason.
- **Bulk exports** of employee lists are limited to non-sensitive fields, require employee.list plus audit.view, and are audited. Sensitive identifiers and bank numbers are never included in bulk exports.

### 25.8 Items Requiring Legal Review
1. Lawful basis per data category (employment-related legitimate use vs consent) and notice content.
2. DPDP Rules commencement status and the applicable breach-intimation procedure and timelines.
3. Whether and how Aadhaar may be collected, stored and displayed by each organization.
4. Retention periods per data category and document category.
5. Handling of employees or interns under 18 (children's data provisions; minimum age defaults).
6. Copying personal data between group companies on inter-organization transfer.
7. Cross-organization duplicate detection within a group.
8. Scheme-specific nomination eligibility rules (EPF, gratuity, insurance).
9. Collection of religion, social category and disability data, and the documented purposes.
10. Hosting location and cross-border transfer of backups or support access.
11. Processor contracts with hosting, storage, scanning and email/SSO vendors.

### 25.9 Other Security Controls
- Secrets in a managed secrets store; none in source code, images or environment files committed to repositories.
- Application logs MUST NOT contain personal data, identifiers, salary values, tokens or document contents; request bodies are not logged.
- Secure development: OWASP ASVS Level 2 as baseline; dependency and container scanning on every build; penetration test before go-live and annually.
- Rate limiting on authentication, reveal and download endpoints.
- Upload security per §15.4–15.5; CSRF protection for cookie sessions; strict Content-Security-Policy in the UI.
- Backups encrypted, restoration tested quarterly (NFR-09); backup access restricted to infrastructure administrators with audit.
- Incident response runbook covering detection, containment, assessment of affected organizations and data principals, and notifications as determined by counsel.

---

## 26. Search, Filters, Sorting and Pagination

### 26.1 Search Scope
All searches are organization-scoped (`Org/employees`). A user with grants in several organizations searches one organization at a time; group-wide search is not provided in v1.

### 26.2 Search and Filter Parameters

| Parameter | Type | Behaviour | Permission |
|---|---|---|---|
| q | string (2–100 chars) | Matches employee_code (prefix, case-insensitive), full_name_normalized (trigram/prefix), official_email (prefix, case-insensitive) | employee.list |
| employee_code | string | Exact, case-insensitive | employee.list |
| department_id[] | UUID list (≤ 50) | Exact | employee.list |
| designation_id[] | UUID list | Exact | employee.list |
| branch_id[] | UUID list | Exact | employee.list |
| status[] | enum list | Default: all except archived drafts | employee.list |
| employment_type_id[] | UUID list | Exact | employee.list |
| joining_date_from / joining_date_to | DATE | Inclusive range; from ≤ to; span ≤ 50 years | employee.list |
| reporting_manager_employee_id | UUID | Direct reports; `include_indirect=true` for full chain (depth ≤ 20) | employee.list |
| official_email | string | Exact, case-insensitive | employee.list |
| grade_id[], business_unit_id[], cost_center_id[], work_location_id[], employee_category_id[], work_mode[] | lists | Exact | employee.list |
| include_archived | boolean | Default false | employee.archive |
| identifier_type + identifier_value | enum + string | Exact match via blind index only; no partial or wildcard search; types PAN, UAN, PF_MEMBER_ID, ESIC_IP, PASSPORT | identity.reveal (search is audited as `IDENTIFIER_SEARCHED`); Aadhaar and bank numbers are not searchable |

Department Managers receive only scoped employees regardless of filters (§5.4).

### 26.3 Normalization
Input is trimmed and NFC-normalized; whitespace collapsed; case-insensitive comparison; employee codes compared upper-case; emails lower-case. Wildcard characters (`%`, `_`, `*`) are escaped and treated as literals.

### 26.4 Sorting
Allowed `sort` values: `employee_code`, `display_name`, `joining_date`, `department_name`, `designation_name`, `status`, `created_at`; direction `asc` (default) or `desc`. A stable tie-breaker `id asc` is always appended. Default sort: `display_name asc`. Unknown sort fields → `422 VALIDATION_FAILED`.

### 26.5 Pagination
- Cursor-based: `limit` (default 25, maximum 100) and opaque `cursor` (encodes sort key + id, signed to prevent tampering, valid 24 hours).
- Response: `items[]`, `next_cursor` (null at end), `total_count` returned only when `with_total=true` and the filtered set ≤ 10,000 rows (otherwise `total_count_estimate`).
- Offset pagination is not supported.

### 26.6 Response Field Sets
List items return: id, employee_code, display_name, photograph thumbnail URL (time-limited), department, designation, branch, status, joining_date, official_email, work_mode, reporting manager (code and name). No personal contact, identifiers, bank or salary data appear in list responses for any role.

---

## 27. Error Handling and Edge Cases

| # | Scenario | Expected behaviour |
|---|---|---|
| E-01 | Two HR users create employees in the same sequence scope simultaneously | Sequence row lock serializes; both succeed with consecutive codes |
| E-02 | Employee create fails after the code was rendered (e.g., validation in a later step) | Transaction rolls back; sequence value not consumed |
| E-03 | Client times out and retries employee create | Same Idempotency-Key returns the original response; no duplicate record or code |
| E-04 | Generated code collides with a manual or retired code | Generator skips to the next value (§19.6) |
| E-05 | Joining date exactly on an ID rule transition date | New rule applies (inclusive start) |
| E-06 | No ID rule covers the department and date | 422 NO_APPLICABLE_ID_RULE; draft not saved when ON_CREATE |
| E-07 | Department of an employee changes after code issuance | Code unchanged |
| E-08 | Joining date changed after code issuance with year token | Code unchanged; HRA may reassign explicitly |
| E-09 | Backdated joiner whose basis date falls under an expired rule | Expired rule applies; its sequence advances |
| E-10 | Scheduled salary revision exists and employee resigns with an earlier end date | Setting end date fails with SALARY_AFTER_EMPLOYMENT_END until the revision is cancelled |
| E-11 | Resignation withdrawn after end-date boundary was applied | Boundary with source EMPLOYMENT_END reverts to open-ended (§18.6) |
| E-12 | Status-refresh job did not run at midnight | As-of queries still return the correct current assignment (date predicate) |
| E-13 | Template updated after employees were assigned | Existing assignments unchanged; new assignments use the version effective on their date |
| E-14 | Component renamed after use | Historical snapshots keep the old name; new assignments use the new name |
| E-15 | Assigning an archived template | 422 TEMPLATE_ARCHIVED |
| E-16 | Two revisions with the same effective_from | Second rejected with SALARY_ASSIGNMENT_OVERLAP |
| E-17 | Approval granted, then another revision confirmed for an overlapping period | Confirmation of the approved draft fails; draft returns to PENDING (§17.4) |
| E-18 | Manager assignment would create a cycle (A→B→C→A) | 422 REPORTING_CYCLE |
| E-19 | Manager is separated | Assignment allowed with warning W-MANAGER-SEPARATED |
| E-20 | Department deactivated while employees remain in it | Existing employees unaffected; no new assignments; warning shown on deactivation |
| E-21 | Document ID from another organization used in a request | 404 RESOURCE_NOT_FOUND; access attempt audited |
| E-22 | File renamed from .exe to .pdf | Magic-byte check fails; 415 FILE_TYPE_NOT_ALLOWED |
| E-23 | Malware detected | Document QUARANTINED; not downloadable; audit DOCUMENT_QUARANTINED |
| E-24 | Pre-signed download URL shared after expiry | Storage rejects; new URL requires re-authorization |
| E-25 | Nominee shares total 99.99 for PF | 422 NOMINEE_SHARE_TOTAL_INVALID showing computed total |
| E-26 | Nominee is a minor without guardian | 422 VALIDATION_FAILED on guardian fields |
| E-27 | Same PAN entered for a second active employee in the same organization | 409 DUPLICATE_IDENTIFIER; HRA may override with reason |
| E-28 | Same PAN for a rehire in the same organization | Warning W-DUP-REHIRE with option to link previous record |
| E-29 | Permanent address fields sent with same-as-current = true | 422 CONFLICTING_PERMANENT_ADDRESS |
| E-30 | Stale ETag on employee edit | 412 PRECONDITION_FAILED; client reloads |
| E-31 | Inter-organization transfer by user lacking target-organization access | 403 ORG_ACCESS_DENIED; no changes in either organization |
| E-32 | Organization deactivated while drafts exist | Drafts remain; activation blocked until organization reactivated |
| E-33 | Lookup value deactivated while in use | Existing records keep and display the value; new selections blocked |
| E-34 | Employee age below the configured minimum but above the absolute minimum | Saved as DRAFT with PENDING age exception; activation blocked until approved |
| E-35 | Very large sequence exceeding padding width | Full number rendered without truncation; if code length > 30, 422 VALIDATION_FAILED and OA alerted in the rule UI at 90% capacity |
| E-36 | Two bank accounts marked primary for overlapping dates (race) | Exclusion constraint / lock ensures one fails with 409 CONCURRENT_MODIFICATION |

---

## 28. Acceptance Criteria
Each criterion is written as Given / When / Then and MUST be covered by an automated test.

| ID | Scenario | Given | When | Then |
|---|---|---|---|---|
| AC-01 | Multiple organizations without data leakage | Organizations ABC and XYZ, each with employees; user U has HRA only in ABC | U calls list, detail, document, salary and audit endpoints for XYZ resources (by XYZ path and by ABC path with XYZ IDs) | XYZ path returns 403 ORG_ACCESS_DENIED; ABC path with XYZ IDs returns 404; no XYZ data appears in any response; attempts are audited |
| AC-02 | Independent ID sequences | ABC rule `{ORG}{SEP}{SEQ}` and XYZ rule `{ORG}{SEP}{SEQ}`, both starting at 1 | One employee is created in each organization | Codes ABC-0001 and XYZ-0001 are issued; each organization's sequence row shows 1 |
| AC-03 | Department from another organization rejected | Department D belongs to XYZ | HRA of ABC creates/edits an ABC employee with department_id = D | 422 INVALID_ORGANIZATION_REFERENCE; nothing saved; DB composite FK also rejects a direct insert |
| AC-04 | No duplicate employee codes within an organization | ABC-0005 exists | A manual code ABC-0005 or a reassignment to ABC-0005 is attempted | 409 DUPLICATE_EMPLOYEE_CODE; unique index rejects any direct insert |
| AC-05 | Create and reuse templates | Template STD-STAFF v1 published | Two employees are assigned STD-STAFF | Both assignments reference v1 and contain all required components with snapshots |
| AC-06 | Permitted overrides | BASIC mapping is overridable; PF_REF mapping is not | User sets BASIC to a non-default value with reason, and PF_REF to a non-default value | BASIC saved with is_overridden = true; PF_REF rejected with COMPONENT_NOT_OVERRIDABLE |
| AC-07 | Template change does not alter history | Employee E has a confirmed assignment from STD-STAFF v1 | v2 is published with changed defaults and a renamed component | E's assignment values, names and template_version_number are unchanged |
| AC-08 | Future revision does not change current salary | Today 2026-10-28; E has active A1 | Revision R effective 2027-04-01 is confirmed | Current salary (as of today) is A1 with unchanged values; A1.effective_to = 2027-04-01 (SUCCESSOR); R is SCHEDULED |
| AC-09 | Activation at the effective date | R scheduled for 2027-04-01 | Clock set to 2027-04-01 00:00 IST (before and after the refresh job) | As-of query returns R in both cases; after the job, R is ACTIVE and A1 EXPIRED with ACTIVATED/EXPIRED events |
| AC-10 | No overlapping confirmed assignments | E has R [2027-04-01, ∞) | A revision with explicit range [2027-01-01, 2027-06-01) is confirmed, and separately another revision starting 2027-04-01 | Both rejected with SALARY_ASSIGNMENT_OVERLAP; DB exclusion constraint rejects a direct overlapping insert |
| AC-11 | Cancel future revision without altering history | Timeline A0 (expired), A1 (active), R (scheduled) | R is cancelled | R is CANCELLED; A1 values unchanged and A1.effective_to restored to R's end; A0 untouched; events recorded |
| AC-12 | View and compare history | E has three confirmed assignments | User opens history and compares the first and third | Chronological list shown; comparison shows per-component values with change_type; no totals or deltas computed |
| AC-13 | Department-specific ID rules | ABC rule for IT `{ORG}{SEP}{DEPT}{SEP}{SEQ}` and an org-wide rule | An IT employee and an HR employee are created | IT employee gets ABC-IT-0001; HR employee gets a code from the org-wide rule |
| AC-14 | Organization-wide fallback | Only an org-wide rule is active | An employee in any department is created | Code generated from the org-wide rule; with no rule at all, 422 NO_APPLICABLE_ID_RULE |
| AC-15 | Scheduled rule change | Rule A [2026-01-01, 2027-04-01), Rule B [2027-04-01, ∞) | Employees join on 2027-03-31 and 2027-04-01 | First uses A, second uses B; preview for each date shows the correct rule without consuming sequences |
| AC-16 | Concurrency at a rule transition | 50 creations submitted concurrently for joining date 2027-04-01 under Rule B | All requests complete | 50 unique codes, contiguous sequence values 1–50, zero errors other than retryable 409 that succeed on retry |
| AC-17 | Existing codes not changed by rule updates | Employees issued under Rule A | Rule A is ended and Rule B published; department of one employee changes | All existing codes remain identical |
| AC-18 | Document lifecycle | Category PAN_PROOF requires verification | HRE uploads a PDF, RDR verifies, HRE uploads a replacement, HRA downloads | v1 VERIFIED then superseded; v2 PENDING; download URL expires in 5 minutes; all events audited |
| AC-19 | Cross-organization document denial | Document belongs to an XYZ employee | ABC user requests its metadata or download URL using the ID | 404 RESOURCE_NOT_FOUND; no URL issued; attempt audited |
| AC-20 | Salary, bank and identity restricted by permission | Users with RV, HRE and HRA roles | Each opens E's profile | RV sees no salary/bank/identity data; HRE sees masked identity and bank, no salary unless granted; HRA sees masked values and can reveal with reason (audited) |
| AC-21 | Audit preserved | Any create/update/status/salary/ID-rule/permission change | The change is committed | Audit record exists with actor, action, entity, organization, timestamp, changed fields, masked sensitive values and correlation ID; audit rows cannot be updated or deleted by the app role |
| AC-22 | Deactivate master without breaking history | Branch GNR01 referenced by employees and job history | GNR01 is deactivated | Existing employees still show GNR01 (Inactive badge); new assignments to GNR01 rejected with MASTER_INACTIVE; delete attempt rejected |
| AC-23 | Rehire and transfer keep previous records | E resigned from ABC | E is rehired in ABC, and separately E2 is transferred from ABC to XYZ | New records with new codes, linked via previous_employment_record_id; previous records unchanged except their own separation fields |
| AC-24 | Excluded modules absent | Full API and UI inventory | Reviewed and tested | No endpoints, screens or jobs exist for payroll, payslips, tax, statutory contributions, attendance, leave, recruitment, performance, training, expenses, assets, F&F or accounting; no stored or displayed computed totals |
| AC-25 | Status transitions enforced | E is RESIGNED | HRE attempts RESIGNED → ACTIVE; HRA attempts correction within and beyond the window | HRE gets 403; HRA succeeds within window (is_correction = true), fails beyond with STATUS_CORRECTION_WINDOW_EXPIRED |
| AC-26 | Nominee validation | PF requires full allocation | PF set with 60% + 30% is saved; then 60% + 40% with a minor lacking guardian; then with guardian | First rejected (total 90.00); second rejected (guardian); third accepted and previous set end-dated |
| AC-27 | Aadhaar protection | AADHAAR policy OPTIONAL | Aadhaar is saved and E's profile, list, audit and logs are inspected | Only XXXX XXXX + last 4 appears; ciphertext stored; reveal requires identity.reveal and is audited |
| AC-28 | Backdating controls | backdated_salary_max_days = 365 | HRE (no backdate permission) and HRA confirm revisions dated 30 days ago and 400 days ago | HRE: 403; HRA: 30-day succeeds with reason, 400-day fails with BACKDATE_NOT_ALLOWED |

---

## 29. Assumptions and Open Questions

### 29.1 Assumptions

| ID | Assumption |
|---|---|
| A-01 | Single deployment with a shared database and schema for all organizations. |
| A-02 | Default timezone Asia/Kolkata; days are calendar days. |
| A-03 | English UI in v1; data supports Unicode, including Indic scripts. |
| A-04 | An OIDC/SAML identity provider is available; local accounts are a fallback. |
| A-05 | A managed KMS/HSM and private object storage with pre-signed URLs are available. |
| A-06 | A malware scanning service is available for uploads. |
| A-07 | No integrations with payroll, HRMS, banking or government portals in v1. |
| A-08 | NFR targets in §3.2 marked [ASSUMPTION] are initial and require confirmation. |
| A-09 | Hosting and backups are in India pending Q-09. |
| A-10 | Grade rank: lower number = more junior. |
| A-11 | Override reasons are required for components that have template defaults. |
| A-12 | Retention values in §25.6 are placeholders. |

### 29.2 Open Questions

| ID | Question | Default until decided |
|---|---|---|
| Q-01 | Are group-level shared masters (e.g., common designations across group companies) needed? | Not supported; masters are per organization |
| Q-02 | Should reporting managers be allowed in a different organization of the same group? | Not allowed (composite FK) |
| Q-03 | Can employment_start_date differ from joining_date (e.g., recognized continuity of service)? | Must be equal |
| Q-04 | Should identifier requirement policies vary by employment type (e.g., consultants without UAN)? | Same policy for all types |
| Q-05 | Should rehires be able to reuse their previous employee code? | Never reused |
| Q-06 | Are future-dated job changes (scheduled transfers/promotions) required? | Not supported; effective_date ≤ today |
| Q-07 | May the comparison view show arithmetic differences between amounts? | Not shown (§4.3) |
| Q-08 | Must HRA be able to change effective_from of ACTIVE/EXPIRED assignments? | Not supported; use backdated revision or correction |
| Q-09 | Is India-only hosting and backup mandatory? | Yes [ASSUMPTION] |
| Q-10 | Final retention periods per data and document category | Placeholders in §25.6 |
| Q-11 | Scheme-specific nomination eligibility rules (EPF, gratuity, insurance) | Only share, minor and DOB validation |
| Q-12 | Will any organization collect Aadhaar, and under what documented purpose? | NOT_COLLECTED |
| Q-13 | Minimum age defaults and treatment of under-18 apprentices/interns | Minimum 18, absolute 14, exception approval required |
| Q-14 | Should component values be validated against Annual CTC? | No validation (would require calculation) |
| Q-15 | Is multi-level salary approval needed? | Single-level maker-checker |
| Q-16 | Source and refresh frequency of an IFSC reference dataset | Manual bank name entry |
| Q-17 | Malware scanner vendor and PDF active-content policy | Quarantine on detection |
| Q-18 | Audit every document download for non-restricted categories at scale? | Yes (configurable) |
| Q-19 | Is employee self-view in scope for v1? | Disabled by default |
| Q-20 | Should official email be restricted to organization-configured domains? | No domain restriction |
| Q-21 | Which custom statuses to seed (e.g., TRANSFERRED, DECEASED)? | None seeded; OA defines |
| Q-22 | Should HR Executives see salary by default? | No (configurable) |
| Q-23 | Required depth of the DM indirect-reportee scope | 20 levels |

---

## Appendix A. Completeness Checklist

| Requirement area (from brief) | Covered in |
|---|---|
| Product scope and exclusions | §1.3, §4 |
| Organization fields, conditional identifiers, registrations | §7.2–§7.4 |
| Branches and constraints | §8 |
| Organization-specific masters | §9 |
| Employee statuses, transitions, required dates | §10.2–§10.3 |
| Basic/personal, contact, identity, employment fields and constraints | §10.4–§10.7, §11 |
| Education and previous employment | §12 |
| Family and nominees | §13 |
| Bank details | §14 |
| Document repository | §15 |
| Salary templates, components, mappings | §16 |
| Salary assignments, overrides, history, comparison | §17 |
| Effective-dating model and transactions | §18 |
| Employee ID rules, selection, sequences, concurrency | §19 |
| Roles and permissions | §5 |
| Database entities, relationships, schema, constraints | §22–§23 |
| APIs and error responses | §24 |
| Search and pagination | §26 |
| Audit | §20 |
| Security, privacy, retention, legal review | §25 |
| Field specification format and legend | §1.4–§1.5 and all field tables |
| Non-functional requirements | §3.2 |
| Acceptance criteria (24 required + extras) | §28 |
| Assumptions and open questions | §29 |