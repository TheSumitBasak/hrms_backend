# Frontend changes: payroll configuration and statutory rules (2026-11-02)

Everything here is **new and additive**. No existing endpoint changed shape except two small additions to the organization context response (§2). Nothing in this document computes pay: runs, payslips, salary structure and tax arrive later. This slice is the **configuration** a payroll run will depend on.

Spec source: `docs/PAYROLL_AND_STATUTORY_COMPLIANCE.md` §4 (configuration) and §15.2 (endpoints).

Base path for organization endpoints: `/api/v1/orgs/{org_id}`. Platform endpoints (no organization) live at `/api/v1/statutory-rules`.
Auth, the error envelope, `ETag` / `If-Match`, `acknowledged_warnings` and money-as-string behave exactly as in the rest of the API.

---

## 0. What you are building

| # | Screen | Who | Endpoints |
|---|---|---|---|
| 1 | Payroll settings (and module switches) | Payroll Administrator | `GET/PUT /payroll-settings` |
| 2 | Pay groups (list, create, edit) | Payroll Administrator (read: payroll viewers) | `GET/POST /pay-groups`, `GET/PATCH /pay-groups/{id}` |
| 3 | Payroll calendar (periods) | Payroll Administrator (read: payroll viewers) | `GET /payroll-periods`, `PATCH /payroll-periods/{id}` |
| 4 | Registration payroll attributes | Payroll Administrator, Statutory Compliance Officer | `GET/PUT /statutory-registration-settings/{registrationId}` |
| 5 | Statutory rules browser | any payroll viewer | `GET /statutory-rules` |
| 6 | Statutory rules admin (draft / publish) | System Super Admin only | `POST /statutory-rules/{setId}/versions`, `POST /statutory-rules/versions/{vid}/publish` |

Suggested build order: **§2 shared plumbing → 1 → 2 → 3 → 4 → 5 → 6**. Screen 1 must exist first: until payroll settings are saved and the module is on, every mutation in 2 to 4 is refused with `403 MODULE_NOT_ENABLED`.

---

## 1. Shared conventions (read once)

### Error envelope
```json
{ "error": { "code": "PAY_...", "message": "...", "details": [{ "field": "x", "code": "...", "message": "..." }],
             "warnings": [], "correlation_id": "..." } }
```
Show `message` as the banner, map each `details[]` entry to its form field via `field`, and put `correlation_id` in "report a problem".

### Optimistic locking (every PUT/PATCH on an existing row)
- Every `GET` and every successful write returns an `ETag: "<row_version>"` header. Store it with the record.
- Send it back as `If-Match: "<row_version>"` (quoted, bare number and `W/` forms all work).
- **Missing or stale `If-Match` → `412 PRECONDITION_FAILED`.** Handle it by re-fetching and showing "someone else changed this".
- Exception: the **first** `PUT` that creates a row (payroll settings, registration settings) takes **no** `If-Match`.
- The JSON body also carries `row_version`; the header is the source of truth.

### Money, rates and decimals are strings
A JSON number for a rate or money field is refused with `422` containing `MONEY_MUST_BE_STRING`. Send `"50.0000"`, `"15000"`; never `50`. Read them back as strings and use a decimal library, not `parseFloat`.

### Warnings
Some actions return `422 WARNINGS_REQUIRE_ACKNOWLEDGEMENT` with `error.warnings[]`. Show them, and on confirm re-send the **same request** with `"acknowledged_warnings": ["<code>", ...]`. Used here by the period edit (§5).

### Module gating
- Reads work whether or not the module is on.
- Mutations on pay groups, periods and registration settings return **`403 MODULE_NOT_ENABLED`** until `enable_payroll_module` is true.
- Hide or disable the Payroll menu using the flags in §2, but still handle the 403.

### Dates
`YYYY-MM-DD`. End dates (`effective_to`) are **exclusive**: a version with `effective_to = 2027-04-01` is in force through 2027-03-31.

---

## 2. Navigation, permissions and the context response

