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
