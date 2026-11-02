n# HRMS v2: Limitations and Errors (HR Review)

**Reviewer view:** Senior HR Manager, with Payroll and Compensation in scope
**Scope:** Salary Components, Salary Templates, Employee Salary Revisions, plus a few cross-module gaps
**Basis:** Review of the frontend source (`src/app/(authed)/o/[orgId]/...`). Backend rules were not inspected. Where the backend might already enforce something, the item says "confirm with backend".

Severity: **Critical** = wrong pay or unusable for payroll. **High** = needs manual workarounds or invites errors. **Medium** = friction or audit risk. **Low** = polish.

---

## 0. Resolution status (backend, 2026-11-01)

The backend was inspected after this review. Items marked **Done** are fixed in the API; **Frontend** items need UI work only (see FRONTEND_CHANGES.md); **Deferred** items need the payroll engine and conflict with the EMS rule (§4.3) that forbids arithmetic on salary values, so they belong to PAYROLL_AND_STATUTORY_COMPLIANCE.md.

| # | Status | Detail |
|---|---|---|
| 1, 2, 3, 4, 5 | Deferred | Basic flag, component references, formulas, totals and statutory logic need the payroll addendum. Not started. |
| 6 | Partly done | Server already validates ranges (`>= 0`, percentage 0-100) and typed columns. Input masking is Frontend. |
| 7 | Done | `POST …/publish` accepts the unsaved builder state and saves it in the same transaction. Old clients that send no body behave as before. |
| 8 | Done | `GET …/versions` and `GET …/versions/{vid}` expose every version and its mappings. The client can restore a draft after refresh and show history. |
| 9 | Frontend | The server rejects an inactive component in a mapping (by design). The client must keep or explicitly drop deactivated components and tell the user. |
| 10 | Already enforced | `ComponentNotOverridable` and the override-reason rule are enforced server-side. The UI should read `is_overridable` from the version mappings and mark rows read-only. |
| 11 | Frontend | Clearing a value works if the client sends an explicit `null` with an `override_reason`; the client currently drops empty strings. |
| 12 | Already supported | `components` is accepted on `POST …/salary-assignments`. The form just does not use it. |
| 13 | Done (partly) | `GET …/salary-components/{id}/usage` gives the impact counts. Code and value type stay permanent by design; no delete. |
| 14 | Done (currency) | Currency must be an active ISO 4217 code on components, templates and versions. Period normalisation is Deferred. |
| 15, 16 | Deferred | Bulk revision, arrears and numeric comparison need arithmetic. |
| 17 | Frontend | The employee list endpoint already accepts `RETIRED` and `ABSCONDING`. Add them to the filter. |

---

## 1. Summary

The module records **which components an employee has and what text or number sits against each one**. It does not model compensation. There is no CTC or gross anchor, no way to identify Basic, no formulas and no totals. HR cannot use it as the source of truth for payroll without a spreadsheet beside it.

| # | Area | Issue | Severity |
|---|------|-------|----------|
| 1 | Components | No way to mark a component as Basic (or any system role) | Critical |
| 2 | Components | "Percentage of" is free text, not a link to a component | Critical |
| 3 | Components | No formula or calculated component type | Critical |
| 4 | Templates / Assignments | No CTC, gross or net anchor amount; no totals anywhere | Critical |
| 5 | Components | No statutory deduction logic (PF, ESI, PT, TDS) | Critical |
| 6 | Values | All values are unvalidated strings | High |
| 7 | Templates | Publish silently discards unsaved edits | High |
| 8 | Templates | Draft versions vanish on page refresh; no version list | High |
| 9 | Templates | Saving a draft silently drops deactivated components | High |
| 10 | Assignments | Override rules (`is_overridable`, override reason) are not enforced in the UI | High |
| 11 | Assignments | A value cannot be cleared or zeroed reliably | High |
| 12 | Assignments | New revision cannot set component values at creation | Medium |
| 13 | Components | Code and value type are permanent; no delete | Medium |
| 14 | Currency / Period | Free-text currency; no period normalisation | Medium |
| 15 | Revisions | No bulk revision, increment tool or arrears for backdated changes | Medium |
| 16 | Revisions | Compare shows no differences in numbers | Medium |
| 17 | Cross-module | Employee list status filter is missing statuses | Low |

---

## 2. Salary Components (`setup/salary-components`)

### 2.1 No way to identify "Basic" (Critical)
A component is defined by `component_code` (free text, max 30), `component_name`, `component_category` and `value_type`. Nothing marks a component as Basic.