### Context response (existing endpoint, two new keys)
`GET /orgs/{org_id}/me/context` now includes:
```json
{ "modules":      { "onboarding": false, "attendance": false, "leave": false, "payroll": false },
  "self_service": { "...": "...", "payroll": false } }
```
Use `modules.payroll` to show or hide the whole Payroll section. `self_service.payroll` is reserved for the later payslip screens.

### Permissions (from `GET /auth/me`, `permissions[]` per organization)
| Permission | Used for | Roles that hold it by default |
|---|---|---|
| `payroll.view` | read settings, pay groups, periods; browse rules | Payroll Administrator, Processor, Approver, Finance, Statutory Compliance Officer, Auditor |
| `payroll.config_manage` | write settings, pay groups, periods | Payroll Administrator |
| `statutory.config_manage` | read/write registration payroll attributes | Payroll Administrator, Statutory Compliance Officer |
| `statutory.rule_prepare` / `statutory.rule_publish` | draft / publish rule versions | System Super Admin only |

Notes:
- **HR Admin and Organization Admin do not get payroll access by default** (they can be granted it). A 403 on these screens is normal: show an "ask your payroll administrator" state, not an error toast.
- Gate buttons by permission, but treat the API as authoritative.
- The bootstrap administrator holds none of the payroll-only permissions. Grant the Payroll Administrator role to a real user to use these screens.

Suggested sidebar:
```
Payroll
├─ Settings              (payroll.view)
├─ Pay groups            (payroll.view)
├─ Calendar              (payroll.view)
├─ Statutory registrations (statutory.config_manage)
└─ Statutory rules       (payroll.view)   ← admin tab only for statutory.rule_prepare/publish
```

---

## 3. Screen 1: Payroll settings

`GET  /orgs/{org_id}/payroll-settings`  → `200`, or **`404` before the first save** (show the "Set up payroll" empty state).
`PUT  /orgs/{org_id}/payroll-settings`  → `200`. **Full replacement**: an omitted field returns to its default. Always send the whole form.

First save: no `If-Match`. Later saves: `If-Match` required.

### Fields
| Field | Type | Default | Notes |
|---|---|---|---|
| `enable_payroll_module` | bool | `false` | Master switch. Needs an **INR** organization, else `422 PAYROLL_CURRENCY_NOT_SUPPORTED`. |
| `enable_payroll_self_service` | bool | `false` | Employee payslip access (later). |
| `enable_payroll_alumni_access` | bool | `true` | |
| `payroll_go_live_period` | date | **required** | Must be the **first of a month** (`422` otherwise). Use a month picker that sends day `01`. |
| `opening_ytd_required` | bool \| null | auto | Omit/`null` → server sets `true` unless go-live is April. Show as read-only hint, let the user override. |
| `default_proration_basis` | `CALENDAR_DAYS` \| `FIXED_30` \| `WORKING_DAYS` | `CALENDAR_DAYS` | |
| `component_rounding_default` | `NEAREST_RUPEE` \| `UP_RUPEE` \| `DOWN_RUPEE` \| `TWO_DECIMALS` | `NEAREST_RUPEE` | |
| `net_pay_rounding` | `NEAREST_RUPEE` \| `NONE` | `NEAREST_RUPEE` | |
| `lop_requires_finalized_attendance` | bool | `true` | |
| `lop_lookback_periods` | int 0-12 | `3` | |
| `arrear_lookback_periods` | int 0-36 | `12` | |
| `salary_hold_default_on_notice` | bool | `false` | |
| `deduction_limit_percent` | **string** `0`-`100`, 4 dp | `"50.0000"` | |
| `maker_checker_mode` | `"STRICT"` | `STRICT` | Only value allowed. Render read-only. |
| `one_time_tds_method` | `INCREMENTAL_IN_MONTH` \| `SPREAD_REMAINING` | `INCREMENTAL_IN_MONTH` | |
| `bonus_accounting_year_start_month` | int 1-12 | `4` | |
| `gratuity_provision_enabled` | bool | `false` | |
| `overtime_pay_enabled` | bool | `false` | |
| `maternity_pay_method` | `REGULAR_SALARY` \| `AVERAGE_DAILY_WAGE` | `REGULAR_SALARY` | |
| `payslip_zero_pay_generate` | bool | `true` | |
| `alumni_access_months` | int 0-36 | `18` | |
| `fnf_wage_payment_working_days` | int 0-30 | `2` | |

