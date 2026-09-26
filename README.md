# Backend

Django + SQLite manuscript review API.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python scripts\generate_admin.py --username admin
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Copy the generated `ADMIN_*` values into `.env`, install Ollama, and run `ollama pull qwen2.5:0.5b-instruct`. The review engine uses the local Ollama API; no cloud AI API key is required. The default 4096-token context is intentionally conservative for an 8 GB RAM development machine.


## Scholarly network venue setup

The multi-venue author workflow uses versioned `VenueAgentConfig` records. For the current Flexee Publishing outlets, seed the canonical starting configurations after migrations:

```powershell
python manage.py seed_flexee_venues
```

This creates **Field Notes Journal** and **Five Zero Books** under **Flexee Publishing** if they do not already exist. The command is idempotent and preserves existing configuration versions. To intentionally create a new active version from the canonical repository criteria:

```powershell
python manage.py seed_flexee_venues --refresh
```

Editors can then maintain venue metadata, create new configuration versions, reactivate older versions, review venue-specific submissions, record venue-scoped feedback, and make human editorial decisions from the protected admin workspace.

## Production

Production deployment requires two processes sharing the same `DATABASE_URL` (or using the same SQLite database file):

1. The Gunicorn/API process serving HTTP requests
2. The Django-Q worker process running `python manage.py qcluster`

See `flexee-qcluster.service` for an example systemd unit configuration for the worker process.

## Production-style scholarly-network E2E

Use the live E2E harness only after the API and Django-Q worker are running against the same database. It exercises the real author APIs, queued qcluster jobs, venue matching/assessment, packet submission, temporary editor TOTP login, editor feedback, and a human editorial decision.

Prerequisites:

```powershell
python manage.py migrate
python manage.py seed_flexee_venues
python manage.py runserver 127.0.0.1:8000
```

In a second backend terminal:

```powershell
python manage.py qcluster
```

For an Ollama run, set the backend `.env` explicitly and restart both the API and qcluster after editing it:

```env
AI_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen2.5:0.5b-instruct
COOKIE_SECURE=false
FRONTEND_ORIGINS=http://127.0.0.1:5173
```

Then run, using a fresh test email address:

```powershell
python manage.py production_e2e --manuscript "C:\path\to\field-notes-test.docx" --author-email "yourname+flexee-e2e-ollama@example.com" --manuscript-type practitioner_article --venue-slug field-notes-journal
```

For Anthropic, change `.env`, restart both API and qcluster, and run the same manuscript again with a different fresh email:

```env
AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=your_real_key
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
```

```powershell
python manage.py production_e2e --manuscript "C:\path\to\field-notes-test.docx" --author-email "yourname+flexee-e2e-anthropic@example.com" --manuscript-type practitioner_article --venue-slug field-notes-journal
```

The command requires real SMTP by default. Use `--allow-console-email` only for a local rehearsal; it is not evidence of production email delivery. The report is written as `production_e2e_<provider>_<timestamp>.json` and includes stage timings, observed model names, final decision state, evidence/feedback counts, email-path checks, and the AI usage/cost rows recorded during the E2E window.

To exercise the book path, use a real Five Zero-style book or ZIP and run:

```powershell
python manage.py production_e2e --manuscript "C:\path\to\five-zero-book.zip" --author-email "yourname+flexee-book-ollama@example.com" --manuscript-type book --venue-slug five-zero-books
```

Use the same book again with `AI_PROVIDER=anthropic` to compare wall-clock timings and the instrumented token/cost section in the JSON report. Run this proof in a dedicated environment when you need an isolated cost figure; concurrent application AI calls that occur inside the same E2E time window are included in that report.

## Per-venue content retention

Venue Agent configurations can set an optional `retention_days` value. Blank means the venue does not automatically expire content. When an author formally submits a packet, the active configuration's retention window is snapshotted onto that `VenueSubmission`, so later configuration changes do not rewrite historical retention dates.

Install the hourly Django-Q retention schedule after migrations:

```powershell
python manage.py install_retention_schedule
```

Keep `python manage.py qcluster` running. The retention sweep removes expired venue-specific packet/brief/evidence/requirement-file content. The shared manuscript file and semantic/readiness artifacts are removed only after every venue submission for that manuscript has been purged; an unexpired or no-expiry venue copy prevents premature deletion. Human editorial decision/status metadata is preserved.

