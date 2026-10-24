# Keka-style HR & Payroll Suite — Complete Build Contract

**Companion file:** `keka_schema.csv` (data model — 50+ tables, all fields, types, constraints)
**Audience:** UI engineers, AI coding subagents, QA, tech leads
**Purpose:** This file + `keka_schema.csv` = everything needed to build the product. No page, dialog, state, or field should be ambiguous after reading both.

---

## 0. HOW TO USE THIS CONTRACT

| Role | Start here |
|---|---|
| Tech lead | §1 Architecture, §2 Conventions, §11 Build order |
| UI engineer | §3 App shell, §4 Primitives, §5 Page specs |
| Subagent (page build) | §5 Page spec → §6 Dialog catalog → §7 State matrix → §12 Checklist |
| QA | §5 Page specs + §8 Acceptance criteria |
| Data engineer | `keka_schema.csv` + §9 API contract |

**Contract rule:** If this file and `keka_schema.csv` disagree, the schema wins for data shape and this file wins for UI/UX. Raise a ticket.

---

## 1. ARCHITECTURE OVERVIEW

### 1.1 System shape

```
┌─────────────────────────────────────────────────────────────────┐
│  Web SPA (React/Next)  │  Mobile (iOS/Android)  │  Admin CLI    │
└────────────┬───────────────────────┬────────────────────┬───────┘
             │                       │                    │
             ▼                       ▼                    ▼
      ┌─────────────────────────────────────────────────────┐
      │              API Gateway (REST + Webhooks)          │
      │         Auth: OAuth2 / SSO / 2FA / API keys         │
      └──────────────────────────┬──────────────────────────┘
                                 │
     ┌───────────────────────────┼───────────────────────────┐
     ▼                           ▼                           ▼
┌──────────┐             ┌──────────────┐            ┌──────────────┐
│ Core HR  │             │   Payroll    │            │  Compliance  │
│ Service  │             │   Engine     │            │   Service    │
└────┬─────┘             └──────┬───────┘            └──────┬───────┘
     │                          │                           │
     └──────────────┬───────────┴───────────────┬───────────┘
                    ▼                           ▼
          ┌──────────────────┐         ┌──────────────────┐
          │  Primary DB      │         │  Audit / Events  │
          │  (Postgres)      │         │  (immutable)     │
          └──────────────────┘         └──────────────────┘
```

### 1.2 Tech assumptions (adjust if your stack differs)

- **Frontend:** React + TypeScript, TanStack Query for server state, Zustand/Redux for UI state, React Hook Form + Zod for forms.
- **Design system:** tokens-first (color, spacing, radius, shadow, typography); components built on Radix primitives.
- **Backend:** REST + JSON, OpenAPI-documented. Async jobs (payroll, bulk import) via queue.
- **Auth:** OAuth2 + SSO (Okta, Azure AD, JumpCloud, OneLogin), 2FA, IP allowlist.
- **Storage:** Documents in S3-compatible object store; DB stores paths + metadata only.
- **Multi-tenancy:** Every table has `org_id`. Row-level security enforced at DB.

### 1.3 Environments

`local` → `staging` → `production`. Feature flags per tenant. Sandbox tenants for demo/training.

---

## 2. GLOBAL CONVENTIONS (apply to every page)

### 2.1 Layout shell

Every authenticated page renders inside the **App Shell**:

```
+--------------------------------------------------------------------------------+
| TopBar: [Logo] [Global Search] [Tenant Switcher] [Notifications] [Avatar Menu] |
+--------------------------------------------------------------------------------+
| SideNav  |  Breadcrumb                                                         |
| (fixed)  |  Page Title + Primary Action(s)                                     |
|          |  Filter Bar (optional)                                              |
|          |  Content Area (table / form / detail)                               |
|          |  Pagination / Footer Actions                                        |
+--------------------------------------------------------------------------------+
| Toast Stack (bottom-right)  |  Command Palette (Cmd/Ctrl + K)                 |
+--------------------------------------------------------------------------------+
```

- **SideNav** collapsible; state persisted in `localStorage`.
- **Breadcrumb** derived from route; last crumb = current page.
- **Page Title** is `<h1>`. Primary action right-aligned on desktop, stacked on mobile.
- **Command Palette** (Cmd/Ctrl+K) lists all pages + quick actions.

### 2.2 UI primitives (build once, reuse everywhere)

| Primitive | Purpose | Notes |
|---|---|---|
| `<DataTable>` | List pages | Column config, sort, filter, row-select, bulk actions, pagination, column visibility, sticky header, CSV export |
| `<FilterBar>` | List pages | Debounced search + filter chips + saved views |
| `<FormSection>` | Create/Edit | Title + description + grid of fields |
| `<Field>` wrapper | All forms | Label, required asterisk, help text, error text, tooltip |
| `<LookupSelect>` | FK fields | Async search, chip display, "Create new" if permitted |
| `<MoneyInput>` | Currency fields | Locale-aware, currency prefix, thousand separators |
| `<DatePicker>` | Date fields | Timezone-aware, business-day warnings |
| `<PermissionGate>` | RBAC | Hide/disable children by permission |
| `<AuditDrawer>` | Detail/Edit | Right-side change history drawer |
| `<ConfirmDialog>` | Destructive actions | Typed-confirm for high-risk |
| `<ApprovalTimeline>` | Approval flows | Vertical timeline of approvers |
| `<EmptyState>` | Empty lists/forms | Illustration + headline + CTA |
| `<SkeletonTable>` / `<SkeletonForm>` / `<SkeletonDetail>` | Loading | Row/shape skeletons |
| `<ErrorState>` | Error | Retry + error code + support link |
| `<Drawer>` | Secondary flows | 480px right panel |
| `<Wizard>` | Multi-step | Stepper header, back/next, save-draft |
| `<FileUpload>` | Documents/receipts | Drag-drop, per-file progress, OCR hook |
| `<CurrencyDisplay>` | Money rendering | Base + secondary, tooltip with rate/date |
| `<StatusPill>` | Statuses | draft=gray, pending=amber, approved=green, rejected=red, paid=blue |
| `<BulkActionBar>` | List pages | Appears on selection; count + actions |
| `<Inbox>` | Global | Aggregated approvals |
| `<CommentThread>` | Detail | Threaded comments + mentions |

### 2.3 Global states (every page must implement)