Response adds `id`, `organization_id`, `row_version`.

### Behaviour to reflect
- Saving with the module on **auto-generates payroll periods** 13 months ahead (§5). After a successful save, refresh the calendar.
- Changing `payroll_go_live_period` after runs exist will be blocked in a later release (`PAYROLL_RUNS_IN_PROGRESS`). Not enforced yet; do not rely on its absence. Show a "this is hard to change later" warning on that field.
- Turning the module **off** keeps reads working and makes mutations return `403`.

### Example
```http
PUT /api/v1/orgs/{org}/payroll-settings
{ "enable_payroll_module": true, "payroll_go_live_period": "2026-11-01",
  "deduction_limit_percent": "50.0000" }
```

---

## 4. Screen 2: Pay groups

| Call | Purpose | Notes |
|---|---|---|
| `GET /pay-groups?include_inactive=false` | list | default hides `INACTIVE` |
| `POST /pay-groups` | create | `201` + `ETag`; `409 DUPLICATE_RESOURCE` on a repeated code (field `pay_group_code`) |
| `GET /pay-groups/{id}` | read | `ETag` |
| `PATCH /pay-groups/{id}` | edit | `If-Match` required; send only changed fields |

### Create fields
| Field | Rule |
|---|---|
| `pay_group_code` | required, `^[A-Z0-9][A-Z0-9_./-]{0,29}$`, **immutable** after creation. Uppercase it client-side. |
| `pay_group_name` | required, 2-100 chars |
| `pay_frequency` | `"MONTHLY"` only (default) |
| `proration_basis` | optional; `null` → inherits the organization default from settings |
| `input_window_mode` | `SAME_MONTH` (default) or `OFFSET_CYCLE` |
| `input_cycle_start_day` | int 2-28. **Required for `OFFSET_CYCLE`, forbidden for `SAME_MONTH`** (`422`). Show/hide with the mode toggle. |
| `input_cutoff_offset_days` | 0-15, default 2 |
| `target_payment_day` | 1-28, default 1 |
| `pay_in_period` | bool, default false |

`PATCH` also accepts `status`: `ACTIVE` / `INACTIVE` (this is how you deactivate; nothing is deleted).

### Important behaviour
- **Cycle changes apply to future periods only.** Existing periods are not rewritten. Put this note under the cycle fields.
- Creating a group generates its periods immediately.
- When switching `input_window_mode` in a PATCH, send `input_cycle_start_day` in the same request (set it to `null` when going back to `SAME_MONTH`), or the server returns `422`.

---

## 5. Screen 3: Payroll calendar

`GET /payroll-periods?pay_group_id=&start=&end=` → array, ordered by month then group. `start`/`end` filter on `period_month` (inclusive). Names are **`start` and `end`**, not `from`/`to`.

### Period object
| Field | Meaning |
|---|---|
| `id`, `pay_group_id`, `row_version` | |
| `period_month` | first day of the month |
| `period_start`, `period_end` | pay period dates |
| `tax_year_start` | calendar year in which the tax year begins (April-March) |
| `input_window_start`, `input_window_end`, `input_cutoff_at` | when inputs are accepted; `input_cutoff_at` is a UTC timestamp, convert to the organization timezone for display |
| `payment_due_date` | statutory due date. **Always `null` for now** (needs a published wage-payment rule). Render "Not set". |
| `target_payment_date` | the date the organization plans to pay; the only editable field |
| `status` | `OPEN` or `CLOSED` |