For large installations, `RETENTION_SWEEP_BATCH_SIZE` controls the maximum expired submissions processed per hourly sweep (default 200).

## Production audit trail

The admin/editor workflow records append-only `AuditEvent` rows for manuscript/submission views, manuscript and requirement-file downloads, review starts, editor feedback, human decisions, venue creation/metadata changes, Venue Agent configuration creation/activation, legacy platform submission actions, SMTP configuration changes, and retention purges.

Audit records snapshot the editor/admin email and organization role at the time of the action. The request source is stored only as a keyed hash; raw IP addresses are not written to the audit table.

The protected endpoint is:

```
GET /api/admin/audit-events/
```

Organization users are automatically scoped to organizations in their memberships. Platform superusers can see platform-wide and unscoped events. The frontend exposes the same data under **Admin → Audit Log**.

After pulling this change, apply the migration:

```powershell
python manage.py migrate
```

## Production backup and restore

Production backups include both the PostgreSQL database and every file under `MEDIA_ROOT`. Each run creates one timestamped backup bundle containing:

- `database.dump` — PostgreSQL custom-format dump created by `pg_dump`
- `media.tar.gz` — uploaded manuscript and venue-requirement files
- `manifest.json` — SHA-256 checksums, sizes, media file count, creation time, release SHA, and verification state

The backup is written to a temporary directory and renamed into place only after the database dump, media archive, checksums, and archive verification succeed. Completed bundles older than `BACKUP_RETENTION_DAYS` are removed only after a new successful backup. Every attempt is also appended to `backup_attempts.jsonl`, and successful/failed backup/restore operations are added to the production audit trail when the application database is available.

### Production prerequisites

Install PostgreSQL client tools:

```bash
sudo apt update
sudo apt install postgresql-client
pg_dump --version
pg_restore --version
```

Configure a protected backup location outside `MEDIA_ROOT`. A mounted durable volume is preferable to the application filesystem:

```env
BACKUP_ROOT=/var/backups/flexee
BACKUP_RETENTION_DAYS=30
PG_DUMP_BIN=pg_dump
PG_RESTORE_BIN=pg_restore
RELEASE_SHA=
```

Create the directory and restrict access:

```bash
sudo mkdir -p /var/backups/flexee
sudo chown www-data:www-data /var/backups/flexee
sudo chmod 700 /var/backups/flexee
```

Run one backup manually first:

```bash
cd /var/www/flexee/flexee_admin_manuscript_backend
sudo -u www-data .venv/bin/python manage.py backup_production
```

A successful run prints the final bundle path and `Verified=True`.

Verify any completed bundle again without restoring it:

```bash
sudo -u www-data .venv/bin/python manage.py verify_production_backup \
  --backup /var/backups/flexee/flexee-backup-YYYYMMDDTHHMMSSZ-xxxxxxxx
```

### Daily systemd backup

The repository contains `flexee-backup.service` and `flexee-backup.timer`. Install them after confirming the paths/users match the server:

```bash
sudo cp flexee-backup.service /etc/systemd/system/
sudo cp flexee-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flexee-backup.timer
sudo systemctl list-timers flexee-backup.timer
```

Run an immediate scheduled-service test:

```bash
sudo systemctl start flexee-backup.service
sudo systemctl status flexee-backup.service --no-pager
sudo journalctl -u flexee-backup.service -n 100 --no-pager
```

### Required restore test

Checksum/archive verification is not a substitute for an actual restore. Periodically restore a recent backup into a separate disposable PostgreSQL database.

Create a restore-test database (example):

```bash
sudo -u postgres createdb flexee_restore_test
```

Use a dedicated restore target URL. Do **not** use the live production database URL:

```bash
export RESTORE_TARGET_DATABASE_URL='postgresql://flexee_user:YOUR_PASSWORD@127.0.0.1:5432/flexee_restore_test'
```

Restore and smoke-test the database:

```bash
.venv/bin/python manage.py restore_production_backup \
  --backup /var/backups/flexee/flexee-backup-YYYYMMDDTHHMMSSZ-xxxxxxxx \
  --target-database-url "$RESTORE_TARGET_DATABASE_URL" \
  --confirm-target-database flexee_restore_test
```

The restore command verifies the source bundle before touching the target, restores with `pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error`, then connects to the restored database and confirms that it is reachable and contains the Django migration table.

To test media restoration as well, use a disposable directory:

