# Flexee Manuscript Production Deployment

This checklist is for the first-pilot production scope. It assumes Ubuntu, Nginx, systemd, PostgreSQL, and the React frontend build from `flexee_admin_manuscript_frontend`.

## 1. Install application dependencies

```bash
cd /var/www/flexee/flexee_admin_manuscript_backend
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Install PostgreSQL client tools used by backup/restore:

```bash
sudo apt update
sudo apt install -y postgresql-client nginx
```

## 2. Configure production environment

```bash
cp .env.production.example .env
chmod 600 .env
```

Replace every `CHANGE_ME` value and every zero AI cost/pricing value that the readiness verifier reports as incomplete.

Generate strong Django/admin session secrets with a password manager or a cryptographically secure generator. Do not reuse development secrets.

## 3. Create protected storage

Adjust the service user if your deployment does not use `www-data`.

```bash
sudo install -d -m 0700 -o www-data -g www-data /var/lib/flexee-private-media
sudo install -d -m 0700 -o www-data -g www-data /var/backups/flexee
```

Never expose `/var/lib/flexee-private-media` through Nginx.

## 4. Database and application bootstrap

```bash
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_flexee_venues
.venv/bin/python manage.py install_retention_schedule
.venv/bin/python manage.py create_editor_user admin@example.com --platform_superuser
```

Store the generated password and TOTP setup URI securely.

## 5. Build the frontend

From the frontend repository:

```bash
npm ci
npm audit --audit-level=high
npm run build
```

For the recommended same-origin deployment leave `VITE_API_BASE_URL` blank.

## 6. Install systemd services

Review paths/users first, then:

```bash
sudo cp flexee-api.service /etc/systemd/system/
sudo cp flexee-qcluster.service /etc/systemd/system/
sudo cp flexee-backup.service /etc/systemd/system/
sudo cp flexee-backup.timer /etc/systemd/system/
sudo cp flexee-queue-health.service /etc/systemd/system/
sudo cp flexee-queue-health.timer /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now flexee-api.service
sudo systemctl enable --now flexee-qcluster.service
sudo systemctl enable --now flexee-backup.timer
sudo systemctl enable --now flexee-queue-health.timer
```

Verify:

```bash
sudo systemctl status flexee-api --no-pager
sudo systemctl status flexee-qcluster --no-pager
sudo systemctl list-timers flexee-backup.timer flexee-queue-health.timer
```

## 7. Install Nginx and HTTPS

Copy `deploy/nginx-flexee.conf` into `/etc/nginx/sites-available/flexee`, replace the example hostname/certificate paths, enable the site, and obtain/install a real TLS certificate.

```bash
sudo ln -s /etc/nginx/sites-available/flexee /etc/nginx/sites-enabled/flexee
sudo nginx -t
sudo systemctl reload nginx
```

The production environment should match the proxy layout:

```env
FRONTEND_ORIGINS=https://manuscript.example.com
TRUST_X_FORWARDED_PROTO=true
TRUSTED_PROXIES=127.0.0.1
COOKIE_SECURE=true
```

## 8. Production verification

Run all checks as the application user:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py check --deploy
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py verify_production_readiness
.venv/bin/python manage.py check_ai_usage --fail-if-unbounded
.venv/bin/python manage.py verify_error_monitoring
.venv/bin/python manage.py check_queue_health
```

`verify_production_readiness` must finish with zero `[FAIL]` lines.

Send one Sentry verification event:

```bash
.venv/bin/python manage.py verify_error_monitoring --send-event
```

Confirm it arrives in Sentry.

## 9. Backup and restore proof

Run and verify a real backup:

```bash
sudo systemctl start flexee-backup.service
sudo journalctl -u flexee-backup.service -n 100 --no-pager
```

Then restore a recent bundle into a disposable PostgreSQL database using `restore_production_backup`. Never use the live production DB for the rehearsal.

## 10. Live deployed E2E

Run the E2E harness against the deployed HTTPS URL using a real deliverable mailbox and without `--allow-console-email`:

```bash
.venv/bin/python manage.py production_e2e \
  --manuscript "/path/to/real-test-manuscript.docx" \
  --author-email "real-test-mailbox@example.com" \
  --manuscript-type practitioner_article \
  --venue-slug field-notes-journal \
  --base-url https://manuscript.example.com \
  --frontend-origin https://manuscript.example.com
```

Also run the book/ZIP path for Five Zero Books. Keep the generated E2E JSON reports as deployment evidence.

## Production acceptance

Do not mark the deployment production-ready until all of these are true:

- backend CI is green
- frontend CI/audit/build is green
- PostgreSQL migrations are current
- HTTPS is valid
- Gunicorn API and qcluster are healthy
- SMTP delivers to a real mailbox
- Sentry verification event is received
- queue-health check is healthy
- backup succeeds
- restore rehearsal succeeds
- production readiness verifier has zero failures
- live deployed article E2E succeeds
- live deployed book E2E succeeds for the book path you intend to offer
