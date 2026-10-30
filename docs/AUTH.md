# Authentication and Authorization

Implements §5 (roles and permissions), §24.1 (bearer tokens) and §25.5 (access control and
revocation). Read alongside `ARCHITECTURE.md` §6.

---

## 1. Authentication

Bearer tokens on every request: `Authorization: Bearer <token>`.

Two token sources resolve to the same `TokenClaims`, so nothing downstream of
`get_current_user` knows which was used:

| Source | State | Notes |
|---|---|---|
| **OIDC / SAML identity provider** | **Not configured in this environment.** `OidcTokenVerifier` raises until `OIDC_ISSUER`, `OIDC_AUDIENCE` and `OIDC_JWKS_URL` are set. | The primary strategy (§24.1, A-04). To finish: fetch the JWKS, cache it for `OIDC_JWKS_CACHE_SECONDS`, select the key by the token's `kid`, verify with RS256. The claim mapping is already shared with the local issuer. |
| **Local password accounts** | Implemented. | The documented fallback. bcrypt (cost 12) over a SHA-256 pre-hash, so passwords longer than bcrypt's 72-byte limit are not silently truncated. |

### Token lifecycle

- **Access tokens** are JWTs, TTL capped at **15 minutes** by configuration (NFR-13).
- **Refresh tokens** are opaque 256-bit random strings, **stored only as a SHA-256 hash**
  (DEC-005), rotated on use, with an absolute session cap of 12 hours.
- **Deactivating a user revokes every refresh token immediately** (§25.5). Access tokens
  cannot be revoked individually, which is why their lifetime is short.
- A refresh token presented as a bearer credential is rejected: `_claims_from_payload`
  requires `typ == "access"`.

### Roles and permissions are never read from the token

Claims carry only the subject, token id and validity window. Roles and permissions are
resolved **per request** from `user_organization_access`, so revoking a grant takes effect
within the §5.5 60-second permission-cache window rather than surviving until token expiry.

### MFA

NFR-12 requires MFA for SSA, OA, HRA and RDR. MFA is enforced **by the identity provider**,
not by this application; `users.mfa_enabled` records the state for the §25.5 access review.
Because local password login cannot provide MFA, `Settings` refuses a staging or production
configuration that enables local login without OIDC.

---

## 2. Authorization

### Permissions, not role names

`app/core/permissions.py` is the single source of truth: the §5.2 `Permission` enum and the
§5.3 role matrix. An endpoint declares what it needs:

```python
@router.get("/employees/{employee_id}")
def get_employee(ctx: OrgContext = Depends(require(Permission.EMPLOYEE_VIEW))) -> ...:
```

No endpoint branches on a role code, with three exceptions the specification itself defines by
role: the Department Manager reporting-tree scope and the Read-only Viewer / Restricted
Document Reviewer field sets (§5.4).

### Effective permissions

For one organization: the **union** of the permissions of every role granted there (§5.1),
plus any `C` cell the organization has enabled.

```
effective = ⋃ over granted roles of ( Y(role) ∪ ( C(role) ∩ enabled_for_org ) )
```

A `C` cell can only ever grant a permission that is a `C` cell *for that role*, so a stale or
tampered override row cannot widen a role beyond the §5.3 matrix. This is asserted in
`tests/unit/test_permissions.py::test_override_cannot_grant_a_permission_outside_the_matrix`.

### Organization scope

Every organization-scoped resource lives under `/api/v1/orgs/{org_id}/…`. `OrgContext` is
resolved from the **path** and the caller's active grants — never from a request body.

| Outcome | Meaning |
|---|---|
| `403 ORG_ACCESS_DENIED` | The caller holds no grant for this `{org_id}` |
| `404 RESOURCE_NOT_FOUND` | The id is not in an organization the caller can access — **including when the row exists elsewhere** (§24.3: no existence disclosure) |

Both are audited; a cross-organization attempt writes `CROSS_ORG_ACCESS_DENIED` (DEC-040).

### Scoped roles

- **Department Manager** sees only direct and indirect reportees through
  `reporting_manager_employee_id` (depth ≤ 20) plus employees of a department whose
  `head_employee_id` is the DM's own record. A DM who *also* holds an unscoped role is not
  scoped, because §5.1 makes permissions a union.
- **Read-only Viewer** sees the same field set organization-wide.
- **Restricted Document Reviewer** sees a minimal profile plus documents and masked
  identifiers.

### Field-level access

`§5.5`: restricted fields are **omitted from the payload, never returned as `null`**. Each
resource has an explicit view builder mapping `(row, permission_set, role_set) → dict`
(ADR-005) rather than a reflective serialiser, so what each role can see is auditable and
directly testable. Masked fields render the §11.6 formats from the stored `value_last_four`
with **no decryption**.

### System Super Admin has no employee data access

By design, `ROLE_PERMISSIONS[SSA]` contains no employee-data permission, and no `C` cell
exists for SSA — so enabling every configurable permission still grants none. Employee data is
reachable only through **break-glass**: `POST /break-glass` mints a time-bound grant
(≤ 240 minutes) with a mandatory reason (≥ 20 characters). The grant is an ordinary expiring
HRA grant (DEC-007), so every downstream permission check and audit entry works unchanged;
only its provenance differs. The grant and every access made under it are audited into the
target organization's log.

### Maker-checker

- Salary approval: `approved_by` must differ from both `created_by` and `submitted_by`
  (§17.4) → `403 SELF_APPROVAL_NOT_ALLOWED`.
- Age exception: the decider must differ from the requester (§10.4).

---

## 3. Rate limiting

Reveal endpoints are limited to `IDENTIFIER_REVEAL_RATE_PER_HOUR` (default 30) per user per
organization → `429 RATE_LIMITED` with `Retry-After` (DEC-025). Authentication and download
endpoints are rate limited per §25.9.

---

## 4. Implementation status

| Element | State |
|---|---|
| Permission matrix, scoping rules, field sets | Implemented and tested |
| Password hashing, token issue/verify, claim mapping | Implemented |
| OIDC verification | **Seam only — not configured** |
| `users`, grants, refresh tokens, break-glass endpoints | **Implemented and tested** |
| Request dependencies (`get_current_user`, `OrgContext`, `require`) | **Implemented; every organization router resolves `OrgContext` from the path** |
| Identifier/bank reveal authorization and audit | **Implemented**: `identity.reveal` / `bank.reveal`, mandatory reason, audit row, configurable hourly limit (`IDENTIFIER_REVEAL_RATE_PER_HOUR`) |
| Organization-scoped routers | **Implemented**: §24.4.1, §24.4.2, §24.4.3, §24.4.4 (identifiers, bank), §24.4.7, §24.4.8 audit reads |