```bash
mkdir -p /tmp/flexee-media-restore-test

.venv/bin/python manage.py restore_production_backup \
  --backup /var/backups/flexee/flexee-backup-YYYYMMDDTHHMMSSZ-xxxxxxxx \
  --target-database-url "$RESTORE_TARGET_DATABASE_URL" \
  --confirm-target-database flexee_restore_test \
  --restore-media \
  --media-root /tmp/flexee-media-restore-test \
  --confirm-media-replace REPLACE_MEDIA
```

When media is replaced, an existing target directory is renamed to a timestamped `.pre-restore-` path rather than silently destroyed.

The restore command intentionally does not default to `DATABASE_URL`. If the requested target matches the database currently used by Django, restoration is refused unless `--allow-current-database` is explicitly supplied. That flag is intended only for an intentional disaster-recovery operation.

### Local Windows/SQLite rehearsal

The production path is PostgreSQL, but the backup/restore workflow can be rehearsed locally with SQLite without installing PostgreSQL client tools:

```powershell
python manage.py backup_production --allow-sqlite
```

The command prints a bundle path under `backups`. Verify it:

```powershell
python manage.py verify_production_backup --backup ".\backups\flexee-backup-..."
```

Restore that bundle into a separate SQLite file instead of overwriting your working database:

```powershell
python manage.py restore_production_backup `
  --backup ".\backups\flexee-backup-..." `
  --target-sqlite-path ".\restore-test.sqlite3" `
  --confirm-target-database "restore-test.sqlite3"
```

To include a media restore rehearsal:

```powershell
python manage.py restore_production_backup `
  --backup ".\backups\flexee-backup-..." `
  --target-sqlite-path ".\restore-test.sqlite3" `
  --confirm-target-database "restore-test.sqlite3" `
  --restore-media `
  --media-root ".\restore-media-test" `
  --confirm-media-replace REPLACE_MEDIA