| State | Component | Trigger |
|---|---|---|
| Loading | `<SkeletonTable>` / `<SkeletonForm>` / `<SkeletonDetail>` | Initial fetch |
| Empty | `<EmptyState>` with module copy + CTA | Zero records |
| Error | `<ErrorState>` with retry + code + support | Fetch/mutation failure |
| Permission-denied | Full-page state + "Request access" CTA | RBAC block |
| Offline | Banner "You are offline. Changes won't save." | Network loss |
| Partial-error | Inline row error + retry per row | Bulk ops |

### 2.4 Toasts & feedback

| Variant | Color | Auto-dismiss | Actions |
|---|---|---|---|
| Success | Green | 4s | Undo (if reversible) |
| Error | Red | manual | Copy error, Retry |
| Warning | Amber | 6s | Dismiss |
| Info | Blue | 6s | Dismiss |

### 2.5 Keyboard shortcuts (global)

| Shortcut | Action |
|---|---|
| `Cmd/Ctrl + K` | Command palette |
| `g` then `e` | Go to Employees |
| `g` then `p` | Go to Payroll |
| `c` then `e` | Create Employee |
| `Esc` | Close active drawer/modal |
| `?` | Shortcut cheatsheet |
| `j` / `k` | Next / prev row (in tables) |
| `x` | Select row |
| `e` | Edit current row |

### 2.6 Accessibility (WCAG 2.1 AA)

- All interactive elements keyboard-reachable.
- Focus trap in dialogs/drawers.
- `aria-live="polite"` for toasts and validation summaries.
- Contrast ≥ 4.5:1. Never rely on color alone.
- Icon-only buttons have `aria-label`.
- Form errors linked via `aria-describedby`.

### 2.7 Mobile behavior

- SideNav → bottom tab bar (max 5) + "More" drawer.
- Tables → stacked cards (3–4 key fields + overflow menu).
- Filters → bottom sheet.
- Primary action → FAB.

### 2.8 Internationalization

- Dates/numbers/currency via `Intl`.
- All strings externalized.
- RTL-ready layout (logical properties).

### 2.9 Feature flags (plan tiers)

- Foundation / Strength / Growth + add-ons.
- Locked features render with lock icon + "Upgrade" tooltip.
- Flag check on both client and server.

### 2.10 Audit logging

Every mutation to these entities writes an `audit_logs` row:

`employees`, `employee_salaries`, `salary_revisions`, `payroll_runs`, `payslips`, `employee_bank_details`, `employee_statutory_details`, `tax_declarations`, `expense_claims`, `loans`, `legal_entities`, `pay_groups`, `salary_structures`, `salary_components`, `roles`, `workflows`, `currency_rates`, `display_currency_preferences`.

Format: `(org_id, user_id, entity_type, entity_id, action, old_values JSONB, new_values JSONB, ip, created_at)`.

---

## 3. APP SHELL DETAIL

### 3.1 TopBar

| Element | Behavior |
|---|---|
| Logo | Click → `/dashboard` |
| Global Search | Opens overlay; grouped results (Employees, Documents, Pages, Reports); debounced 300ms |
| Tenant Switcher | Multi-org users only; dropdown |
| Notifications | Bell with badge; dropdown panel of latest 20; "View all" → `/inbox` |
| Avatar Menu | Profile, Preferences (incl. currency), Theme, Logout, Switch to mobile app |

### 3.2 SideNav (role-aware)

Groups (collapsed by default, expand active group):

1. **Home** — Dashboard, Inbox
2. **People** — Employees, Departments, Designations, Locations, Legal Entities, Org Chart
3. **Time** — Attendance, Leave, Shifts, Holidays
4. **Payroll** — Pay Groups, Salary Structures, Salary Components, Employee Salaries, Payroll Runs, Payslips, Statutory
5. **Finance** — Expenses, Loans, Tax Declarations
6. **Lifecycle** — Onboarding, Exit, Assets, Documents
7. **Support** — Helpdesk, Knowledge Base
8. **Insights** — Reports, Analytics
9. **Admin** — Users, Roles, Workflows, Integrations, Audit Logs, Currency Settings, Billing

Items hidden if `!hasPermission`.

### 3.3 Inbox — `/inbox`

Aggregates pending actions across modules:

- Leave approvals
- Expense approvals
- Salary approvals
- Loan approvals
- Document sign-offs
- Exit approvals
- Helpdesk assignments
- Probation confirmations

Each item card: title, requester, age, SLA, inline `[Approve] [Reject] [Open]`.

Bulk: select multiple → Approve all / Reject all.

Filters: module, urgency, date.

---

## 4. STANDARD DIALOG CATALOG (reusable)

| Dialog | Trigger | Content | Buttons |
|---|---|---|---|
| `ConfirmDelete` | Delete row | "Delete {entity}? This cannot be undone." | Cancel / Delete |
| `ConfirmFinalize` | Payroll finalize | Typed-confirm "FINALIZE" + summary | Cancel / Finalize |
| `UnsavedChanges` | Nav away from dirty form | "You have unsaved changes." | Stay / Discard / Save |
| `AssignOwner` | Bulk reassign | LookupSelect new owner | Cancel / Reassign |
| `ExportDialog` | Export button | Format, columns, date range | Cancel / Export |
| `BulkUploadDialog` | Bulk actions | File dropzone + template + validation report | Cancel / Upload |
| `RateConfirmDialog` | Currency conversion | Rate + effective date, requires accept | Cancel / Accept |
| `CommentDialog` | Approve/reject | Textarea + attachments | Cancel / Submit |
| `RejectDialog` | Reject action | Reason (required) + optional attachment | Cancel / Reject |
| `RequestInfoDialog` | Need more info | Message to requester | Cancel / Send |
| `ScheduleReportDialog` | Report schedule | Frequency, recipients, format, next run | Cancel / Schedule |
| `DuplicateEmailDialog` | Duplicate detected | "Email in use by {name}." | Reassign / Cancel |
| `MigrationDialog` | Structure version change | "Migrate {n} employees now or later?" | Now / Later |
| `CycleWarningDialog` | Cut-off proximity | "Employee may miss current cycle." | Continue / Cancel |
| `PolicyBreachDialog` | Expense submit | Rule violated + fix options | Adjust / Cancel |
| `InsufficientBalanceDialog` | Leave apply | Options: apply LOP / adjust dates / cancel | — |
| `TeamOverlapWarningDialog` | Leave apply | Overlap with teammates | Continue / Cancel |
| `NoticePeriodShortfallDialog` | Exit | Recovery preview | Accept / Adjust |
| `ChangeBaseCurrencyDialog` | Legal entity edit | Blocked if payroll exists + explanation | Close |
| `DisableCurrencyDialog` | Currency settings | Fallback warning for users | Confirm / Cancel |
| `BulkResetDialog` | Currency settings | Reset all users to org default | Confirm / Cancel |
| `ResetPasswordDialog` | User edit | Generate + copy + send | Cancel / Reset |
| `CloneDialog` | Structure/pay group/template | New name + target | Cancel / Clone |
| `CommentThread` | Detail pages | Threaded comments with @mentions | — |

