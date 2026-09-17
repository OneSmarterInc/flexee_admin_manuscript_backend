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

Copy the generated `ADMIN_*` values into `.env`, and set `ANTHROPIC_API_KEY`.
