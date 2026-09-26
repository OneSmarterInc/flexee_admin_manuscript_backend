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