---

## 5. PAGE SPECIFICATIONS

> Every page below is a build contract. Each entry lists: **type**, **tables**, **layout**, **fields**, **row/bulk actions**, **dialogs**, **states**, **API**, **permissions**.

---

### 5.1 Dashboard — `/dashboard`

**Type:** Dashboard
**Tables:** aggregate only
**Purpose:** Role-aware landing.

**Widgets** (drag-orderable, per-user persisted in `user_preferences`):

| Widget | Data | Notes |
|---|---|---|
| Headcount snapshot | employees grouped by status | Total, joiners MTD, exits MTD |
| Pending approvals | approvals where status=pending | Link to /inbox |
| Payroll cost MTD | payroll_runs + lines | Bar by department; currency toggle |
| Attrition trend | employees exited over 12m | Line chart |
| Upcoming events | birthdays, anniversaries, probations | Feed |
| Open tickets | helpdesk_tickets where status≠closed | Count + SLA breaches |
| My payslips | payslips for current user | Last 3 + download |
| Quick actions | — | Create Employee, Run Payroll, Raise Ticket, Apply Leave |

**Dialogs:** `CustomizeDashboard` (checkbox list, drag reorder, reset), `DateRangePicker`.

**Role gating:** Payroll widget hidden for HR Executive; financial widgets for non-finance.

**States:** skeleton cards per widget; per-widget error fallback with retry.

---

### 5.2 Employee List — `/employees`

**Type:** List
**Main table:** `employees` (+ joined `departments`, `designations`, `locations`)

**Layout:**

```
[FilterBar]
  Search: name, code, email
  Filters: Status, Department, Designation, Location, Legal Entity, Pay Group, Employment Type, DOJ range
  Saved views dropdown  [Columns] [Export] [+ Add Employee]
[BulkActionBar] (on select)
[DataTable]
[Pagination]
```

**Columns (default):** checkbox, employee_code, full_name (avatar+name+email), department, designation, location, status pill, date_of_joining, kebab.

**Column config dialog:** show/hide, reorder, save as personal view.

**Row actions (kebab):** View · Edit · Manage salary · View documents · Initiate exit · Disable login · Delete (draft only).

**Bulk actions:** Bulk edit job details · Assign reporting manager · Assign pay group · Assign expense policy · Enable/disable PF · Export · Invite · Delete (drafts).

**Dialogs:** `BulkJobEditDialog`, `AssignOwner`, `BulkPayGroupDialog`, `BulkExpensePolicyDialog`, `BulkPFDialog`, `ExportDialog`, `BulkInviteDialog`, `InitiateExitDialog`, `ConfirmDisableLoginDialog`, `ConfirmDelete`.

**States:** empty → "No employees yet" + [Add] + [Import CSV]; loading → skeleton table; error → ErrorState.

**Row click** → detail; checkbox click does not navigate.

**Keyboard:** `j/k`, `Enter`, `x`, `e`.

**API:** `GET /api/employees?filters&sort&page` · `POST /api/employees/bulk` · `POST /api/employees/bulk-delete`.

**Permissions:** `employee.view` (list); `employee.create` (Add); `employee.bulk` (bulk).

---

### 5.3 Employee Create — `/employees/new`

**Type:** Wizard (4 steps)
**Tables written:** `employees`, `employee_personal_details`, `employee_statutory_details`, `employee_bank_details`, `employee_job_history`, `users` (if invite)

**Step 1 — Personal**

| Field | Component | Validation | Required |
|---|---|---|---|
| First name | Text | 1–100 | Yes |
| Last name | Text | 1–100 | Yes |
| Work email | Email | Unique per org; MX check | Yes |
| Personal email | Email | — | No |
| Phone | PhoneInput | E.164 | Yes |
| Date of birth | DatePicker | Age ≥ 14 | No |
| Gender | Select | — | No |
| Marital status | Select | — | No |
| Address lines | Text | — | No |
| City/State/Country/Postal | Text/Select | — | No |
| Emergency contact | Text + PhoneInput | — | No |

**Dialogs:** `DuplicateEmailDialog`, `ImageCropDialog` (avatar).

**Step 2 — Job**

| Field | Component | Notes |
|---|---|---|
| Employee code | Text | Auto-suggested; editable if permitted |
| Date of joining | DatePicker | Not future beyond 90d |
| Legal entity | LookupSelect | Required; drives base currency |
| Department | LookupSelect (tree) | Required; "Create new" allowed |
| Designation | LookupSelect | Required |
| Location | LookupSelect | Required |
| Cost centre | LookupSelect | Optional |
| Reporting manager | EmployeeLookup | Cannot self |
| Employment type | Select | FT/Contract/Intern/Consultant |
| Pay group | LookupSelect | Required for payroll |
| Probation months | Number | Default from policy |

**Dialogs:** `CreateDepartmentDialog` (nested), `CycleWarningDialog`.

**Step 3 — Policy** (all required LookupSelects)

Leave plan · Shift · Week-off policy · Holiday list · Attendance policy · Expense policy · Salary structure (optional).

**Step 4 — Invite**

Toggles: send welcome email (default on) · require onboarding checklist (default on).
Onboarding template picker · Buddy/mentor · Email preview with [Edit].

**Footer:** `[Cancel] [Save Draft] [Save & Invite]`.

**On success:** toast "Employee created. Invite sent." + [View profile].

**States:** validation summary banner with jump links; `UnsavedChanges` guard.

**Permissions:** `employee.create`.

**API:** `POST /api/employees` (atomic) · `POST /api/employees/draft`.

---

### 5.4 Employee Detail — `/employees/:id`

**Type:** Detail (tabs)