### Edit the target payment date
`PATCH /payroll-periods/{id}` with `If-Match`:
```json
{ "target_payment_date": "2026-12-03", "acknowledged_warnings": [] }
```
- Only `OPEN` periods are editable (`422` otherwise). Disable the control for `CLOSED`.
- If the date is after `payment_due_date`, the server returns `422 WARNINGS_REQUIRE_ACKNOWLEDGEMENT` with warning code **`W-PAY-PAYDATE-LATE`**. Show "later than the statutory due date", and on confirm re-send with `"acknowledged_warnings": ["W-PAY-PAYDATE-LATE"]`. (Cannot trigger until due dates exist, but build the flow now.)

### UI suggestion
A month-by-month table per pay group (group filter at the top, range picker), `OPEN`/`CLOSED` chips, inline date edit on `target_payment_date`.

---

## 6. Screen 4: Registration payroll attributes

Statutory registrations themselves already exist (`GET /orgs/{org_id}/registrations`). This adds **payroll-only attributes** to a registration. Put it as a "Payroll settings" panel or drawer on each registration row.

| Call | Notes |
|---|---|
| `GET /statutory-registration-settings/{registrationId}` | **`404` until first saved**: show an empty form. A registration id from another organization is also `404`. |
| `PUT /statutory-registration-settings/{registrationId}` | full replacement; first save has no `If-Match`, later saves require it. Returns `ETag`. |

Needs `statutory.config_manage` for **both** read and write, and the payroll module on for the write (`403 MODULE_NOT_ENABLED`).

### Which fields apply to which registration type
The server refuses fields that do not apply (`422`, `details[].code = NOT_APPLICABLE`). Drive the form from `registration_type`:

| `registration_type` | Fields shown | Branches |
|---|---|---|
| `PF_ESTABLISHMENT` | `pf_contribution_rate_category` (`STANDARD`/`REDUCED`), `pf_exempted_trust`, `pf_trust_name` | yes |
| `ESIC_EMPLOYER`, `ESIC_SUB_CODE` | `esi_implemented_area` (bool, default true) | yes |
| `TAN` | `deductor_category`, `responsible_person_name`, `responsible_person_designation`, `responsible_person_pan` | no |
| `PT_REGISTRATION`, `PT_ENROLMENT`, `LWF` | none (send `{}`) | no |
| any other (GSTIN, shops, factory, …) | **no payroll settings**: do not show the panel; PUT returns `422` | no |

### Field rules
- `pf_trust_name` is required **if and only if** `pf_exempted_trust` is true (`422` otherwise). Disable the name input when the toggle is off.
- `deductor_category`: `COMPANY`, `GOVERNMENT`, `OTHERS`, `INDIVIDUAL_HUF`, `FIRM`, `LLP`, `AOP_BOI`, `TRUST`, `OTHER`.
- `responsible_person_pan`: `^[A-Z]{5}[0-9]{4}[A-Z]$`. Uppercase client-side.
  - **Write-only.** It is never returned. The response has `responsible_person_pan_masked` (last four visible). Show the mask, and a "Change" button that opens an empty input.
  - **Because PUT replaces everything, omitting the PAN on a later save clears it.** For an unchanged PAN you must re-enter it. Make this explicit in the UI ("Leave blank to remove"), or the user will wipe it by editing another field. (This is a deliberate full-replace semantic; see "Known limitations".)

### Branch assignments (PF and ESI only)
Part of the same body; the list **replaces** the previous list.
```json
{ "pf_contribution_rate_category": "STANDARD",
  "branches": [ { "branch_id": "<uuid>", "effective_from": "2026-04-01", "effective_to": null } ] }
```
- Branches come from `GET /orgs/{org_id}/branches`.
- `effective_to` is exclusive and must be after `effective_from`.
- **The same branch cannot appear twice with overlapping dates** (`422`, code `OVERLAP`). Adjacent ranges are fine (`to` of one = `from` of the next).
- Unknown or other-organization branch → `404`.
- Meaning: PF/ESI contributions for employees of that branch report under this registration (fallback: the organization-level active registration).

