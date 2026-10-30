# Deployment

## ASGI server

The application is exposed as a **factory**, not a module-level instance, so importing
`app.main` does not validate the environment as a side effect.

Development:

```bash
uvicorn app.main:create_app --factory --reload
```

Production:

```bash
gunicorn "app.main:create_app()" \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers 4 \
  --bind 0.0.0.0:8000 \
  --timeout 60 \
  --graceful-timeout 30 \
  --max-requests 2000 --max-requests-jitter 200 \
  --access-logfile -
```

### Sizing

Endpoints are synchronous `def` handlers executed in Starlette's threadpool (ADR-001), so
concurrency is bounded by **both** the threadpool and the database pool. Keep them aligned:

```
per-worker capacity ≈ min(threadpool size, DB_POOL_SIZE + DB_MAX_OVERFLOW)
total DB connections = workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW)
```

Check `total DB connections` against the server's `max_connections` before increasing
workers. With the defaults (`DB_POOL_SIZE=10`, `DB_MAX_OVERFLOW=20`) four workers can open 120
connections.

`--max-requests` recycles workers periodically, which bounds the impact of any slow leak.

---

## Pre-deployment checklist

### Configuration

- [ ] `ENVIRONMENT=production` (the application then refuses several unsafe settings — see
      `CONFIGURATION.md`)
- [ ] `SECRET_KEY`, `ENCRYPTION_MASTER_KEY`, `BLIND_INDEX_MASTER_KEY` supplied from the
      secrets store, never from a file in the image
- [ ] `KEY_PROVIDER=kms` with `KMS_KEY_ID` set — **and the `KmsKeyProvider` integration
      completed** (it is not implemented; see `STATUS.md`)
- [ ] `STORAGE_BACKEND=s3` with both the private and quarantine buckets, and
      `STORAGE_S3_REGION` chosen deliberately for data residency (NFR-21, Q-09)
- [ ] `MALWARE_SCANNER_BACKEND=external` with a reachable `MALWARE_SCANNER_URL`
- [ ] `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL` configured, and MFA enforced at the
      provider for SSA, OA, HRA and RDR (NFR-12)
- [ ] `LOCAL_LOGIN_ENABLED=false` once OIDC is live
- [ ] `CORS_ORIGINS` set to exact origins, no `*`
- [ ] `DEBUG=false`, `DB_ECHO=false`, `LOG_FORMAT=json`
- [ ] `python -m app.cli show-config` reviewed; every secret shows `[set]`

### Database

- [ ] MySQL 8.0+ with `utf8mb4` / `utf8mb4_0900_ai_ci`
- [ ] A dedicated application user with the **least privilege** it needs
- [ ] `audit_logs`: grant `INSERT` and `SELECT` **only** — `UPDATE` and `DELETE` revoked
      (§20.1). This is a grant, not an application setting, and it is what makes the audit
      trail append-only in fact rather than by convention:
      ```sql
      REVOKE UPDATE, DELETE ON ems.audit_logs FROM 'ems_app'@'%';
      ```
- [ ] `wait_timeout` comfortably above `DB_POOL_RECYCLE_SECONDS`
- [ ] Backup taken **before** migrating — MySQL DDL is not transactional
- [ ] `alembic upgrade --sql head` reviewed, then `alembic upgrade head`
- [ ] `alembic current` confirms the expected revision

### Edge and transport

- [ ] TLS 1.2+ (1.3 preferred) terminated at the edge; HTTP redirected (NFR-11)
- [ ] HSTS passed through (the application sets it; the edge must not strip it)
- [ ] Request body size limit at the edge, consistent with the largest document category
      (NFR-18: up to 25 MB)
- [ ] Rate limiting at the edge for authentication, reveal and download endpoints (§25.9),
      in addition to the application's own reveal limit
- [ ] `/health` and `/ready` reachable by the orchestrator but not publicly routable
- [ ] Decide whether `/docs`, `/redoc` and `/openapi.json` should be publicly routable. The
      application serves them in every environment. The two UI pages carry a wider
      Content-Security-Policy than the API (`DOCS_CONTENT_SECURITY_POLICY` in
      `app/api/middleware.py`) because Swagger UI and ReDoc load their bundles from jsDelivr
      and boot from an inline script; `default-src 'none'` blocks both. If the UI must be
      exposed, vendoring the two bundles and serving them from this origin removes the CDN
      and `'unsafe-inline'` from that policy; blocking the three paths at the edge is the
      other option. Every other response keeps the strict policy.

### Observability

- [ ] JSON logs shipped to the log platform; **verify no personal data appears** — spot-check
      for identifier values, salary figures and contact details (§25.9)
- [ ] Alerts on 5xx rate, `409 CONCURRENT_MODIFICATION` rate (lock contention), readiness
      failures, and the nightly integrity job reporting any finding
- [ ] Correlation ids retained long enough to be useful for support

### Scheduled jobs (§24.7)

Run as separate processes, not inside a web worker:

| Job | Schedule |
|---|---|
| Salary status refresh | 00:05 organization local time |
| ID-rule status refresh | 00:05 organization local time |
| Pending-upload cleanup | hourly |
| Quarantine purge | hourly |
| Retention purge | daily 02:00 IST — **only executes an explicitly approved purge batch** |
| Integrity checker | nightly — reports overlap and orphan violations as P1 |

The integrity checker is not optional on MySQL: it is part of the compensating control for
NFR-06 (`ARCHITECTURE.md` §7).

### Verification after deploy

- [ ] `/ready` returns `status: ready`, `database: up`
- [ ] A read endpoint returns an `ETag`; a stale `If-Match` returns `412`
- [ ] A cross-organization id returns `404`, and the attempt appears in the audit log
- [ ] An error response carries a correlation id that resolves to a log entry
- [ ] An audit `UPDATE` attempted manually as the application user is **refused**

---

## Rollback

1. Roll the application back to the previous image.
2. Roll the schema back **only** if the previous application cannot run against the current
   schema. `alembic downgrade -1` reverses structure, not data; a revision that drops a column
   loses that data irreversibly, which is why forward-compatible migrations (add nullable,
   backfill, switch reads, drop later) are strongly preferred.
3. If a migration failed part-way, remember MySQL DDL is not transactional: compare
   `alembic current` against the schema before retrying.

---

## Backup and restore

Per NFR-08/NFR-09: RPO ≤ 15 min, RTO ≤ 4 h, daily full backups plus continuous binlog for
point-in-time recovery, retained 35 days, encrypted, with a quarterly restoration test. All of
these are flagged `[ASSUMPTION]` in §3.2 and need product-owner confirmation.

Two things a conventional database backup does **not** cover:

1. **Key material.** `[E]` fields are AES-256-GCM ciphertext. A database restored without its
   KMS keys is unreadable — by design. Key backup and recovery is a separate, tested
   procedure.
2. **Document bytes.** Only metadata lives in the database. Object storage needs its own
   backup and its own residency guarantee.

Restore drill: restore the database to a point in time, restore or re-grant key access, verify
a document download and a sensitive-field reveal both succeed, and confirm
`alembic current` matches the application version.
