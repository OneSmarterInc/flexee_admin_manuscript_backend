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

The command requires real SMTP by default. Use `--allow-console-email` only for a local rehearsal; it is not evidence of production email delivery. The report is written as `production_e2e_<provider>_<timestamp>.json` and includes stage timings, observed model names, final decision state, evidence/feedback counts, and whether each email path was requested.

To exercise the book path, use a real Five Zero-style book or ZIP and run:

```powershell
python manage.py production_e2e --manuscript "C:\path\to\five-zero-book.zip" --author-email "yourname+flexee-book-ollama@example.com" --manuscript-type book --venue-slug five-zero-books
```

Use the same book again with `AI_PROVIDER=anthropic` to compare wall-clock timings. The current AI-provider abstraction does not persist token usage/cost, so the JSON report marks cost as not instrumented; record the provider billing separately for the run.

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

