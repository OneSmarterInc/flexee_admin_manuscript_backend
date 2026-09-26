import base64
import json
import mimetypes
import os
import secrets
import time
import uuid
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.signing import dumps
from django_q.tasks import async_task

from review.auth import allowed_frontend_origins, hash_password, totp_code
from review.models import (
    Author,
    EditorFeedback,
    EditorUser,
    Membership,
    ReviewJob,
    SMTPSettings,
    Venue,
    VenueSubmission,
)
from review.services.ai_provider import ai_available
from review.services.email_service import _send


SUPPORTED_MANUSCRIPTS = {'.docx', '.pdf', '.md', '.zip'}
MANUSCRIPT_TYPES = {
    'research_article',
    'practitioner_article',
    'review_article',
    'case_study',
    'conference_paper',
    'book',
    'other',
}


class Command(BaseCommand):
    help = (
        'Run a production-style scholarly-network E2E against a live Django API and a real qcluster worker. '
        'The command requires AI_PROVIDER=ollama or AI_PROVIDER=anthropic and writes a timing/report JSON file.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--manuscript', required=True, help='Path to a real .docx, .pdf, .md, or .zip manuscript.')
        parser.add_argument('--author-email', required=True, help='Fresh test email address that can receive SMTP messages.')
        parser.add_argument('--author-name', default='Flexee Production E2E Author')
        parser.add_argument('--title', default='')
        parser.add_argument('--manuscript-type', default='practitioner_article', choices=sorted(MANUSCRIPT_TYPES))
        parser.add_argument('--venue-slug', default='field-notes-journal')
        parser.add_argument('--base-url', default='http://127.0.0.1:8000')
        parser.add_argument('--frontend-origin', default='')
        parser.add_argument('--timeout', type=int, default=1800, help='Maximum seconds to wait for each queued AI stage.')
        parser.add_argument(
            '--decision',
            default='revision_requested',
            choices=['accepted', 'rejected', 'revision_requested'],
            help='Human editorial decision recorded at the end of the E2E run.',
        )
        parser.add_argument('--report', default='', help='Output JSON report path. A timestamped file is used by default.')
        parser.add_argument(
            '--allow-console-email',
            action='store_true',
            help='Allow Django console email backend. Omit this for a production-readiness run so real SMTP is required.',
        )
        parser.add_argument(
            '--cleanup',
            action='store_true',
            help='Delete the E2E author/manuscript/submission data after the report is written.',
        )

    def handle(self, *args, **options):
        started = time.monotonic()
        stages = {}
        created = {'author_id': None, 'manuscript_id': None, 'submission_id': None, 'editor_id': None}
        report = {
            'started_at': datetime.now(dt_timezone.utc).isoformat(),
            'status': 'running',
            'provider': os.getenv('AI_PROVIDER', 'auto').strip().lower(),
            'ollama_model': os.getenv('OLLAMA_MODEL', ''),
            'anthropic_model': os.getenv('ANTHROPIC_MODEL', ''),
            'base_url': options['base_url'].rstrip('/'),
            'venue_slug': options['venue_slug'],
            'manuscript_type': options['manuscript_type'],
            'stages_seconds': stages,
            'ids': created,
            'email': {},
            'models_observed': {},
            'cost': {
                'status': 'not_instrumented',
                'note': 'The current provider abstraction does not persist token usage/cost. Record provider billing separately for this run.',
            },
        }

        editor = None
        try:
            manuscript_path = self._preflight(options, report)
            venue = Venue.objects.select_related('organization').get(slug=options['venue_slug'], active=True)
            config = venue.agent_configs.filter(active=True).order_by('-version', '-created_at').first()
            if not config:
                raise CommandError(f'Venue {venue.slug!r} has no active VenueAgentConfig.')
            if config.required_submission_items:
                raise CommandError(
                    f'Venue {venue.slug!r} has required_submission_items configured. '
                    'Use a venue with no required items for the automated production E2E, '
                    'then test its required-item UI manually.'
                )

            origin = options['frontend_origin'].strip() or self._default_origin()
            report['frontend_origin'] = origin

            self._stage(stages, 'api_health', lambda: self._check_health(report['base_url']))
            self._stage(stages, 'qcluster_probe', lambda: self._probe_qcluster(options['timeout']))

            if self._smtp_configured():
                smtp_result = self._stage(
                    stages,
                    'smtp_probe',
                    lambda: _send(
                        options['author_email'],
                        'Flexee production E2E SMTP probe',
                        'This message confirms that the Flexee production E2E harness can reach the configured SMTP service.',
                    ),
                )
                report['email']['smtp_probe'] = smtp_result
                if not smtp_result.get('sent'):
                    raise CommandError(f'SMTP probe did not report success: {smtp_result}')
            else:
                report['email']['smtp_probe'] = {'console_backend': True}
                if not options['allow_console_email']:
                    raise CommandError(
                        'Real SMTP is not configured. Configure SMTP_HOST or SMTPSettings, '
                        'or use --allow-console-email only for a non-production local rehearsal.'
                    )

            author_client = httpx.Client(
                base_url=report['base_url'],
                timeout=60.0,
                follow_redirects=False,
            )
            admin_client = httpx.Client(
                base_url=report['base_url'],
                timeout=60.0,
                follow_redirects=False,
                headers={'Origin': origin},
            )
            try:
                author_id = self._stage(
                    stages,
                    'author_registration',
                    lambda: self._register_author(author_client, options),
                )
                created['author_id'] = author_id
                report['email']['verification_email_requested'] = True

                self._stage(
                    stages,
                    'author_email_verification',
                    lambda: self._verify_author_email(author_client, author_id),
                )

                upload_payload = self._stage(
                    stages,
                    'manuscript_upload',
                    lambda: self._upload_manuscript(author_client, manuscript_path, options),
                )
                manuscript_id = upload_payload['manuscript']['id']
                created['manuscript_id'] = manuscript_id

                mechanical = self._stage(
                    stages,
                    'mechanical_readiness',
                    lambda: self._post_json(
                        author_client,
                        f'/api/author/manuscripts/{manuscript_id}/readiness/run/',
                        {},
                        expected={201},
                    ),
                )
                if not mechanical['readiness']['summary'].get('ready_for_matching'):
                    raise CommandError(
                        'Mechanical readiness is blocking matching. '
                        f"Findings: {json.dumps(mechanical['readiness'].get('findings', []), ensure_ascii=False)}"
                    )

                semantic_job = self._stage(
                    stages,
                    'semantic_readiness_enqueue',
                    lambda: self._post_json(
                        author_client,
                        f'/api/author/manuscripts/{manuscript_id}/readiness/semantic/',
                        {},
                        expected={202},
                    ),
                )
                self._stage(
                    stages,
                    'semantic_readiness_worker',
                    lambda: self._wait_for_job(semantic_job['job_id'], options['timeout']),
                )
                readiness = self._get_json(
                    author_client,
                    f'/api/author/manuscripts/{manuscript_id}/readiness/',
                    expected={200},
                )
                semantic_summary = (readiness.get('semantic_readiness') or {}).get('summary') or {}
                report['models_observed']['semantic_readiness'] = semantic_summary.get('model', '')

                matches_payload = self._stage(
                    stages,
                    'deterministic_matching',
                    lambda: self._post_json(
                        author_client,
                        f'/api/author/manuscripts/{manuscript_id}/matches/run/',
                        {},
                        expected={201},
                    ),
                )
                target = self._find_match(matches_payload.get('matches', []), venue.id)
                if target.get('eligibility') == 'ineligible':
                    raise CommandError(
                        f"Target venue is deterministically ineligible: {target.get('gaps', [])}"
                    )

                semantic_match_job = self._stage(
                    stages,
                    'semantic_matching_enqueue',
                    lambda: self._post_json(
                        author_client,
                        f'/api/author/manuscripts/{manuscript_id}/matches/semantic/',
                        {'venue_ids': [str(venue.id)]},
                        expected={202},
                    ),
                )
                self._stage(
                    stages,
                    'semantic_matching_worker',
                    lambda: self._wait_for_job(semantic_match_job['job_id'], options['timeout']),
                )
                semantic_matches = self._get_json(
                    author_client,
                    f'/api/author/manuscripts/{manuscript_id}/matches/',
                    expected={200},
                )
                report['match_after_semantic'] = self._find_match(
                    semantic_matches.get('matches', []),
                    venue.id,
                )

                submission_payload = self._stage(
                    stages,
                    'venue_submission_create',
                    lambda: self._post_json(
                        author_client,
                        f'/api/author/manuscripts/{manuscript_id}/submissions/',
                        {'venue_id': str(venue.id)},
                        expected={201},
                    ),
                )
                submission_id = submission_payload['submission']['id']
                created['submission_id'] = submission_id

                assessment_job = self._stage(
                    stages,
                    'venue_assessment_enqueue',
                    lambda: self._post_json(
                        author_client,
                        f'/api/author/venue-submissions/{submission_id}/assessment/run/',
                        {},
                        expected={202},
                    ),
                )
                self._stage(
                    stages,
                    'venue_assessment_worker',
                    lambda: self._wait_for_job(assessment_job['job_id'], options['timeout']),
                )
                assessed = self._get_json(
                    author_client,
                    f'/api/author/venue-submissions/{submission_id}/',
                    expected={200},
                )['submission']
                if assessed.get('status') != 'packet_ready':
                    raise CommandError(f"Venue assessment did not produce packet_ready; status={assessed.get('status')}")
                report['models_observed']['venue_assessment'] = (
                    assessed.get('editorial_brief') or {}
                ).get('model', '')

                submitted = self._stage(
                    stages,
                    'packet_submission',
                    lambda: self._post_json(
                        author_client,
                        f'/api/author/venue-submissions/{submission_id}/submit/',
                        {},
                        expected={200},
                    ),
                )
                if submitted['submission'].get('status') != 'submitted':
                    raise CommandError('Packet submission did not enter submitted status.')
                report['email']['submission_confirmation_requested'] = True

                editor, editor_password = self._create_temp_editor(venue)
                created['editor_id'] = str(editor.id)
                self._stage(
                    stages,
                    'editor_login',
                    lambda: self._editor_login(admin_client, editor, editor_password),
                )

                self._stage(
                    stages,
                    'editor_start_review',
                    lambda: self._post_json(
                        admin_client,
                        f'/api/admin/venue-submissions/{submission_id}/start-review/',
                        {},
                        expected={200},
                    ),
                )

                self._stage(
                    stages,
                    'editor_feedback',
                    lambda: self._post_json(
                        admin_client,
                        f'/api/admin/venues/{venue.id}/feedback/',
                        {
                            'venue_submission_id': submission_id,
                            'assessment_field': 'outlet_fit',
                            'agent_value': (assessed.get('editorial_brief') or {}).get('outlet_fit'),
                            'editor_value': {'note': 'Production E2E human review completed.'},
                            'reason': 'Production E2E verification of venue-scoped human feedback.',
                        },
                        expected={201},
                    ),
                )

                decision_note = (
                    'Production E2E test: human editor recorded this decision after reviewing the venue-specific brief.'
                )
                final = self._stage(
                    stages,
                    'human_editor_decision',
                    lambda: self._post_json(
                        admin_client,
                        f'/api/admin/venue-submissions/{submission_id}/decision/',
                        {'decision': options['decision'], 'note': decision_note},
                        expected={200},
                    ),
                )
                final_status = final['submission'].get('status')
                if final_status != options['decision']:
                    raise CommandError(
                        f'Human decision mismatch: expected {options["decision"]}, got {final_status}'
                    )
                report['email']['editor_decision_notification_requested'] = True

                editor_detail = self._get_json(
                    admin_client,
                    f'/api/admin/venue-submissions/{submission_id}/',
                    expected={200},
                )['submission']
                report['final_submission_status'] = editor_detail.get('status')
                report['human_decision'] = editor_detail.get('decision')
                report['evidence_count'] = len(editor_detail.get('evidence') or [])
                report['feedback_count'] = len(editor_detail.get('feedback') or [])
                report['decision_authority'] = (
                    editor_detail.get('editorial_brief') or {}
                ).get('decision_authority', '')
            finally:
                author_client.close()
                admin_client.close()

            report['status'] = 'passed'
            self.stdout.write(self.style.SUCCESS('Production E2E completed successfully.'))
        except Exception as exc:
            report['status'] = 'failed'
            report['error'] = str(exc)
            raise
        finally:
            report['total_seconds'] = round(time.monotonic() - started, 3)
            report['finished_at'] = datetime.now(dt_timezone.utc).isoformat()
            report_path = self._report_path(options, report['provider'])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
            self.stdout.write(f'E2E report: {report_path}')

            if editor is not None:
                EditorUser.objects.filter(id=editor.id).delete()
                created['editor_id'] = None

            if options.get('cleanup'):
                self._cleanup(created)

        self.stdout.write(
            self.style.SUCCESS(
                f"PASS provider={report['provider']} total={report['total_seconds']}s "
                f"submission={created.get('submission_id') or 'cleaned'}"
            )
        )

    def _preflight(self, options, report):
        provider = report['provider']
        if provider not in {'ollama', 'anthropic'}:
            raise CommandError(
                'Set AI_PROVIDER explicitly to ollama or anthropic for a production E2E run. '
                f'Current value: {provider!r}. Do not use mock or auto for this proof.'
            )
        if not os.getenv('ADMIN_SESSION_SECRET', '').strip():
            raise CommandError('ADMIN_SESSION_SECRET must be configured.')
        if not ai_available():
            raise CommandError(f'AI provider {provider!r} is not available from this process.')

        manuscript = Path(options['manuscript']).expanduser().resolve()
        if not manuscript.is_file():
            raise CommandError(f'Manuscript file not found: {manuscript}')
        if manuscript.suffix.lower() not in SUPPORTED_MANUSCRIPTS:
            raise CommandError('Manuscript must be .docx, .pdf, .md, or .zip.')
        if manuscript.stat().st_size <= 0:
            raise CommandError('Manuscript file is empty.')

        if Author.objects.filter(email=options['author_email'].strip().lower()).exists():
            raise CommandError(
                'The E2E author email already exists. Use a fresh email alias or delete the previous E2E author first.'
            )

        if report['base_url'].startswith('http://') and os.getenv('COOKIE_SECURE', 'false').lower() in {'1', 'true', 'yes', 'on'}:
            raise CommandError('COOKIE_SECURE=true prevents local HTTP session cookies. Use HTTPS or COOKIE_SECURE=false locally.')

        return manuscript

    def _default_origin(self):
        origins = sorted(allowed_frontend_origins())
        if not origins:
            raise CommandError('FRONTEND_ORIGINS must contain at least one allowed frontend origin.')
        return origins[0]

    def _smtp_configured(self):
        if SMTPSettings.objects.filter(host__gt='').exists():
            return True
        backend = str(getattr(settings, 'EMAIL_BACKEND', ''))
        return backend.endswith('smtp.EmailBackend') and bool(os.getenv('SMTP_HOST', '').strip())

    def _check_health(self, base_url):
        try:
            response = httpx.get(f'{base_url}/api/health/', timeout=10.0)
        except httpx.HTTPError as exc:
            raise CommandError(f'Cannot reach Django API at {base_url}: {exc}') from exc
        if response.status_code != 200:
            raise CommandError(f'Health endpoint returned HTTP {response.status_code}: {response.text[:300]}')
        return response.json()

    def _probe_qcluster(self, timeout):
        reference = str(uuid.uuid4())
        job = ReviewJob.objects.create(job_type='e2e_probe', reference_id=reference, status='queued')
        async_task('review.tasks.run_e2e_worker_probe_task', job.id)
        try:
            return self._wait_for_job(job.id, min(timeout, 60))
        finally:
            ReviewJob.objects.filter(id=job.id).delete()

    def _wait_for_job(self, job_id, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                job = ReviewJob.objects.get(id=job_id)
            except ReviewJob.DoesNotExist as exc:
                raise CommandError(f'ReviewJob {job_id} disappeared while waiting for qcluster.') from exc
            if job.status == 'completed':
                return {
                    'job_id': str(job.id),
                    'job_type': job.job_type,
                    'status': job.status,
                    'progress': job.progress,
                }
            if job.status == 'failed':
                raise CommandError(
                    f'Queued job {job.id} ({job.job_type}) failed: {job.error_message or "unknown error"}'
                )
            time.sleep(1)
        raise CommandError(
            f'Timed out after {timeout}s waiting for ReviewJob {job_id}. '
            'Confirm python manage.py qcluster is running against the same database.'
        )

    def _register_author(self, client, options):
        password = secrets.token_urlsafe(18)
        response = client.post(
            '/api/author/register/',
            json={
                'name': options['author_name'],
                'email': options['author_email'],
                'password': password,
            },
        )
        payload = self._response_json(response, expected={201})
        if not payload.get('verification_email_sent'):
            raise CommandError(
                'Author registration succeeded but the verification email was not sent.'
            )
        return payload['id']

    def _verify_author_email(self, client, author_id):
        token = dumps({'author_id': str(author_id)})
        payload = self._get_json(
            client,
            '/api/author/verify-email/',
            expected={200},
            params={'token': token},
        )
        author = Author.objects.get(id=author_id)
        if not author.email_verified:
            raise CommandError('Author verification endpoint returned but email_verified is still false.')
        return payload

    def _upload_manuscript(self, client, manuscript_path, options):
        content_type = mimetypes.guess_type(manuscript_path.name)[0] or 'application/octet-stream'
        files = {
            'manuscript': (manuscript_path.name, manuscript_path.read_bytes(), content_type),
        }
        data = {
            'title': options['title'].strip() or manuscript_path.stem,
            'author': options['author_name'],
            'email': options['author_email'],
            'manuscript_type': options['manuscript_type'],
            'abstract': 'Production E2E manuscript used to validate the complete scholarly-network workflow.',
            'keywords': 'production e2e, scholarly workflow',
            'disclosure': 'AI tools may have assisted with editing or analysis; the manuscript remains human-authored.',
            'notes': 'Created by python manage.py production_e2e.',
            'attestation': 'human-authored-with-ai-assistance',
        }
        response = client.post('/api/author/manuscripts/', data=data, files=files, timeout=120.0)
        return self._response_json(response, expected={201})

    def _find_match(self, matches, venue_id):
        for item in matches:
            if str((item.get('venue') or {}).get('id')) == str(venue_id):
                return item
        raise CommandError(f'Target venue {venue_id} was not present in matching results.')

    def _create_temp_editor(self, venue):
        password = secrets.token_urlsafe(20)
        secret = base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')
        editor = EditorUser.objects.create(
            email=f'flexee-e2e-editor-{uuid.uuid4().hex[:12]}@example.invalid',
            password_hash=hash_password(password),
            totp_secret=secret,
            platform_superuser=False,
        )
        Membership.objects.create(user=editor, organization=venue.organization, role='editor')
        return editor, password

    def _editor_login(self, client, editor, password):
        response = client.post(
            '/api/admin/login/',
            json={
                'username': editor.email,
                'password': password,
                'totp': totp_code(editor.totp_secret),
            },
        )
        payload = self._response_json(response, expected={200})
        session = self._get_json(client, '/api/admin/session/', expected={200})
        if not session.get('authenticated'):
            raise CommandError(
                'Admin login returned 200 but /api/admin/session/ is unauthenticated. '
                'Check cookie host/SameSite/COOKIE_SECURE configuration.'
            )
        return {'login': payload, 'session': session}

    def _get_json(self, client, path, *, expected, **kwargs):
        response = client.get(path, **kwargs)
        return self._response_json(response, expected=expected)

    def _post_json(self, client, path, payload, *, expected):
        response = client.post(path, json=payload)
        return self._response_json(response, expected=expected)

    def _response_json(self, response, *, expected):
        if response.status_code not in expected:
            body = response.text[:1500]
            raise CommandError(
                f'{response.request.method} {response.request.url} returned '
                f'HTTP {response.status_code}; expected {sorted(expected)}. Body: {body}'
            )
        try:
            return response.json()
        except ValueError as exc:
            raise CommandError(
                f'{response.request.method} {response.request.url} did not return valid JSON.'
            ) from exc

    def _stage(self, stages, name, func):
        started = time.monotonic()
        self.stdout.write(f'[{name}] starting')
        result = func()
        elapsed = round(time.monotonic() - started, 3)
        stages[name] = elapsed
        self.stdout.write(self.style.SUCCESS(f'[{name}] {elapsed}s'))
        return result

    def _report_path(self, options, provider):
        if options.get('report'):
            return Path(options['report']).expanduser().resolve()
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        return Path.cwd() / f'production_e2e_{provider}_{stamp}.json'

    def _cleanup(self, created):
        submission_id = created.get('submission_id')
        manuscript_id = created.get('manuscript_id')
        author_id = created.get('author_id')

        if submission_id:
            EditorFeedback.objects.filter(venue_submission_id=submission_id).delete()
            VenueSubmission.objects.filter(id=submission_id).delete()
            ReviewJob.objects.filter(reference_id=str(submission_id)).delete()
        if manuscript_id:
            ReviewJob.objects.filter(reference_id=str(manuscript_id)).delete()
            from review.models import Manuscript
            Manuscript.objects.filter(id=manuscript_id).delete()
        if author_id:
            Author.objects.filter(id=author_id).delete()