### Response
```json
{ "id": "...", "registration_id": "...", "pf_contribution_rate_category": "STANDARD",
  "pf_exempted_trust": false, "pf_trust_name": null, "esi_implemented_area": true,
  "deductor_category": null, "responsible_person_name": null, "responsible_person_designation": null,
  "responsible_person_pan_masked": null,
  "branches": [{ "branch_id": "...", "effective_from": "2026-04-01", "effective_to": null }],
  "row_version": 1 }
```

---

## 7. Screen 5: Statutory rules browser (read-only)

`GET /api/v1/statutory-rules?family=&jurisdiction_code=&as_of=`

Not under an organization. Allowed for anyone who holds `payroll.view` in at least one organization.

- `family`: `INCOME_TAX`, `TAX_EXEMPTIONS`, `PF`, `ESI`, `PT`, `LWF`, `BONUS`, `GRATUITY`, `MIN_WAGE`, `WAGE_CODE`, `OVERTIME`, `LEAVE_ENCASHMENT_TAX`, `DUE_DATES`, `FORM_REFERENCES`
- `jurisdiction_code`: `IN` for central rules, or a GST state code such as `24`.
- `as_of` (date): return only the version **in force that day** per rule set. Omit it to get all **published** versions.
- **Drafts are never listed here.** Only published versions appear.

### Response
```json
[ { "id": "<setId>", "rule_family": "PF", "jurisdiction_code": "IN", "rule_set_code": "PF_CENTRAL",
    "description": null,
    "versions": [ {
      "id": "...", "version_number": 2, "effective_from": "2026-04-01", "effective_to": null,
      "status": "PUBLISHED", "parameter_schema_version": "PF.v1",
      "verification_status": "SECONDARY_CONFIRMED",
      "prepared_by": "...", "published_by": "...", "published_at": "...",
      "parameters": [{ "parameter_code": "EMPLOYEE_RATE", "value_type": "RATE_PCT",
                       "value_decimal": "12.000000", "value_text": null, "value_boolean": null, "value_date": null }],
      "slabs": [{ "slab_table_code": "RATES", "sequence_no": 1, "condition_code": "",
                  "lower_bound": "0.00", "upper_bound": "300000.00", "rate_pct": "0.0000", "fixed_amount": null }],
      "list_values": [{ "list_code": "EXEMPT", "value_code": "X1", "label": "Exempt one" }] } ] } ]
```
Read a parameter from the one non-null `value_*` column matching `value_type`.

### UI
- Filters: family, jurisdiction, **"as of" date** (default today).
- Per rule set: version timeline, then tabs for Parameters / Slabs / Lists.
- **Show the `verification_status` badge prominently.** `UNVERIFIED` and `SECONDARY_CONFIRMED` mean the values are not yet legally approved; payroll cannot be approved on them without an acknowledgement (later release).
- Group slabs by `slab_table_code` then `condition_code` (empty string = unconditional), sorted by `sequence_no`. `upper_bound: null` means "and above".

---

## 8. Screen 6: Statutory rules admin (System Super Admin only)

Show only to users holding `statutory.rule_prepare` / `statutory.rule_publish`. Two people are needed: the preparer cannot publish.

