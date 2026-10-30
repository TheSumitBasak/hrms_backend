# Configuration Reference

Every setting is read from the environment (falling back to `.env`) and validated at startup
by `app/core/config.py`. **There is no production-usable default for anything secret**: a
deployment that omits a key fails to boot rather than running with a guessable one.

Inspect the effective configuration with `python -m app.cli show-config`, which replaces
every secret with `[set]` / `[unset]`.

---

## Startup refusals

The application will not start if:

- `SECRET_KEY` is a known placeholder (`change-me`, `secret`, …) or shorter than 32 characters;
- `ENCRYPTION_MASTER_KEY` or `BLIND_INDEX_MASTER_KEY` is absent, not valid base64, or does not
  decode to exactly 32 bytes, while `KEY_PROVIDER=env`;
- the two keys are **identical** — §23.7 requires blind-index keys separate from encryption keys;
- `DATABASE_URL` is not a MySQL driver URL (ADR-002);
- `KEY_PROVIDER=kms` without `KMS_KEY_ID`;
- `STORAGE_BACKEND=s3` without both bucket names;
- `MALWARE_SCANNER_BACKEND=external` without `MALWARE_SCANNER_URL`;
- the `OIDC_*` trio is partially configured;
- and, when `ENVIRONMENT` is `staging` or `production`, any of: `DEBUG=true`, `DB_ECHO=true`,
  `KEY_PROVIDER != kms`, `STORAGE_BACKEND != s3`, `MALWARE_SCANNER_BACKEND != external`,
  local login enabled without OIDC, `*` in `CORS_ORIGINS`, or `SEED_ADMIN_PASSWORD` set
  (the first-run bootstrap is development only, §19).

---

## Settings

### Application

| Variable | Default | Notes |
|---|---|---|
| `APP_NAME` | `Employee Management System` | |
| `ENVIRONMENT` | `local` | `local` · `test` · `staging` · `production` |
| `DEBUG` | `false` | Must be false in staging/production |
| `API_PREFIX` | `/api/v1` | §24.1 |
| `CORS_ORIGINS` | empty | Comma-separated. `*` forbidden in staging/production |

### Database

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — **required** | `mysql+pymysql://user:pass@host:3306/ems?charset=utf8mb4` |
| `DB_POOL_SIZE` | `10` | |
| `DB_MAX_OVERFLOW` | `20` | |
| `DB_POOL_RECYCLE_SECONDS` | `1800` | Below MySQL's `wait_timeout` |
| `DB_POOL_PRE_PING` | `true` | |
| `DB_LOCK_WAIT_TIMEOUT_SECONDS` | `10` | Exceeding it returns retryable `409 CONCURRENT_MODIFICATION` |
| `DB_ECHO` | `false` | Logs SQL **including bound parameters, which are personal data** (§25.9). Local only |

Each connection is configured on checkout: `READ-COMMITTED` isolation (§18.8), the strict
`sql_mode` set **appended** to the server's own (DEC-041), `time_zone = '+00:00'`, and the
lock-wait timeout.

### Tokens

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | — **required** | Signs local access tokens and pagination cursors |
| `ACCESS_TOKEN_TTL_SECONDS` | `900` | Capped at 900 by NFR-13 |
| `REFRESH_TOKEN_TTL_HOURS` | `12` | Absolute session cap, NFR-13 |
| `TOKEN_ALGORITHM` | `HS256` | `HS256` or `RS256` |
| `TOKEN_ISSUER` / `TOKEN_AUDIENCE` | `ems-backend` / `ems-api` | Non-secret metadata |
| `OIDC_ISSUER` · `OIDC_AUDIENCE` · `OIDC_JWKS_URL` | empty | All three together, or none. **Unconfigured here** |
| `OIDC_JWKS_CACHE_SECONDS` | `3600` | |
| `LOCAL_LOGIN_ENABLED` | `true` | Cannot satisfy NFR-12 MFA; configure OIDC for staging/production |

### Encryption and key management (§23.7)

| Variable | Default | Notes |
|---|---|---|
| `KEY_PROVIDER` | `env` | `env` (development) or `kms` (required in staging/production) |
| `ENCRYPTION_MASTER_KEY` | — | base64, exactly 32 bytes. Required when `KEY_PROVIDER=env` |
| `BLIND_INDEX_MASTER_KEY` | — | base64, exactly 32 bytes, **must differ** from the above |
| `ENCRYPTION_KEY_VERSION` | `1` | Stamped on every ciphertext; drives lazy re-encryption |
| `KMS_KEY_ID` · `KMS_ENDPOINT` | empty | Required when `KEY_PROVIDER=kms` |

**Key hierarchy.** §23.7 specifies envelope encryption: a data encryption key per
organization, wrapped by a customer master key in a managed KMS, with a version on every
ciphertext.

- `EnvKeyProvider` (development) derives each per-organization key from the master key with
  **HKDF-SHA256**, binding purpose, version and scope into the `info` parameter. This keeps the
  §23.7 *shape* without a KMS, but the master key lives in the process environment, which
  §23.7 and §25.9 forbid in production.
- `KmsKeyProvider` (production) is **not implemented** because no KMS exists here. It raises
  rather than falling back. To complete it: store one wrapped DEK per organization, implement
  `data_key` as "look up the wrapped DEK, call `kms.decrypt` once, cache the plaintext key in
  memory", do the same for `blind_index_key` against a **separate** key hierarchy, and keep
  every KMS network call outside database transactions (§24.7).