**Header:** avatar + full name + code (copy) · status pill · dept • designation • location · actions `[Edit] [Manage Salary] [Initiate Exit] [More]`.

**Kebab:** View documents · View assets · View loans · View expenses · Reset password · Disable login · Audit log.

**Tabs:**

| Tab | Content | In-tab dialogs |
|---|---|---|
| Profile | personal, contact, addresses, experience, education, identity, custom fields | `EditFieldDialog` (with approval if sensitive) |
| Job | current job + history timeline | `ChangeJobDetailsDialog` (effective date + reason) |
| Time | attendance summary, leave balance, recent requests | `RegulariseDialog` |
| Documents | list + upload + expiry alerts | `UploadDocumentDialog`, `DeleteDocumentDialog` |
| Assets | assigned assets + dates | `AssignAssetDialog`, `ReturnAssetDialog` |
| Finances | salary, CTC, revisions, payslips, tax, loans | `AddSalaryDialog`, `ReviseSalaryDialog`, `AddLoanDialog` |
| Performance | goals, reviews, feedback | link out |

**Right drawer:** `<AuditDrawer>` toggle via "History".

**Permissions:** tab visibility per role (Payroll Admin sees Finances, not Time).

---

### 5.5 Employee Edit — `/employees/:id/edit`

**Type:** Edit (tabbed form mirroring Detail tabs)
**Difference from Create:** all fields pre-filled; system fields read-only (code, DOJ); change reason + effective date required for job changes; approval for sensitive fields.

**Sensitive fields:** bank account, salary, statutory IDs → `ApprovalRequiredDialog` on save; fields show "Pending approval" state.

**Dialogs:** `EffectiveDateDialog`, `ApprovalRequiredDialog`, `UnsavedChanges`.

**Sticky footer:** `[Cancel] [Save Changes]` + unsaved indicator.

**Audit drawer:** always available.

---

### 5.6 Department List/Create/Edit — `/departments`, `/departments/new`, `/departments/:id/edit`

**List:** tree-table with expand/collapse; columns name, code, head (avatar), employee count, parent, active, actions.

**Row actions:** View · Edit · Add sub-department · Deactivate · Delete (no employees).
**Bulk:** Activate · Deactivate.
**Dialogs:** `ConfirmDeleteDepartment`, `ReassignEmployeesDialog`.

**Create/Edit fields:** name*, code, parent (tree-select), head (employee lookup), description, active toggle.

**Validation:** code unique; parent cannot be self/descendant.

**Permissions:** `org.department.manage`.

---

### 5.7 Designation List/Create/Edit — `/designations`, `/designations/new`, `/designations/:id/edit`

**List columns:** title, code, grade, employee count, active, actions.
**Filters:** grade, active.
**Row actions:** Edit · Deactivate · Delete (with reassignment check).
**Bulk:** Activate/Deactivate.
**Create/Edit fields:** title*, code, grade (select), description, active.

---

### 5.8 Location List/Create/Edit — `/locations`, `/locations/new`, `/locations/:id/edit`

**List columns:** name, code, legal entity, city, state, country, employee count, active, actions.
**Filters:** legal entity, country, active.
**Row actions:** Edit · View employees · Deactivate.
**Dialog:** `DeleteLocationDialog` (reassignment).
**Create/Edit fields:** legal entity*, name*, code*, address, city, state, country*, timezone, active. Changing legal entity warns about payroll implications.

---

### 5.9 Legal Entity List/Create/Edit — `/legal-entities`, `/legal-entities/new`, `/legal-entities/:id/edit`

**List columns:** name, code, country, base currency (locked pill if payroll run exists), PF code, ESIC code, TAN, active, actions.

**Row actions:** Edit · View settings · Deactivate.
**Dialog:** `ChangeBaseCurrencyDialog` (blocked if payroll exists; explains why).

**Create sections:** Identity (name, code, CIN, country) · Currency (base) · Statutory (PF code, ESIC sub-code, TAN, PAN, GSTIN) · Address.
**Help text:** "Base currency is permanent once payroll is run."

**Edit:** base currency read-only if any payroll exists; statutory fields editable with audit.

---

### 5.10 Pay Group List/Create/Edit — `/pay-groups`, `/pay-groups/new`, `/pay-groups/:id/edit`

**List columns:** name, legal entity, cycle, cut-off day, pay date, currency, employee count, active, actions.
**Row actions:** Edit · View employees · Clone.
**Dialog:** `ClonePayGroupDialog`, `CycleChangeDialog` (with effective date).

**Create fields:** name*, legal entity*, pay cycle* (Monthly/Biweekly/Weekly/Semi-monthly), cut-off day, pay date, currency (defaults from entity), payslip settings link.
**Advanced:** attendance cut-off, LOP rules, payslip template.

---

### 5.11 Salary Structure List/Create/Edit — `/salary-structures`, `/salary-structures/new`, `/salary-structures/:id/edit`

**List columns:** name, code, components count, assigned employees, active, actions.
**Row actions:** Edit · Clone · Preview · Deactivate.
**Dialog:** `CloneStructureDialog`.

**Create — Builder:**

```
[Header] name* [___] code [___] description [___]
[Builder]
  Left: Available components (searchable)
  Right: Selected components (drag reorder)
    Row: Component | Type | Formula | Default | Mandatory | Actions
[Preview] Live sample CTC breakdown
[Footer] [Cancel] [Save]
```

**Row edit drawer:** `EditComponentDialog` — formula editor (Monaco-like), rounding, caps, floors.

**Validation:** ≥1 earning; percentage components sum to 100 if percentage-based.

**Edit:** creates new version. Banner: "Editing creates v{n+1}. {x} employees use v{n}." + `MigrationDialog` (Migrate now / Later).

---

### 5.12 Salary Component List/Create/Edit — `/salary-components`, `/salary-components/new`, `/salary-components/:id/edit`

**List columns:** name, code, type, taxable, statutory, FBP, active, actions.
**Filters:** type, taxable, statutory.
**Row actions:** Edit · Deactivate · Duplicate.

**Create fields:** name*, code*, type*, calculation type* (Fixed/Percentage/Formula), formula (conditional), taxable toggle, statutory toggle, FBP toggle, description.
**Formula editor:** syntax help, test input, live validation.

**Edit:** if used in active structures → "Used in {n} structures. Changes affect future payroll runs only." + `ConfirmDialog`.

