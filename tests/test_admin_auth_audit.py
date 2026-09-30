import json
import os
from unittest.mock import patch

from django.test import Client, TestCase

from review.audit import ADMIN_AUTH_RESOURCE_TYPE
from review.models import AuditEvent, EditorUser, Membership, Organization


ENV = {'ADMIN_SESSION_SECRET': 'sessionsecret'}


class AdminAuthAuditTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = EditorUser.objects.create(
            email='admin@example.com',
            password_hash='hash',
            totp_secret='JBSWY3DPEHPK3PXP',
            platform_superuser=True,
        )

    def post(self, path, body):
        return self.client.post(path, data=json.dumps(body), content_type='application/json')

    def events(self, action):
        return list(AuditEvent.objects.filter(action=action))

    @patch.dict(os.environ, ENV)
    @patch('review.views.verify_password', return_value=False)
    def test_wrong_password_is_audited(self, _verify):
        response = self.post('/api/admin/verify-password/', {'username': 'admin@example.com', 'password': 'nope'})
        self.assertEqual(response.status_code, 401)
        [event] = self.events('admin.login_failed')
        self.assertEqual(event.actor_email, 'admin@example.com')
        self.assertEqual(event.actor_role, 'platform_superuser')
        self.assertEqual(event.resource_type, ADMIN_AUTH_RESOURCE_TYPE)
        self.assertEqual(event.detail['stage'], 'password')
        self.assertEqual(event.detail['reason'], 'invalid_credentials')
        self.assertNotIn('password', json.dumps(event.detail).replace('"stage": "password"', ''))

    @patch.dict(os.environ, ENV)
    def test_unknown_account_is_audited_without_actor(self):
        response = self.post('/api/admin/verify-password/', {'username': 'ghost@example.com', 'password': 'x'})
        self.assertEqual(response.status_code, 401)
        [event] = self.events('admin.login_failed')
        self.assertIsNone(event.actor_id)
        self.assertEqual(event.actor_email, '')
        self.assertEqual(event.detail['username'], 'ghost@example.com')
        self.assertEqual(event.detail['reason'], 'unknown_account')

    @patch.dict(os.environ, ENV)
    @patch('review.views.verify_password', return_value=True)
    def test_correct_password_step_is_not_logged_as_login(self, _verify):
        response = self.post('/api/admin/verify-password/', {'username': 'admin@example.com', 'password': 'ok'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AuditEvent.objects.filter(resource_type=ADMIN_AUTH_RESOURCE_TYPE).exists())

    @patch.dict(os.environ, ENV)
    @patch('review.views.verify_password', return_value=True)
    @patch('review.views.verify_totp', return_value=False)
    def test_wrong_totp_is_audited(self, _totp, _verify):
        response = self.post('/api/admin/login/', {'username': 'admin@example.com', 'password': 'ok', 'totp': '000000'})
        self.assertEqual(response.status_code, 401)
        [event] = self.events('admin.totp_failed')
        self.assertEqual(event.actor_email, 'admin@example.com')
        self.assertEqual(event.detail['reason'], 'invalid_totp')
        self.assertNotIn('000000', json.dumps(event.detail))
        self.assertFalse(self.events('admin.login_succeeded'))

    @patch.dict(os.environ, ENV)
    @patch('review.views.verify_password', return_value=True)
    @patch('review.views.verify_totp', return_value=True)
    def test_successful_login_and_logout_are_audited(self, _totp, _verify):
        response = self.post('/api/admin/login/', {'username': 'admin@example.com', 'password': 'ok', 'totp': '123456'})
        self.assertEqual(response.status_code, 200)
        [login] = self.events('admin.login_succeeded')
        self.assertEqual(login.actor_id, self.admin.id)
        self.assertEqual(login.detail['method'], 'password_totp')

        response = self.post('/api/admin/logout/', {})
        self.assertEqual(response.status_code, 200)
        [logout] = self.events('admin.logout')
        self.assertEqual(logout.actor_email, 'admin@example.com')

    @patch.dict(os.environ, ENV)
    def test_logout_without_session_records_nothing(self):
        response = self.post('/api/admin/logout/', {})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.events('admin.logout'))

    @patch.dict(os.environ, ENV)
    @patch('review.views.verify_password', return_value=True)
    def test_missing_authenticator_is_audited(self, _verify):
        self.admin.totp_secret = ''
        self.admin.save(update_fields=['totp_secret'])
        response = self.post('/api/admin/login/', {'username': 'admin@example.com', 'password': 'ok', 'totp': ''})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.events('admin.totp_not_configured')), 1)

    @patch.dict(os.environ, {**ENV, 'ADMIN_LOGIN_MAX_FAILURES': '1'})
    @patch('review.views.verify_password', return_value=False)
    def test_rate_limited_attempt_is_audited(self, _verify):
        self.post('/api/admin/verify-password/', {'username': 'admin@example.com', 'password': 'bad'})
        response = self.post('/api/admin/verify-password/', {'username': 'admin@example.com', 'password': 'bad'})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(self.events('admin.login_rate_limited')), 1)

    @patch.dict(os.environ, ENV)
    @patch('review.views.verify_password', return_value=True)
    @patch('review.views.verify_totp', return_value=True)
    def test_editor_sees_own_sign_in_events_but_not_others(self, _totp, _verify):
        editor = EditorUser.objects.create(email='editor@example.com', password_hash='hash', totp_secret='secret')
        org = Organization.objects.create(name='Org')
        Membership.objects.create(user=editor, organization=org, role='editor')

        # Another account's failed attempt must stay hidden from the editor.
        with patch('review.views.verify_password', return_value=False):
            self.post('/api/admin/verify-password/', {'username': 'admin@example.com', 'password': 'bad'})

        self.post('/api/admin/login/', {'username': 'editor@example.com', 'password': 'ok', 'totp': '123456'})
        payload = self.client.get('/api/admin/audit-events/').json()
        actions = {(e['action'], e['actor_email']) for e in payload['events']}
        self.assertIn(('admin.login_succeeded', 'editor@example.com'), actions)
        self.assertNotIn(('admin.login_failed', 'admin@example.com'), actions)

    @patch.dict(os.environ, ENV)
    @patch('review.views.verify_password', return_value=True)
    @patch('review.views.verify_totp', return_value=True)
    def test_superuser_sees_all_sign_in_events(self, _totp, _verify):
        with patch('review.views.verify_password', return_value=False):
            self.post('/api/admin/verify-password/', {'username': 'ghost@example.com', 'password': 'bad'})
        self.post('/api/admin/login/', {'username': 'admin@example.com', 'password': 'ok', 'totp': '123456'})
        payload = self.client.get('/api/admin/audit-events/').json()
        actions = [e['action'] for e in payload['events']]
        self.assertIn('admin.login_failed', actions)
        self.assertIn('admin.login_succeeded', actions)