**Generating keys**

```bash
python -m app.cli generate-keys
```

Independently generated, so the "must differ" requirement holds by construction. Keys are
printed to stdout for an operator to place in a secrets manager; no command writes a key to
disk.

**Rotation (§23.7)**

| Key | Cadence | Procedure |
|---|---|---|
| KMS wrapping key | Annually `[ASSUMPTION]` | Rotate in the KMS; no application change |
| Data encryption keys | Annually, or on compromise | Increment `ENCRYPTION_KEY_VERSION`. New writes use the new version; existing rows stay readable and are re-encrypted lazily (`FieldCipher.needs_rotation`) |
| Blind-index keys | Only when required | **Requires recomputing every blind index**, because the digest changes. Planned maintenance with duplicate detection and identifier search unavailable during the rebuild |
| `SECRET_KEY` | On compromise | Invalidates outstanding pagination cursors and local access tokens; both are short-lived |

### Document storage (§15)

| Variable | Default | Notes |
|---|---|---|
| `STORAGE_BACKEND` | `local` | `local` (development) or `s3`. Required `s3` in staging/production |
| `STORAGE_LOCAL_ROOT` | `./var/private-storage` | **Must not be inside any statically served directory** |
| `STORAGE_S3_BUCKET` | empty | Private bucket for available documents |
| `STORAGE_S3_QUARANTINE_BUCKET` | empty | Separate bucket for unscanned uploads (§15.4) |
| `STORAGE_S3_REGION` · `STORAGE_S3_ENDPOINT` | empty | **No region is defaulted.** NFR-21/Q-09 assume India-only hosting; choosing it is a deployment decision |
| `UPLOAD_URL_TTL_SECONDS` | `900` | §15.4: 15 minutes |
| `DOWNLOAD_URL_TTL_SECONDS` | `300` | §15.6: 5 minutes |

### Malware scanning (A-06)

| Variable | Default | Notes |
|---|---|---|
| `MALWARE_SCANNER_BACKEND` | `noop` | `noop` accepts without scanning — **development only** |
| `MALWARE_SCANNER_URL` | empty | Required when `external` |
| `MALWARE_SCANNER_TIMEOUT_SECONDS` | `30` | Explicit timeout (§18 of the brief) |

### Behaviour limits

| Variable | Default | Specification |
|---|---|---|
| `PERMISSION_CACHE_TTL_SECONDS` | `60` | §5.5 ceiling |
| `IDENTIFIER_REVEAL_RATE_PER_HOUR` | `30` | §24.3 `[ASSUMPTION]`, DEC-025 |
| `IDEMPOTENCY_RETENTION_HOURS` | `24` | §24.1 |
| `DEFAULT_PAGE_SIZE` / `MAX_PAGE_SIZE` | `25` / `100` | §26.5 |
| `EXACT_TOTAL_COUNT_LIMIT` | `10000` | §26.5 |
| `CURSOR_TTL_SECONDS` | `86400` | §26.5 |
| `AUDIT_MAX_QUERY_DAYS` | `366` | §24.4.8 |

### Logging

| Variable | Default | Notes |
|---|---|---|
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `json` | `json` in deployment; `text` for local reading |

Logs are structured, carry a correlation id, and contain **no personal data**; request bodies
are never logged (§25.9). A redaction filter additionally scrubs bearer tokens, long digit runs
and known sensitive key names. `sqlalchemy.engine` is held at `WARNING` unless `DB_ECHO` is on,
because statement logs include bound parameters.

### First-run bootstrap (development only)

| Variable | Default | Notes |
|---|---|---|
| `ORGANIZATION_NAME` | unset | Name of the organization created on an empty database. The `organization_code` is derived from it (§7.2) |
| `SEED_ADMIN_EMAIL` | unset | Email of the first administrator (§21.1 V-EMAIL) |
| `SEED_ADMIN_PASSWORD` | unset | At least 12 characters (`MIN_PASSWORD_LENGTH`). Refused in staging/production |

Read **only** by `python -m app.cli seed` and `python -m app.cli bootstrap`, never in the
request path. All three must be set, or nothing happens.

The bootstrap acts only on a database that holds **no users and no organizations**. There it
creates one organization, its §24.4.1 seed data, one `LOCAL` user and three
`user_organization_access` rows — SSA, OA and HRA, whose union is every permission in the
§5.3 matrix, so "all access" needs no per-organization override row (DEC-004) and no bypass
in the permission resolver. Once any user or organization exists the command reports
`Bootstrap skipped` and writes nothing, which makes it safe on every deployment.

Two things are deliberately incomplete afterwards: the organization's `entity_type` is `OTHER`
and its registered address is a placeholder, because configuration supplies only a name and
§7.1 makes those columns mandatory. Correct them with `PATCH /organizations/{id}` before
entering real data, and change the administrator's password at first sign-in.

---

## Secret handling

- Secrets live in a managed secrets store, never in a committed file, an image or a log
  (§25.9). `.env` is gitignored.
- `.env.example` contains placeholders only.
- No command prints a secret value; `show-config` prints `[set]` / `[unset]`.
- `SecretStr` wrapping keeps secrets out of tracebacks and `repr` output.