---

### 5.13 Employee Salary List/Create/Edit — `/employee-salaries`, `/employee-salaries/new`, `/employee-salaries/:id/edit`

**List columns:** employee, structure, CTC, effective from, effective to, currency, status, actions.
**Filters:** structure, status, effective date range.
**Row actions:** Edit · View revisions · Terminate.

**Create fields:** employee* (lookup), structure* (lookup), CTC* (MoneyInput), effective from*, currency (read-only from entity), component overrides (expandable table).
**On save:** validate CTC fits structure; `SalaryPreviewDialog` before commit.

**Edit:** creates a `salary_revisions` entry. Fields: new CTC*, effective date*, reason*, approver (auto from workflow).
**Approval:** CTC change > threshold requires payroll admin approval.

---

### 5.14 Payroll Run List/Create/Detail — `/payroll-runs`, `/payroll-runs/new`, `/payroll-runs/:id`

**List columns:** period, pay group, legal entity, employees count, total gross, total net, status pill, run type, created by, actions.
**Filters:** pay group, status, date range, run type.
**Row actions:** View · Continue (draft) · Rollback (finalized) · Download report.
**Bulk:** Download payslips for selected runs.

**Create — Wizard:**

1. **Select** pay group + period + run type (Regular/Off-cycle/Ad-hoc).
2. **Preview** employee table with gross/deductions/net/variance vs last period; "Recalculate" button.
3. **Validate** PF/ESI/PT/TDS/LWF checklist with pass/warn/fail pills; click warning → drawer details.
4. **Approve** summary card + approval chain + typed-confirm `FINALIZE`.
5. **Finalize** progress bar → toast + link to payslips.

**Side panel:** new joiners this cycle · exits this cycle · manual overrides count.

**Dialogs:** `VarianceDrilldownDialog`, `OverrideDialog`, `ConfirmFinalizeDialog`, `RollbackDialog`.

**Detail tabs:** Summary · Employees · Variances · Statutory · Payslips · Approvals · Audit.
**Summary cards:** gross, deductions, net, employer cost, headcount.
**Employees tab:** DataTable with inline edit for overrides (permission-gated).
**Statutory tab:** PF/ESI/PT/TDS/LWF sub-tables + challan download buttons.
**Approvals tab:** `<ApprovalTimeline>`.
**Actions:** Approve · Finalize · Rollback · Export · Download bank file.

---

### 5.15 Payslip List/Detail — `/payslips`, `/payslips/:id`

**List columns:** employee, period, gross, deductions, net, currency, status, released at, actions.
**Filters:** period, employee, status, pay group.
**Row actions:** View · Download PDF · Release · Hold.
**Bulk:** Download · Release · Hold.
**Dialogs:** `ReleasePayslipsDialog` (channels: app/email/SMS), `HoldPayslipDialog` (reason).

**Detail layout:** two-column. Left: PDF preview. Right: metadata (period, gross, net, status, release) + actions + lines table.
**Dual currency:** secondary amount under base with tooltip (rate, date, disclaimer).
**Actions:** Download · Email · SMS · Release · Hold · Reprint.
**Dialog:** `ResendPayslipDialog`.

---

### 5.16 Statutory Settings — `/settings/statutory`

**Type:** Settings per legal entity.
**Sections:** PF · ESI · PT · LWF · TDS · GCC (if applicable).
Each section: enable toggle, rate fields, applicability thresholds, filing details, override rules, save.

**Dialogs:** `RateChangeDialog` ("Changing PF rate affects {n} employees. Effective from {date}?"), `ImportRatesDialog` (CSV).

**Audit:** every change logged with old/new values.

---

### 5.17 Tax Declaration List/Detail — `/tax-declarations`, `/tax-declarations/:id`

**List columns:** employee, FY, regime, declared, approved, status, submitted at, actions.
**Filters:** FY, status, regime, department.
**Row actions:** Review · Approve · Reject · Request proof.
**Bulk:** Approve · Reject · Send reminder.
**Dialogs:** `RejectDeclarationDialog`, `ReminderDialog`.

**Detail tabs:** Declarations (section-wise) · Proofs · Computation sheet · History.
Each declaration row: approve/reject + proof thumbnail viewer.
**Computation sheet:** projected annual tax, monthly TDS, regime comparison.

---

### 5.18 Expense Claim List/Create/Edit — `/expense-claims`, `/expense-claims/new`, `/expense-claims/:id/edit`

**List columns:** employee, policy, claim date, amount, currency, status, submitted, approver, actions.
**Filters:** status, policy, date range, employee, amount range.
**Row actions:** View · Approve · Reject · Edit (pending) · Delete (draft).
**Bulk:** Approve · Reject · Mark paid · Export.
**Dialogs:** `RejectClaimDialog`, `MarkPaidDialog`.

**Create:** header (policy*, currency*, claim date*) + lines table (category, expense date, description, amount, receipt upload, cost centre) + total + `[Save Draft] [Submit for Approval]`.
**Validation:** receipt required if policy says so; per-line max enforced; category rules inline.
**Dialogs:** `ReceiptViewerDialog` (OCR side-by-side), `PolicyBreachDialog`.

**Edit:** only `draft` / `pending` (with approval reset). Banner: "Editing will restart approval."

---

### 5.19 Loan List/Create/Edit — `/loans`, `/loans/new`, `/loans/:id/edit`

**List columns:** employee, principal, interest rate, EMI, tenure, outstanding, status, disbursed at, actions.
**Filters:** status, policy, department.
**Row actions:** View · Edit (pre-disbursement) · Foreclose · Skip EMI · Mark paid outside payroll.
**Bulk:** Export · Send reminder.

**Create fields:** employee*, policy*, principal*, tenure*, interest rate (auto from policy, editable), EMI (calculated), disbursement date, first EMI date.
**Preview panel:** amortization schedule table.
**Approval:** multi-level per policy.
**Dialog:** `EMIPreviewDialog` (full schedule with principal/interest split).

**Edit:** only pre-disbursement fields; post-disbursement → foreclosure/skip EMI actions only.

---

### 5.20 Leave Request List/Create/Edit — `/leave-requests`, `/leave-requests/new`, `/leave-requests/:id/edit`

**List columns:** employee, leave type, start, end, days, status, approver, actions.
**Filters:** type, status, date range, department.
**Row actions:** Approve · Reject · Cancel · View.
**Bulk:** Approve · Reject.
**View toggle:** table / calendar with leave overlays.

