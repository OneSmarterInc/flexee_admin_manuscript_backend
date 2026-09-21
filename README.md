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

Copy the generated `ADMIN_*` values into `.env`, install Ollama, and run `ollama pull qwen3:1.7b`. The review engine uses the local Ollama API; no cloud AI API key is required. The default 8192-token context is intentionally conservative for an 8 GB RAM development machine.