- Categories are only `EARNINGS`, `ALLOWANCE`, `EMPLOYER_BENEFIT`, `EMPLOYEE_DEDUCTION_REFERENCE` and `OTHER`. "Basic" is just an `EARNINGS` row whose name someone typed.
- Two components named "Basic", "BASIC" and "Basic Pay" can all exist. The system cannot tell which one the others refer to.
- There are no flags such as *is basic*, *is pensionable*, *is taxable*, *counts toward PF/ESI wage*, *included in CTC* or *included in gross*.

**Impact:** Any rule that depends on Basic (HRA %, PF 12% of Basic, gratuity, leave encashment) cannot be derived. It cannot even be validated.
**Fix:** Add a system role or flag per component (Basic, HRA, Special Allowance and so on), enforce one Basic per organisation, and add statutory-wage flags.

### 2.2 "Percentage of" is free text (Critical)
The field `percentage_reference_label` is a plain 100-character text box with the hint "What a percentage refers to, e.g. Basic."

- It stores a label, not a reference to another component.
- A typo such as "Bsic", or "Basic" when the real component is named "Basic Salary", is accepted.
- Renaming the target component leaves the label stale.
- The reference cannot be checked for existence, for circularity, or for whether the target is the same period or currency.

**Impact:** A `PERCENTAGE` component such as HRA at 40% has no machine-readable base. The value `40` means nothing to the system.
**Fix:** Replace the text with a `reference_component_id` selector limited to active components.

### 2.3 No direct or calculated amount (Critical)
Value types are `FIXED_AMOUNT`, `PERCENTAGE`, `TEXT`, `BOOLEAN` and `OTHER_CONFIGURED`.

- `FIXED_AMOUNT` is a typed-in number. A percentage is a typed-in number. Neither is ever resolved to money.
- There is no formula type (for example `HRA = 40% of Basic`, or `Special Allowance = CTC − all other earnings`). The balancing-figure pattern HR uses daily is impossible.
- `OTHER_CONFIGURED` has no UI and no explanation of what it configures.
- `TEXT` and `BOOLEAN` are listed as value types for salary components. It is unclear what a text or yes/no component means for pay.

### 2.4 No statutory or deduction handling (Critical)
- The only deduction category is `EMPLOYEE_DEDUCTION_REFERENCE`, which is a reference label, not a deduction.
- The system stores PF, ESIC and PT registrations (`setup/registrations`) and identifiers (UAN, ESIC IP), but nothing links them to pay. There is no PF, ESI, PT, LWF or TDS calculation, no slabs and no wage ceilings.
- No payroll run, payslip or bank-advice code was found in the frontend.

### 2.5 Permanent fields and no delete (Medium)
- `component_code` and `value_type` are `createOnly`. A wrong value type (for example `TEXT` instead of `FIXED_AMOUNT`) means creating a new component and re-mapping every template.
- Components can only be **deactivated** (`deactivateOnly`). A mistyped component stays in the list for ever.
- There is no "used in N templates / N employees" indicator before deactivating. See 3.3 for what deactivation does to drafts.

### 2.6 Other component gaps (Medium)
- `amount_period` is optional. A `FIXED_AMOUNT` can be saved with no period, so ₹50,000 could be monthly or annual.
- `currency` is optional at component level but required at template level. A component can disagree with its template and nothing flags it.
- `display_order` exists on the component, but the template mapping overwrites it with list position (`display_order: i`). Components appear in whatever order the API returns them.

---

## 3. Salary Templates (`setup/salary-templates`)

### 3.1 Publish silently discards unsaved edits (High)
`Publish` only calls `.../publish`. It does not save the mappings, the change note or the effective date first. A user who ticks components, types defaults and clicks **Publish** without **Save draft** publishes the older saved version. No warning is shown.

### 3.2 Drafts disappear on refresh; no version history (High)
The draft version lives only in React state (`useState`) after "Create template" or "New version".

- Refreshing, navigating away or switching tabs loses the draft panel. There is no way back into it from the UI.
- The page itself says: *"Earlier versions aren't listed here because the API doesn't expose them yet."* HR cannot see which version is live, what it contains, or what changed between versions.
- "New version" is disabled only by local state, so after a refresh a second draft can be started on top of an existing one.

### 3.3 Deactivated components vanish from drafts (High)
The panel lists only `ACTIVE` components and builds mappings only from that list. If a component is deactivated after it was mapped, the next **Save draft** drops it from the template with no message.

### 3.4 No validation in the template builder (High)
- A template can be created and published with no components (confirm with backend).
- A component marked **Required** needs no default, and nothing checks that the template is complete.
- Percentage defaults have no range check (150% or -5% are accepted).
- Nothing checks that a percentage component's base component is also on the template. See 2.2.
- A template has no grade, designation, band or location tag. Matching an employee to a template is manual.