**Create fields:** employee* (self by default), leave type*, start*, end*, days (auto), reason, attachment.
**Side panel:** balance, accrual rate, team overlap warning.
**Dialogs:** `InsufficientBalanceDialog`, `TeamOverlapWarningDialog`.

**Edit:** only pending; banner "Editing resets approval to step 1."

---

### 5.21 Attendance List/Edit — `/attendance`, `/attendance/:id/edit`

**List columns:** employee, date, shift, clock in/out, worked hours, overtime, status, source, actions.
**Filters:** date range, shift, status, department, source.
**Row actions:** Regularise · View timeline · Mark manual.
**Bulk:** Regularise · Export.
**View toggle:** Table / Calendar / Heatmap.

**Edit (Regularise) fields:** clock in*, clock out*, status*, reason*, attachment.
**Approval:** required if outside regularisation window.
**Dialog:** `AttendancePolicyWarningDialog`.

---

### 5.22 Onboarding Template List/Create/Edit — `/onboarding-templates`, `/onboarding-templates/new`, `/onboarding-templates/:id/edit`

**List columns:** name, role, department, task count, active, actions.
**Row actions:** Edit · Clone · Preview · Deactivate.

**Create fields:** name*, role, department, location, description.
**Task builder:** drag-drop; each: title, owner role, due days, depends on, mandatory.
**Preview panel:** candidate-facing checklist.

**Edit:** banner if in use → "Changes apply to new onboardings only."

---

### 5.23 Onboarding Task List/Create/Edit — `/onboarding-tasks`, `/onboarding-tasks/new`, `/onboarding-tasks/:id/edit`

**List columns:** title, template, owner role, due days, depends on, sequence, actions.
**Row actions:** Edit · Delete · Duplicate.

**Create/Edit fields:** template*, title*, description, owner role*, due days, sequence, depends on (task lookup).
**Validation:** no dependency cycles.

---

### 5.24 Exit Request List/Create/Edit — `/exit-requests`, `/exit-requests/new`, `/exit-requests/:id/edit`

**List columns:** employee, resignation date, LWD, reason, status, approver, actions.
**Filters:** status, department, date range.
**Row actions:** Approve · Reject · Cancel · View checklist · Initiate F&F.
**Bulk:** Initiate exit with settlement (CSV).

**Create fields:** employee*, resignation date*, LWD*, reason, notice period (auto from policy, editable), buyout option, exit interview toggle.
**Checklist preview:** auto-generated from template.
**Dialog:** `NoticePeriodShortfallDialog` (recovery preview).

**Edit:** status-dependent; after approval only LWD extension (with reason).

---

### 5.25 Asset List/Create/Edit — `/assets`, `/assets/new`, `/assets/:id/edit`

**List columns:** asset code, name, category, serial, purchase date, status, assigned to, actions.
**Filters:** category, status, location.
**Row actions:** Edit · Assign · Return · Mark unavailable · Delete.
**Bulk:** Assign · Return · Export.

**Create fields:** asset code* (auto), name*, category*, serial, purchase date, cost, vendor, condition, notes.
**Edit:** status transitions gated (available → assigned → returned → available/retired).
**Dialogs:** `AssignAssetDialog`, `ReturnAssetDialog` (damage charge).

---

### 5.26 Document Template List/Create/Edit — `/document-templates`, `/document-templates/new`, `/document-templates/:id/edit`

**List columns:** name, type, version, active, created by, actions.
**Row actions:** Edit · Clone · Preview · Deactivate · Delete.

**Create fields:** name*, type* (Offer/Letter/Policy/Acknowledgement), body (rich text + merge-field picker), version (auto).
**Preview panel:** renders with sample data.

**Edit:** saving bumps version. Banner: "Previous v{n-1} retained. In-flight documents use their original version."

---

### 5.27 User List/Create/Edit — `/users`, `/users/new`, `/users/:id/edit`

**List columns:** email, employee, roles (chips), active, last login, actions.
**Filters:** role, active, last login range.
**Row actions:** Edit · Reset password · Disable login · View login history.
**Bulk:** Disable · Enable · Export.

**Create fields:** email*, employee link (lookup), roles* (multi-select), send invite toggle, 2FA required toggle.
**Dialog:** `DuplicateUserDialog`.

**Edit:** changing roles shows "Review permissions" summary. Reset password → `ResetPasswordDialog`.

---

### 5.28 Role List/Create/Edit — `/roles`, `/roles/new`, `/roles/:id/edit`

**List columns:** name, code, type (system/custom), user count, actions.
**Row actions:** Edit · Duplicate · Delete (custom only).

**Create fields:** name*, code*, description.
**Permission matrix:** tree by module (Employee, Payroll, Attendance, Leave, Expenses, Loans, Documents, Assets, Reports, Settings). Leaf: View/Create/Edit/Delete/Approve. "Select all" per module.
**Preview panel:** "Users with this role will see: …".

**Edit:** system roles read-only + "Duplicate to customize"; custom full edit.

---

### 5.29 Workflow List/Create/Edit — `/workflows`, `/workflows/new`, `/workflows/:id/edit`

**List columns:** name, module, trigger event, steps count, active, actions.
**Filters:** module, active.
**Row actions:** Edit · Duplicate · Deactivate · View runs.

**Create fields:** name*, module*, trigger event* (Leave Applied, Expense Submitted, Salary Revision, Exit Initiated, Document Sign, etc.).
**Step builder:** ordered; each: approver role/type (Reporting Manager, Dept Head, HR Admin, Payroll Admin, Specific User), condition, SLA hours, escalation target.
**Validation:** ≥1 step; no cycles; final step required.

**Edit:** banner if in-flight instances → "Changes apply to new instances only."

---

### 5.30 Currency Settings — `/settings/currencies`

**Type:** Settings (org + per legal entity)

**Sections:**

1. Base currency per legal entity — read-only after payroll run.
2. Secondary currencies — multi-select from master; enable/disable.
3. Org default display currency — select from enabled secondaries.
4. Scoping — table: Legal Entity / Location / Department / Pay Group → default display currency.
5. User override — toggle + restrict allowed list.
6. Rate source — Manual / Scheduled feed / Third-party + provider config.
7. Rate type — Live/Spot · Monthly fixed · Payroll-period-locked · Year-fixed.
8. Manual rate override — table with effective-from, rate, reason, audit link.
9. Rounding — per-currency decimal precision.
10. Surfaces — Payslip annexure · Salary breakup · Expense claims · Loans · Reports · Dashboards.
11. Display mode — Dual vs. Converted-only.
12. Disclaimer text — rich text shown on converted figures.

