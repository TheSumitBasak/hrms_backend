# Frontend changes: salary component restructure (2026-11-02)

Base path: `/api/v1/orgs/{org_id}`. Auth, error envelope, `ETag` / `If-Match`, and money-as-string are unchanged.

All request changes are additive and optional, so existing calls keep working. Two behaviours are new and **will** reject calls that used to succeed: every salary assignment must now include **Basic with an amount** (§5), and `BASIC` is a reserved component code (§1).

---

## 1. Salary components (the catalogue)

### New fields
| Field | On | Meaning |
|---|---|---|
| `display_name` | create (optional), patch, response | What the **employee** sees. Defaults to `component_name`. |
| `component_name` | unchanged | Internal name that lets admins tell components apart. Show it in admin screens only. |
| `component_type` | create (optional, default `EARNING`), response | `EARNING` = receivable, `DEDUCTION` = deductible. Immutable after creation. |
| `is_system` | response | `true` only for **Basic**. |

`component_category` is now optional on create (defaults to `OTHER`) and is just a grouping label; use `component_type` for earning/deduction.

### Calculation type ("fixed" or "% of basic")
Reuse `value_type`:
- `FIXED_AMOUNT` → needs `amount_period` and `currency` (unchanged).
- `PERCENTAGE` → **now means "% of Basic"**. No reference label is needed; the server fills `percentage_reference_label` with `% of Basic`.
- `TEXT`, `BOOLEAN`, `OTHER_CONFIGURED` → unchanged.

### Basic
Every organization already has one component with `component_code = "BASIC"`, `is_system = true`, type `EARNING`, `FIXED_AMOUNT`, `MONTHLY`. Rules to reflect in the UI:
- Hide/disable **Deactivate** for it (`POST …/deactivate` returns `422`).
- Only `component_name`, `display_name`, `description`, `display_order` can be edited (other patch fields return `422`).
- Creating another component coded `BASIC` returns `409`.
- Always list it first and never offer it as removable.

---

## 2. Template builder

- A template version **always contains Basic**. After `POST /salary-templates` or `PUT …/versions/{vid}/components`, the returned `mappings` include Basic even if you did not send it (with `default_value_amount: null` if you gave no amount). Pre-select it in the builder, lock its row (no remove button) and show only an **amount** input for it.
- Other rows: let the user tick components from the catalogue. For `PERCENTAGE` components send `default_value_percentage`; for `FIXED_AMOUNT` send `default_value_amount`.
- **"Add new component" inside the builder** (e.g. HRA 15%) is two calls from the frontend:
  1. `POST /salary-components` with `{ "component_code": "HRA", "component_name": "House rent allowance", "display_name": "HRA", "component_type": "EARNING", "value_type": "PERCENTAGE" }`
  2. Add the returned `id` to the mappings with `default_value_percentage: "15"` and save/publish as usual.

  If step 2 fails, the component still exists in the catalogue (an unused component is harmless and can be deactivated); reuse it on retry rather than creating it again (`409` on a duplicate code).

---

## 3. Employee salary: template or direct selection

`POST /employees/{id}/salary-assignments` (and `PATCH`, `replace`, `correct`) now supports two equal paths:

**A. From a template**: send `template_id` and, optionally, `components` overrides (unchanged).

**B. Without a template**: omit `template_id` and send catalogue components:
```json
{
  "effective_from": "2026-11-02",
  "revision_reason": "NEW_JOINER",
  "components": [
    { "component_id": "<BASIC id>", "value_amount": "40000.00" },
    { "component_id": "<HRA id>",   "value_percentage": "15" }
  ],
  "confirm": true
}
```
Previously path B was refused unless the organization had `allow_custom_salary_components` and the user had `salary.custom_component`. **That gate now applies only to ad-hoc items** (no `component_id`, you invent the code/name). Picking from the catalogue needs only `salary.revision_create`. Ad-hoc items may also carry `display_name` and `component_type`; the code `BASIC` is rejected for them.

**The employee's salary is a copy.** Once saved, editing or deactivating a component, or publishing a new template version, never changes it. Use the revision flow (unchanged) to change an employee's salary.

---

## 4. Reading an assignment

Each object in `components[]` gains:
| Field | Meaning |
|---|---|
| `display_name` | Snapshot of the employee-facing name. **Show this to employees.** |
| `component_type` | `EARNING` / `DEDUCTION` snapshot. Group or colour by it. |
| `computed_amount` | For `PERCENTAGE` components only: the stored amount, `percentage x Basic` rounded half-up to 2 dp (e.g. 15% of `33333.33` → `"5000.00"`). `null` otherwise. |

`value_percentage` keeps the entered percentage (`"15.0000"`); show `"15% of Basic"` and `computed_amount` side by side. The server calculates nothing else: **no gross, net or totals**, so don't present any server-backed ones. If you add a client-side total, label it as a display convenience.

Revision history, timeline, cancel/confirm/approve, and `compare` behave exactly as before. `compare` still compares `value_*` fields by code (it does not look at `computed_amount`).

---

## 5. Stricter validation (the only breaking part)

| Situation | Response |
|---|---|
| Assignment has no Basic, or Basic has no `value_amount` (after applying template defaults and overrides) | `422`, detail code `BASIC_REQUIRED` |
| A `PERCENTAGE` component has no percentage | `422`, detail code `PERCENTAGE_REQUIRED` |
| Ad-hoc component with code `BASIC` | `422` |
| Deactivate Basic | `422` |
| Create a component with code `BASIC` | `409` |

Templates whose latest version predates this change gain a Basic mapping the next time a version is created or its mappings are replaced. Until then, assigning from such an existing published template fails with `BASIC_REQUIRED` (Basic is not part of that version and cannot be added as an override). Prompt the user to create and publish a new version of the template, or assign without a template (§3B).

---

## 6. Suggested UI checklist
- [ ] Component form: add Display name, Type (Earning/Deduction); "Calculation" = Fixed amount / % of Basic.
- [ ] Component list: badge for system components; hide delete/deactivate for Basic.
- [ ] Template builder: Basic pre-added and locked (amount only); "Add component" inline form → create + map.
- [ ] Employee salary form: choose template **or** pick components; require Basic amount; show live preview of `% of Basic` amounts (display only; the server's `computed_amount` is authoritative).
- [ ] Employee/payslip views use `display_name`; admin views show `component_name` too.