### 8.1 Draft a version
`POST /api/v1/statutory-rules/{setId}/versions` → `201`, status `DRAFT`, `version_number` assigned by the server.
```json
{ "effective_from": "2026-04-01", "effective_to": null,
  "parameter_schema_version": "PF.v1",
  "legal_review_notes": "Per EPFO circular ...",
  "parameters": [
    { "parameter_code": "EMPLOYEE_RATE", "value_type": "RATE_PCT", "value": "12" },
    { "parameter_code": "WAGE_CEILING",  "value_type": "MONEY",    "value": "15000" },
    { "parameter_code": "VPF_ALLOWED",   "value_type": "BOOLEAN",  "value": true } ],
  "slabs": [
    { "slab_table_code": "RATES", "sequence_no": 1, "lower_bound": "0.00", "upper_bound": "300000.00", "rate_pct": "0" },
    { "slab_table_code": "RATES", "sequence_no": 2, "lower_bound": "300000.00", "rate_pct": "5" } ],
  "list_values": [ { "list_code": "EXEMPT", "value_code": "X1", "label": "Exempt one" } ] }
```
Field rules (all `422` on violation):
- `parameter_schema_version` must be `<FAMILY>.v<n>` and **match the rule set's family** (`PF.v1` for a PF set).
- `value` is a **string** for every type except `BOOLEAN` (JSON `true`/`false`). Numbers as JSON numbers are refused. `DATE` is `YYYY-MM-DD`. `INTEGER` must have no fraction.
- `value_type`: `DECIMAL`, `MONEY`, `RATE_PCT`, `INTEGER`, `BOOLEAN`, `DATE`, `TEXT`, `CODE`.
- `parameter_code`: `^[A-Z0-9_]{1,60}$`. `slab_table_code`, `list_code`: `^[A-Z0-9_]{1,40}$`.
- Each slab needs `rate_pct` **or** `fixed_amount`; `upper_bound` must be greater than `lower_bound`; `rate_pct` is 0-100.
- No duplicate parameter codes, slab keys (`table + condition + sequence_no`) or list entries.
- `effective_to`, if given, must be after `effective_from`.
- Unknown `setId` → `404`.

Build this as a form with repeatable sections for parameters, slab tables (a grid per `slab_table_code`) and lists, plus a "paste JSON" option since real rule sets are large.

### 8.2 Publish (the checker)
`POST /api/v1/statutory-rules/versions/{versionId}/publish`
```json
{ "verification_status": "PRIMARY_CONFIRMED", "legal_review_notes": "Verified against gazette ..." }
```
`verification_status`: `UNVERIFIED`, `SECONDARY_CONFIRMED`, `PRIMARY_CONFIRMED`, `LEGALLY_APPROVED`.

Errors to handle with specific messages:
| Status / code | Meaning | UI |
|---|---|---|
| `403 RULE_SELF_PUBLISH_NOT_ALLOWED` | You prepared this version | "A different administrator must publish this." Disable the button when `prepared_by` equals the current user id. |
| `422` `SLAB_INVALID` (in `details[]`) | slabs have a gap, overlap, or an open-ended band that is not last | show the message list next to the slab grid |
| `422` `RULE_VERSION_OVERLAP` | `effective_from` is on or before an already published version of the same set | "Must start after the latest published version." |
| `422` (not a draft) | already published | refresh |
| `404` | unknown version | |

Side effect to explain in the confirmation dialog: **publishing closes the previous version** by setting its `effective_to` to this version's `effective_from`. Published versions can never be edited; a correction is a new version.

### 8.3 Not available yet
- No endpoint to **list drafts** or fetch a single version by id. Keep the draft response in client state, or have the preparer share the id. The browser (§7) never shows drafts.
- No **create rule set** endpoint. Rule sets must exist already (see "Known limitations").
- No withdraw, legal sources, per-organization overrides or tax-provision map yet.

---

## 9. TypeScript shapes (suggested)