**Dialogs:** `RateConfirmDialog` (impact preview), `BulkResetDialog`, `DisableCurrencyDialog`.

**Audit:** every config change logged.

---

### 5.31 Display Currency Preference (User) — `/me/preferences/currency`

**Fields:** radio "Use organisation default" / "Choose my display currency" + select (restricted list).
**If admin locked:** controls disabled + explanation + "Request change" → helpdesk.
**Preview panel:** sample amount in base + selected currency with rate + disclaimer.
**Actions:** `[Reset to default] [Save]`.

---

### 5.32 Audit Log List/Detail — `/audit-logs`, `/audit-logs/:id`

**List columns:** timestamp, user, entity type, entity id, action, IP, actions.
**Filters:** entity type, action, user, date range, IP.
**Row actions:** View diff.
**Bulk:** Export.

**Detail:** side-by-side diff (old vs new JSON, syntax-highlighted) + metadata panel + `[Copy as JSON] [Open entity]`.

---

### 5.33 Reports Dashboard + Viewer — `/reports`, `/reports/:code`

**Dashboard sections:** Payroll reports · HR reports · Compliance reports · Expense reports · Custom reports.
Each card: title, description, `[Run] [Schedule] [Export]`.

**Viewer:** filter bar + table/chart + export + schedule dialog.
**Dialog:** `ScheduleReportDialog` (frequency, recipients, format, next run).

---

### 5.34 Helpdesk Ticket List/Create/Edit — `/helpdesk/tickets`, `/helpdesk/tickets/new`, `/helpdesk/tickets/:id/edit`

**List columns:** id, subject, category, requester, assignee, status, SLA due, actions.
**Filters:** status, category, assignee, SLA breach.
**Row actions:** View · Assign · Change status · Close.
**Bulk:** Assign · Close · Export.

**Create fields:** category*, subject*, description*, priority, attachments.
**Knowledge base sidebar:** suggested articles.

**Edit:** status, assignee, priority, internal notes, public reply. SLA timer visible.

---

## 6. CROSS-CUTTING PATTERNS

### 6.1 Bulk upload pattern

Used by: Employees, Salaries, Loans, Documents, Rates.

```
[BulkUploadDialog]
  Step 1: Download template (CSV/XLSX) — context-aware columns
  Step 2: Upload file — drag-drop, max 10MB, .csv/.xlsx
  Step 3: Map columns — auto-match + manual override
  Step 4: Validate — row-level errors (red), warnings (amber)
  Step 5: Confirm — "Import {n} valid rows, skip {m} errors?"
  Step 6: Result — summary + download error report
```

### 6.2 Approval flow pattern

Used by: Salary, Expense, Leave, Loan, Exit, Document sign-off.

Every approvable entity renders `<ApprovalTimeline>`: step number, approver, status, acted at, comments, SLA.
Approver sees `[Approve] [Reject] [Request Info]` on detail page and in Inbox.
`RejectDialog` requires reason. `RequestInfoDialog` notifies requester.

### 6.3 Dual currency rendering rule

- Base amount primary (larger).
- Secondary below, muted, prefixed `≈`.
- Tooltip: "Converted at {rate} on {date}. {disclaimer}".
- On statutory surfaces (payslip, Form 16, bank file), secondary suppressed by default.

### 6.4 Notifications & Inbox

Global Inbox at `/inbox` aggregates leave approvals, expense approvals, salary approvals, loan approvals, document sign-offs, exit approvals, helpdesk assignments, probation confirmations. Inline Approve/Reject/Open per item.

### 6.5 Global search

TopBar search → grouped results (Employees, Documents, Pages, Reports). Debounced 300ms. Keyboard navigable.

### 6.6 Print / PDF

Every Detail page has Print → print-optimized layout (logo header, page-numbered footer, no nav).

### 6.7 Localization

Dates/numbers/currency via `Intl`. Strings externalized. RTL-ready.

### 6.8 Feature flags

Plan-tier gating (Foundation/Strength/Growth + add-ons). Locked features: lock icon + "Upgrade" tooltip. Enforced client + server.

---

## 7. STATE MATRIX (per page type)

| State | List | Create | Edit | Detail | Dashboard |
|---|---|---|---|---|---|
| Loading | SkeletonTable | SkeletonForm | SkeletonForm | SkeletonDetail | SkeletonCards |
| Empty | EmptyState + CTA | N/A | N/A | EmptyState (per tab) | Widget fallback |
| Error | ErrorState + retry | ErrorState + retry | ErrorState + retry | ErrorState + retry | Per-widget error |
| Permission-denied | Full-page | Full-page | Full-page | Full-page | Widget hidden |
| Offline | Banner | Banner + disable submit | Banner + disable submit | Banner | Banner |
| Partial-error | Row inline | Field inline | Field inline | Section inline | Widget inline |
| Unsaved | N/A | Guard | Guard | N/A | N/A |

---

## 8. ACCEPTANCE CRITERIA (per page)

A page is **done** when:

1. Route registered + breadcrumb correct.
2. Permission gate applied; unauthorized users see permission-denied state.
3. All documented fields render with correct component, validation, required markers.
4. All documented dialogs implemented with focus trap, Esc close, and correct primary/secondary buttons.
5. All documented row/bulk actions present and functional.
6. All states (loading, empty, error, permission, offline) implemented.
7. Unsaved-changes guard on forms.
8. Toast on every mutation success/failure.
9. Audit drawer wired to `audit_logs` where applicable.
10. Mobile responsive (table→cards, FAB).
11. Keyboard shortcuts respected.
12. Accessible labels on all inputs and icon buttons.
13. API endpoints wired or stubbed per §9.
14. Currency rendering respects `<CurrencyDisplay>` rules.
15. Export respects current filters.
16. Feature flag check for plan-tier gating.
17. QA sign-off on all dialogs.

---

## 9. API CONTRACT (conventions)

### 9.1 REST conventions