```

Never treat an untested backup as production-ready. Keep at least one recent restore-test result with the backup evidence for the production checklist.

## Production error monitoring (Sentry)

The backend supports opt-in Sentry monitoring for Django HTTP failures, handled Django-Q job failures/timeouts, email-delivery failures, and backup/restore failures.

Monitoring is disabled unless `SENTRY_DSN` is configured. The integration is intentionally privacy-first for manuscript handling:

- request bodies are never sent
- request query strings, headers, cookies, and attached user data are removed
- stack-frame local variables are disabled
- exception messages are redacted before transmission
- breadcrumb message/data payloads are removed
- manual worker events attach only operational tags such as job type, component, operation, and decision state
- performance tracing is disabled by default

The application does not put manuscript text, prompts, author email addresses, SMTP credentials, passwords, TOTP secrets, session tokens, or API tokens into custom Sentry context.

Install dependencies after pulling the feature:

```bash
python -m pip install -r requirements.txt
```

Create a Sentry Python/Django project, copy its DSN into the production environment, and tag the deployed release:

```env
DJANGO_ENV=production
SENTRY_DSN=https://PUBLIC_KEY@YOUR_SENTRY_HOST/PROJECT_ID
SENTRY_ENVIRONMENT=production
SENTRY_RELEASE=<deployed-git-sha>
SENTRY_TRACES_SAMPLE_RATE=0
```

`SENTRY_RELEASE` falls back to `RELEASE_SHA` when it is blank. Do not commit a real DSN to the repository.

Restart both long-running application processes after changing the environment because the SDK is initialized when Django loads:

```bash
sudo systemctl restart flexee-gunicorn
sudo systemctl restart flexee-qcluster
```

Use the built-in command to confirm configuration without emitting an event:

```bash
python manage.py verify_error_monitoring
```

Expected production output includes:

```text
Sentry enabled: True
Environment: production
Privacy mode: request bodies/query strings/cookies/user data/local variables redacted
```

Then send exactly one synthetic verification event:

```bash
python manage.py verify_error_monitoring --send-event
```

The command flushes the SDK before exiting and prints the Sentry event ID. Confirm that the event appears in the configured Sentry project with the `component=operations` and `operation=sentry_verification` tags.

### What is monitored

Unhandled Django 5xx exceptions are captured by the Sentry Django integration. The application also explicitly captures failures that are intentionally handled in code and therefore would otherwise disappear from exception monitoring:

- public-review, semantic-readiness, semantic-matching, and venue-assessment Django-Q job failures
- unexpected crashes in scheduled Django-Q sweeper/retention tasks
- queue/processing timeout signals from the stuck-job sweeper
- author verification/submission-confirmation email failures
- editor decision and legacy submission email failures
- production backup and restore command failures

Expected client validation errors and normal deterministic/AI fallbacks are not deliberately reported as production errors.

### Production alert rule

Repository code sends the events; alert routing is configured in the Sentry project itself. For production, create an issue alert for new/regressed error-level issues and route it to the team notification channel (email, Slack, or the incident system in use). Keep the Sentry project's server-side data scrubbing enabled as a second layer in addition to the application's outbound redaction.

Performance tracing is intentionally off by default. If it is later needed, set `SENTRY_TRACES_SAMPLE_RATE` to a small value such as `0.05` only after reviewing the resulting event payloads in a non-production environment.

## Production queue health monitoring and alerts

The protected endpoint `GET /api/admin/queue-health/` now reports more than queue depth. Platform superusers can inspect:

- overall status: `healthy`, `degraded`, or `critical`
- queued and processing job counts
- age of the oldest queued job
- age of the oldest processing job
- recent completed and failed jobs
- active queue counts grouped by job type
- the thresholds currently in effect
- structured issue codes explaining why the queue is degraded or critical

The original `queued_jobs` and `oldest_job_age_seconds` fields remain in the response for backward compatibility.

Default thresholds are designed to warn before the existing stuck-job sweeper marks work failed:

```env
QUEUE_HEALTH_WARNING_QUEUED_JOBS=10
QUEUE_HEALTH_CRITICAL_QUEUED_JOBS=25
QUEUE_HEALTH_WARNING_OLDEST_QUEUED_SECONDS=300
QUEUE_HEALTH_CRITICAL_OLDEST_QUEUED_SECONDS=600
QUEUE_HEALTH_WARNING_OLDEST_PROCESSING_SECONDS=1350
QUEUE_HEALTH_CRITICAL_OLDEST_PROCESSING_SECONDS=1800
QUEUE_HEALTH_FAILURE_WINDOW_MINUTES=60
QUEUE_HEALTH_WARNING_RECENT_FAILURES=3
QUEUE_HEALTH_CRITICAL_RECENT_FAILURES=10
QUEUE_HEALTH_ALERT_COOLDOWN_MINUTES=30
```

The queued/processing age defaults correspond to the existing 10-minute queue timeout and 30-minute processing timeout. Tune queue-depth and failure thresholds after observing normal production traffic rather than raising them simply to suppress alerts.

### Manual queue check

Run:

```bash
python manage.py check_queue_health
```

Example healthy output:

```text
Queue status: healthy
Queued=0 Processing=0 RecentFailed=0 RecentCompleted=0
OldestQueuedSeconds=None OldestProcessingSeconds=None
No queue health issues detected.
```

For the full machine-readable snapshot:

```bash
python manage.py check_queue_health --json
```

To send a deduplicated monitoring alert when the queue is unhealthy:

```bash
python manage.py check_queue_health --alert
```

Alerts are written to the production audit trail and, when `SENTRY_DSN` is configured, emit a privacy-safe Sentry signal. Identical unhealthy states are suppressed for `QUEUE_HEALTH_ALERT_COOLDOWN_MINUTES`; severity/issue changes alert immediately. The first healthy check after an active alert emits one recovery event.

### Independent production monitor

Do not schedule the queue-health check inside Django-Q: if qcluster itself stops, a Django-Q scheduled monitor would stop too. The repository therefore includes an independent systemd monitor:

- `flexee-queue-health.service`
- `flexee-queue-health.timer`

The timer invokes the health command every five minutes outside qcluster. An unhealthy queue emits the alert and returns a non-zero status so the failure is also visible in systemd/journal logs.

Install it after confirming the deployment paths and service account:

```bash
sudo cp flexee-queue-health.service /etc/systemd/system/
sudo cp flexee-queue-health.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flexee-queue-health.timer
sudo systemctl list-timers flexee-queue-health.timer
```

Test it immediately:

```bash
sudo systemctl start flexee-queue-health.service
sudo systemctl status flexee-queue-health.service --no-pager
sudo journalctl -u flexee-queue-health.service -n 100 --no-pager
```

A healthy queue exits successfully. A degraded/critical queue intentionally makes the one-shot service exit non-zero after recording/emitting the alert; the timer continues checking on subsequent intervals.

For production notification delivery, configure the Sentry alert rule created during the error-monitoring readiness step to route `component=django_q`, `operation=queue_health_alert` events to the team notification channel.

## AI usage tracking and cloud cost ceilings

Every call made through `review.services.ai_provider.ai_chat_json` is now accounted for in `AIUsageEvent`. The accounting layer records only operational metadata:

- provider and model
- application operation (for example semantic readiness, semantic matching, venue assessment, manuscript review, or output repair)
- input/output/total token counts
- whether token usage had to be estimated
- configured-price cost estimate
- completed, failed, reserved, or budget-blocked status

It does **not** store prompts, model responses, manuscript text, author details, API keys, or provider credentials.

Local Ollama calls are tracked with zero provider cost by default. Anthropic usage uses the provider-reported input/output token counts when available. Ollama uses `prompt_eval_count` and `eval_count` when returned by the local server. If a provider/mock does not return usage, the row is marked `usage_estimated=true`.

### Pricing is configuration, not hard-coded application logic

Provider prices can change and may differ by contract, so production prices are deliberately configured rather than embedded in source code. Before enabling a cloud monetary ceiling, enter the rates that apply to the deployed model:

```env
AI_ANTHROPIC_INPUT_USD_PER_MILLION=<current input rate>
AI_ANTHROPIC_OUTPUT_USD_PER_MILLION=<current output rate>
```

Model-specific pricing can override provider defaults:

```env
AI_MODEL_PRICING_JSON={"your-model-id":{"input_usd_per_million":"<rate>","output_usd_per_million":"<rate>"}}
```

Use the rates from the provider account/billing documentation at deployment time. Do not copy example or historical pricing into production.

### Enable the spending ceiling

Choose limits appropriate for the deployment and enable enforcement:

```env
AI_COST_ENFORCEMENT_ENABLED=true
AI_DAILY_COST_LIMIT_USD=<daily limit>
AI_MONTHLY_COST_LIMIT_USD=<monthly limit>
AI_BUDGET_RESERVATION_TTL_MINUTES=60
```

At least one of the daily/monthly limits must be greater than zero when cloud enforcement is enabled. A cloud model must also have non-zero configured pricing. If enforcement is enabled but pricing/limits are incomplete, a direct cloud call fails closed instead of silently becoming unbounded.

Before a billable Anthropic request is sent, the backend:

1. takes the global `AIBudgetState` database lock;
2. calculates completed spend plus active reservations for the current day/month;
3. reserves a deliberately conservative maximum request cost using an input-token upper bound plus the requested maximum output tokens;
4. blocks the provider call if that reservation would cross a configured ceiling;
5. replaces the reservation with provider-reported actual token usage/cost after a successful call.

The lock serializes reservations across production workers so simultaneous requests cannot all pass the same remaining-budget check. Reservations older than `AI_BUDGET_RESERVATION_TTL_MINUTES` stop counting against the ceiling so a worker crash cannot hold budget forever.

When `AI_PROVIDER=auto`, a cloud call blocked by the monetary ceiling falls back to the configured local Ollama provider. A forced/direct Anthropic call remains blocked.

### Verify usage and limits

After pulling the migration:

```bash
python manage.py migrate
```

Show the current totals:

```bash
python manage.py check_ai_usage
```

Show the machine-readable report:

```bash
python manage.py check_ai_usage --json
```

For a production deployment that can reach Anthropic, use:

```bash
python manage.py check_ai_usage --fail-if-unbounded
```

The command exits non-zero if cloud use is possible but monetary enforcement, a daily/monthly ceiling, or model pricing is missing. This is suitable for a deployment verification step.

Platform superusers can also retrieve the same safe aggregate data through:

```text
GET /api/admin/ai-usage/
```

The response includes today's, current month's, and all-time calls/tokens/cost; committed and remaining configured daily/monthly budget; active reservations; failed/blocked call counts; unpriced cloud-call visibility; and totals grouped by provider/model and application operation. The endpoint uses `Cache-Control: no-store`.

A budget-block event also emits the privacy-safe Sentry operation `component=ai_cost`, `operation=budget_block` when Sentry is configured.

## Private manuscript storage and production security hardening

Production manuscript and submission-item uploads are treated as private application data. They are not intended to be served by Django static/media routes, Nginx aliases, a public S3 bucket, or any other unauthenticated file URL.

### Production startup requirements

When `DJANGO_ENV=production`, startup now fails if any of these conditions are unsafe:

- `DJANGO_ALLOWED_HOSTS` is missing or contains `*`
- `FRONTEND_ORIGINS` is missing or contains a non-HTTPS origin
- `ADMIN_SESSION_SECRET` is missing
- `SECURE_SSL_REDIRECT=false`
- `TRUSTED_PROXIES=*`
- `PRIVATE_MEDIA_ROOT` is missing
- `PRIVATE_MEDIA_ROOT` resolves inside the application source tree

Production also forces secure application cookies, HSTS, `X-Content-Type-Options: nosniff`, and a same-origin referrer policy.

A typical filesystem deployment should use a private directory outside `/var/www`, for example:

```bash
sudo install -d -m 0750 -o www-data -g www-data /var/lib/flexee-private-media
```

Then configure:

```env
DJANGO_ENV=production
DJANGO_ALLOWED_HOSTS=api.example.com
FRONTEND_ORIGINS=https://app.example.com
ADMIN_SESSION_SECRET=<strong-random-secret>
PRIVATE_MEDIA_ROOT=/var/lib/flexee-private-media
SECURE_SSL_REDIRECT=true
```

If TLS terminates at Nginx and Nginx proxies plain HTTP to Django, configure Nginx to set the original scheme:

```nginx
proxy_set_header X-Forwarded-Proto $scheme;
```

and enable:

```env
TRUST_X_FORWARDED_PROTO=true
TRUSTED_PROXIES=127.0.0.1
```

Only list actual trusted proxy addresses. Production rejects `TRUSTED_PROXIES=*` because trusting arbitrary `X-Forwarded-For` values would weaken rate limiting and audit-address hashing.

### Do not expose the upload directory

There is intentionally no Django URL pattern for `MEDIA_ROOT`. The configured `MEDIA_URL` is a private placeholder, not a public route.

Do **not** add Nginx configuration such as:

```nginx
location /media/ {
    alias /var/lib/flexee-private-media/;
}
```

For defense in depth, a reverse proxy can explicitly deny conventional media paths:

```nginx
location ^~ /media/ {
    return 404;
}