```ts
type Decimal = string;                       // never number
interface PayrollSettings {
  id: string; organization_id: string; row_version: number;
  enable_payroll_module: boolean; enable_payroll_self_service: boolean; enable_payroll_alumni_access: boolean;
  payroll_go_live_period: string; opening_ytd_required: boolean;
  default_proration_basis: 'CALENDAR_DAYS'|'FIXED_30'|'WORKING_DAYS';
  component_rounding_default: 'NEAREST_RUPEE'|'UP_RUPEE'|'DOWN_RUPEE'|'TWO_DECIMALS';
  net_pay_rounding: 'NEAREST_RUPEE'|'NONE';
  lop_requires_finalized_attendance: boolean; lop_lookback_periods: number; arrear_lookback_periods: number;
  salary_hold_default_on_notice: boolean; deduction_limit_percent: Decimal; maker_checker_mode: 'STRICT';
  one_time_tds_method: 'INCREMENTAL_IN_MONTH'|'SPREAD_REMAINING'; bonus_accounting_year_start_month: number;
  gratuity_provision_enabled: boolean; overtime_pay_enabled: boolean;
  maternity_pay_method: 'REGULAR_SALARY'|'AVERAGE_DAILY_WAGE'; payslip_zero_pay_generate: boolean;
  alumni_access_months: number; fnf_wage_payment_working_days: number;
}
interface PayGroup {
  id: string; row_version: number; pay_group_code: string; pay_group_name: string; pay_frequency: 'MONTHLY';
  proration_basis: PayrollSettings['default_proration_basis'] | null;
  input_window_mode: 'SAME_MONTH'|'OFFSET_CYCLE'; input_cycle_start_day: number | null;
  input_cutoff_offset_days: number; target_payment_day: number; pay_in_period: boolean;
  status: 'ACTIVE'|'INACTIVE';
}
interface PayrollPeriod {
  id: string; pay_group_id: string; row_version: number; period_month: string;
  period_start: string; period_end: string; tax_year_start: number;
  input_window_start: string; input_window_end: string; input_cutoff_at: string;
  payment_due_date: string | null; target_payment_date: string; status: 'OPEN'|'CLOSED';
}
interface RegistrationBranch { branch_id: string; effective_from: string; effective_to: string | null }
interface RegistrationPayrollSettings {
  id: string; registration_id: string; row_version: number;
  pf_contribution_rate_category: 'STANDARD'|'REDUCED'|null; pf_exempted_trust: boolean; pf_trust_name: string|null;
  esi_implemented_area: boolean; deductor_category: string|null;
  responsible_person_name: string|null; responsible_person_designation: string|null;
  responsible_person_pan_masked: string|null; branches: RegistrationBranch[];
}
```

---

## 10. Known limitations the UI must tolerate

1. **No rule sets exist after a fresh install.** Seed data creates none and there is no create endpoint, so the rules browser is empty and the draft endpoint has nothing to target until rows are inserted by the backend team. Build an empty state ("No statutory rules have been loaded").
2. **System Super Admin cannot browse rules** through §7: the browse endpoint requires `payroll.view`, which SSA does not hold. SSA works from the draft/publish responses only until the backend adds a rule-prepare read path. Tell backend if this blocks the admin screen.
3. **`payment_due_date` is always `null`**; the late-payment warning cannot fire yet.
4. **Registration PAN is replace-only** (see §6). Consider asking backend for `PATCH` semantics if users find re-entry painful.
5. **Rule parameter codes are not validated against a registry.** A typo in `parameter_code` is accepted at draft time. Treat the admin form as a power tool and show a confirmation summary before publish.
6. **Settings changes that are not enforced yet:** go-live immutability and "disable with runs in flight" arrive with the processing engine.

---

## 11. QA checklist

- [ ] Payroll section hidden when `modules.payroll` is false; visible after enabling.
- [ ] First settings save works with no `If-Match`; second save without it returns `412` and is handled.
- [ ] Go-live picker only sends a first-of-month date.
- [ ] JSON number in `deduction_limit_percent` never leaves the client.
- [ ] Non-INR organization shows the `PAYROLL_CURRENCY_NOT_SUPPORTED` message on enable.
- [ ] Creating a pay group fills the calendar; duplicate code shows a field error.
- [ ] `OFFSET_CYCLE` shows and requires the cycle day; switching back clears it.
- [ ] `CLOSED` period date edit is disabled; `W-PAY-PAYDATE-LATE` acknowledgement flow works.
- [ ] Registration panel appears only for PF, ESIC, TAN, PT and LWF types, with the right fields per type.
- [ ] Trust name toggles with `pf_exempted_trust`; PAN is masked and write-only; blank PAN on save clears it (with warning text).
- [ ] Overlapping branch dates show the `OVERLAP` error against the branch rows.
- [ ] HR Admin sees a "no access" state on every payroll screen, not a crash.
- [ ] Rules browser `as_of` shows exactly one version per set; boundary dates behave (start inclusive, end exclusive).
- [ ] Preparer's publish button disabled; direct call shows the `RULE_SELF_PUBLISH_NOT_ALLOWED` message.
- [ ] Publishing a successor shows the predecessor's end date updated in the timeline.