```
GET    /api/{resource}?filters&sort&page&pageSize
GET    /api/{resource}/{id}
POST   /api/{resource}
PATCH  /api/{resource}/{id}
DELETE /api/{resource}/{id}
POST   /api/{resource}/bulk
POST   /api/{resource}/bulk-delete
POST   /api/{resource}/{id}/{action}    # e.g. /approve, /reject, /finalize
GET    /api/{resource}/export?format=csv|xlsx
```

### 9.2 Standard response envelope

```json
{
  "data": { ... } | [ ... ],
  "meta": { "page": 1, "pageSize": 25, "total": 312 },
  "errors": [ { "code": "VALIDATION", "field": "email", "message": "..." } ]
}
```

### 9.3 Error codes

| HTTP | Code | Meaning |
|---|---|---|
| 400 | VALIDATION | Field-level errors in `errors[]` |
| 401 | UNAUTHENTICATED | Token missing/expired |
| 403 | FORBIDDEN | RBAC block |
| 404 | NOT_FOUND | Entity missing |
| 409 | CONFLICT | Duplicate / state conflict |
| 422 | BUSINESS_RULE | e.g. base currency locked |
| 429 | RATE_LIMITED | Throttled |
| 500 | INTERNAL | Retry + report |

### 9.4 Async jobs

Bulk import, payroll run, PDF generation → return `202 Accepted` + `jobId`. Client polls `/api/jobs/{jobId}` or subscribes via WebSocket.

### 9.5 Pagination, sorting, filtering

- `?page=1&pageSize=25` (max 100)
- `?sort=name:asc,created_at:desc`
- `?filter[status]=active&filter[department_id]=uuid`

### 9.6 Idempotency

All `POST` mutations accept `Idempotency-Key` header. Server caches for 24h.

---

## 10. SECURITY & COMPLIANCE

### 10.1 Auth

- OAuth2 + SSO (Okta, Azure AD, JumpCloud, OneLogin)
- 2FA (TOTP + SMS fallback)
- IP allowlist per tenant
- Session: 12h idle timeout; refresh token rotation

### 10.2 RBAC

- Roles from `roles` + `role_permissions`
- Implicit roles by org position (Reporting Manager, Dept Head, Business Head)
- Field-level permissions (e.g. Finance sees salary, Manager does not)
- Row-level security by `org_id` + `legal_entity_id`

### 10.3 Data protection

- Encryption at rest (AES-256) + in transit (TLS 1.3)
- PII fields (PAN, Aadhaar, bank account) masked in logs
- Document access via signed URLs (5 min TTL)
- Audit logs immutable (append-only)

### 10.4 Compliance certifications (target)

- SOC 2 Type II
- ISO 27001:2022
- GDPR
- India DPDP Act

### 10.5 Sensitive action log

Every action on these writes to `audit_logs` with mandatory reason: bank detail change, salary change, statutory ID change, role change, currency setting change, payroll finalize, payslip release, F&F settlement.

---

## 11. BUILD ORDER (recommended)

| Phase | Deliverable | Depends on |
|---|---|---|
| 0 | Design tokens + primitives (§2.2) + App Shell (§3) | — |
| 1 | Auth, Users, Roles, Permissions | Phase 0 |
| 2 | Org setup: Legal Entities, Locations, Departments, Designations, Cost Centres | Phase 1 |
| 3 | Employees (List, Create, Detail, Edit) + Documents + Assets | Phase 2 |
| 4 | Pay Groups, Salary Structures, Salary Components, Employee Salaries | Phase 3 |
| 5 | Payroll Runs, Payslips, Statutory Settings, Tax Declarations | Phase 4 |
| 6 | Expenses, Loans, Leave, Attendance | Phase 3 |
| 7 | Onboarding, Exit | Phase 3 |
| 8 | Workflows, Approvals, Inbox | Phase 5, 6, 7 |
| 9 | Currency Settings + Display Preference + `<CurrencyDisplay>` across surfaces | Phase 5 |
| 10 | Reports, Audit Logs, Helpdesk | Phase 5–9 |
| 11 | Bulk upload, exports, scheduled reports | Phase 10 |
| 12 | Mobile parity, accessibility audit, performance tuning | Phase 11 |

---

## 12. SUBAGENT BUILD CHECKLIST (per page)

Copy this into the PR description for every page:

```
Page: ____________________
Route: ____________________
Main table(s): ____________________

[ ] Route registered + breadcrumb
[ ] Permission gate applied
[ ] Loading / Empty / Error / Permission-denied / Offline states
[ ] All fields rendered with correct component + validation
[ ] All dialogs implemented (list): ____________________
[ ] Focus trap + Esc close on all dialogs
[ ] Unsaved-changes guard on forms
[ ] Toast on every mutation (success + error)
[ ] Audit drawer wired (if entity is audited)
[ ] Mobile responsive (table→cards, FAB)
[ ] Keyboard shortcuts respected
[ ] Accessible labels on all inputs + icon buttons
[ ] API endpoints wired/stubbed per §9
[ ] Currency rendering via <CurrencyDisplay>
[ ] Bulk actions appear only when rows selected + permitted
[ ] Export respects current filters
[ ] Feature flag check for plan-tier gating
[ ] QA verified all dialogs
[ ] Screenshots (desktop + mobile) attached
```

---

## 13. GLOSSARY

| Term | Meaning |
|---|---|
| **Base currency** | Immutable currency of a legal entity; all stored amounts denominated in it |
| **Secondary/display currency** | Display-only conversion; never affects stored values or statutory computation |
| **Pay group** | Group of employees sharing a pay cycle and cut-off |
| **Structure** | Salary structure: set of components + formulas |
| **Component** | Single line item (earning/deduction/reimbursement) |
| **FBP** | Flexible Benefit Plan — employee-allocated basket |
| **LOP** | Loss of Pay |
| **F&F** | Full & Final settlement |
| **ECR** | Electronic Challan-cum-Return (PF filing) |
| **PT** | Professional Tax |
| **LWF** | Labour Welfare Fund |
| **TDS** | Tax Deducted at Source |
| **SUI/SUTA** | State Unemployment Insurance / Tax (US) |
| **PFML** | Paid Family & Medical Leave (US) |
| **FLSA** | Fair Labor Standards Act (US) |
| **Implicit role** | Auto-assigned by org position (manager, dept head) |

---

## 14. CHANGE LOG

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-09-20 | Initial build contract paired with `keka_schema.csv` |

---

*End of build contract. Pair with `keka_schema.csv` for the complete specification.*