location ^~ /__private_media__/ {
    return 404;
}
```

Editors download files only through authenticated API endpoints. Those responses are forced to attachments and include `Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and a sandbox CSP.

If private object storage is introduced later, use a private bucket/storage backend and preserve these authenticated application download controls or equivalent short-lived signed access. Do not make the manuscript bucket public.

### Upload and archive limits

Top-level manuscript and requirement-file limits remain enforced server-side:

```env
MAX_MANUSCRIPT_BYTES=20971520
MAX_SUBMISSION_ITEM_BYTES=10485760
```

Manuscript ZIPs also have centralized validation before persistence and again before worker extraction:

```env
MANUSCRIPT_ZIP_MAX_FILES=40
MANUSCRIPT_ZIP_MAX_UNCOMPRESSED_BYTES=52428800
MANUSCRIPT_ZIP_MAX_COMPRESSION_RATIO=200
```

ZIP validation rejects:

- too many archive entries
- excessive total uncompressed size
- suspicious compression ratios
- absolute or parent-traversal member paths
- Windows drive-style member paths
- symlink/special-file entries
- encrypted entries
- archives without at least one supported `.docx`, `.pdf`, or `.md` manuscript file

Original upload names are reduced to safe basenames before they are stored or placed in download headers.

### Deployment verification

After deploying with the production environment:

```bash
python manage.py check
python manage.py check --deploy
```

Confirm the service starts with the private media directory configured, then verify the public URLs are not exposed:

```bash
curl -I https://api.example.com/media/test.pdf
curl -I https://api.example.com/__private_media__/test.pdf
```

Both should return `404` rather than manuscript content. Then verify a legitimate editor download through the authenticated application UI still succeeds.

## Dependency vulnerability audit

Runtime dependencies are pinned and CI now audits the resolved production dependency graph on every push and pull request.

Local verification:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip check
python -m pip_audit -r requirements.txt --strict --progress-spinner off
```

The current audit baseline is documented in `SECURITY_DEPENDENCY_AUDIT_2026-09-26.md`.

Do not bypass or remove the audit step to make a dependency update pass. If a future advisory causes CI to fail, update or replace the affected dependency, run the full backend test suite, and merge through the normal review process.

Weekly Dependabot PRs are enabled for both Python packages and GitHub Actions so new dependency versions are surfaced automatically without direct changes to `main`.