### 3.5 Other template gaps (Low)
- Currency is a free 3-letter text box (uppercased only). Codes such as `XXX` are accepted client-side.
- No duplicate or "save as" for templates, so each new grade starts from scratch.

---

## 4. Employee Salary (`employees/[id]/salary`)

### 4.1 No CTC, gross or net (Critical)
- The current-salary card shows only the **template name, version and currency**. It never shows an amount.
- Component values appear inside a collapsed `<details>` as raw strings. There is no total CTC, monthly gross, net pay or employer cost.
- Code comments state the design intent: *"no totals, differences or conversions"* and *"no float conversion, no arithmetic"*. This is a deliberate limit, but it means HR cannot answer "what is this person paid?" from the screen.

### 4.2 Values are unvalidated strings (High)
Amounts, percentages and text all pass through the `Comp` type as `string | number | boolean`.

- The input uses `inputMode="decimal"`, which only changes the mobile keyboard. Letters, `1,00,000` and `12..5` can still be typed. Only server errors can stop them.
- No thousands formatting or currency symbol is shown, so large amounts are hard to read and easy to mis-key.
- Annual and monthly values sit side by side and are not normalised.

### 4.3 Override rules are not enforced in the UI (High)
Templates carry `is_overridable`, and components carry `is_editable_per_employee`. The employee component editor ignores both. Every row is editable, and the **Override reason** box is always optional. The reason is sent only if the value changed and a reason was typed, so an override can go through with no reason (confirm with backend). This weakens the audit trail for pay changes.

### 4.4 A value cannot be cleared or set to blank (High)
On save, empty strings are dropped from the payload. If HR clears a value, the field is simply not sent. The server then keeps the old value or rejects the request, and the UI cannot say which.

### 4.5 New revision cannot set values (Medium)
The *New salary revision* form takes a template, dates and a reason. The code comment says component values "come from the template." To give an employee an individual figure, HR must create the draft, then open **Edit**. Template defaults are rarely right per person, so this is the normal path, not an edge case.

### 4.6 Bulk and arrears gaps (Medium)
- There is no bulk revision, no annual-increment run, no "increase by x%" and no promotion-linked revision. Each employee is revised one at a time.
- Backdated revisions need only a text reason. No arrears are calculated.
- There is no export of salary data and no salary report. None was found in the code.

### 4.7 Comparison does not compare (Medium)
The compare page shows earlier and later values as stored, with a label such as "changed". It does not show the difference or the percentage change. Its own hint says: *"the app doesn't calculate differences."* Because it is string-based, `50000` and `50000.00` can show as different.

### 4.8 Workflow and display gaps (Medium)
- The UI offers one **Approve** and one **Reject** action. There is no multi-level approval chain, no approver comment on approval, and no visible block on self-approval (confirm with backend).
- The timeline preview prints `boundary_adjustments` as raw JSON. A non-technical HR user cannot interpret it.
- The salary list has no pagination, and the edit and correct pages fetch the entire list to find one record.
- The only permission gate is whole-tab `salary.view`. There is no masking of figures for users who see the tab but should not see amounts.

---

## 5. Other Issues Found in Passing

- **Status filter mismatch (Low):** `employees/page.tsx` filters on `ACTIVE, DRAFT, ON_NOTICE, INACTIVE, RESIGNED, TERMINATED`. The status-change page also allows `RETIRED` and `ABSCONDING`, so employees in those states cannot be filtered for.
- **Duplicate type definitions (Low):** `Comp` is declared separately in `salary/page.tsx`, `components-editor.tsx` and `version-panel.tsx` with different shapes. A backend field change can break them unevenly.

---

## 6. Recommended Fix Order

1. **Component roles:** add a system role (Basic, HRA, Special) and statutory flags. Replace the "Percentage of" text with a component reference (2.1, 2.2).
2. **Calculation engine:** add formula components and a balancing-figure option. Add CTC / gross / net totals computed on the server and shown in the UI (2.3, 4.1).
3. **Template safety:** save before publish (or warn), a version list, restore of drafts after refresh, and a check for orphaned components (3.1 to 3.4).
4. **Value integrity:** typed numeric inputs with min/max, a period required for amounts, and ISO currency validation (4.2, 2.6, 3.5).
5. **Override controls:** honour `is_overridable`, require a reason on any override, and allow clearing a value (4.3, 4.4).
6. **Statutory and payroll:** PF, ESI, PT and TDS rules, then payroll run and payslips.
7. **HR operations:** bulk revisions, arrears, difference-aware comparison and salary reports (4.6, 4.7).

## 7. Not Reviewed

Backend validation and calculation, database constraints, other modules (leave, attendance, onboarding, retention), automated test coverage, and actual runtime behaviour. Every finding above comes from reading the frontend source and has not been reproduced in a running system.
