# Dependency Security Audit — 2026-09-26

## Scope

This audit covers the Python runtime dependency set resolved from `requirements.txt`, the development audit tooling in `requirements-dev.txt`, and the GitHub Actions dependencies used by backend CI.

The audit is intentionally repeatable in CI. It is not a one-time claim that the dependency set will remain vulnerability-free indefinitely.

## Findings and remediation

### Django

Previous pin:

```text
Django==5.2.6
```

Updated pin:

```text
Django==5.2.17
```

Django 5.2.6 predates multiple security patch releases in the supported 5.2 LTS series. The audit date is 2026-09-26; Django 5.2.17 is the current released 5.2 patch level used by this project at that date.

### pypdf

Previous pin:

```text
pypdf==6.1.0
```

Updated pin:

```text
pypdf==6.19.0
```

The old 6.1.0 version predates multiple parser/resource-exhaustion fixes relevant to untrusted PDF processing. Because manuscript PDFs are user-controlled input, keeping pypdf at a current patched release is important even when application-level upload limits are enabled.

### Remaining runtime dependencies

The existing direct pins for HTTP, WSGI, PostgreSQL, Django-Q, database URL parsing, DOCX parsing, and Sentry remain unchanged in this change.

After resolving the complete runtime dependency graph from `requirements.txt`, `pip-audit` reports:

```text
No known vulnerabilities found
```

This statement is limited to vulnerabilities known to the audit service at execution time.

## Dependency integrity

CI now runs:

```bash
python -m pip check
```

Current result:

```text
No broken requirements found.
```

This catches incompatible or missing installed package requirements separately from vulnerability scanning.

## Vulnerability gate

Development tooling now pins:

```text
pip-audit==2.10.1
```

CI runs:

```bash
python -m pip_audit -r requirements.txt --strict --progress-spinner off
```

A newly reported vulnerability in the resolved production dependency graph will make CI fail instead of silently passing.

The production requirement file is audited rather than only the currently installed environment so transitive runtime dependencies are resolved and checked from the declared deployment set.

## GitHub Actions hardening

CI actions were also refreshed:

```text
actions/checkout@v7
actions/setup-python@v7
```

The workflow now declares:

```yaml
permissions:
  contents: read
```

Checkout also uses:

```yaml
persist-credentials: false
```

This removes the previous Node 20 action-runtime deprecation warning and reduces the credential/permission footprint of the test job.

## Ongoing monitoring

`.github/dependabot.yml` now requests weekly update PRs for:

- Python/pip dependencies
- GitHub Actions dependencies

Dependabot only opens PRs; updates still go through normal CI and manual merge review.

## Validation result

Final branch CI:

```text
python -m pip check
No broken requirements found.

python -m pip_audit -r requirements.txt --strict --progress-spinner off
No known vulnerabilities found

python manage.py check
PASS

python manage.py makemigrations --check --dry-run
PASS

pytest -q tests
160 passed
```

No database migration is required for this dependency audit.

## Local verification

From the activated virtual environment:

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip check
python -m pip_audit -r requirements.txt --strict --progress-spinner off
python manage.py check
python manage.py makemigrations --check --dry-run
pytest -q tests
```

A clean audit should report no broken requirements, no known vulnerabilities, and the full test suite passing.

## Operational note

A clean vulnerability scan is a point-in-time result, not a permanent security guarantee. New advisories can be published after deployment. Keep the CI audit enabled and review the weekly Dependabot PRs rather than disabling the gate when a future advisory appears.
