import json
from unittest.mock import patch
from django.test import TestCase, Client
from django.utils import timezone
from datetime import timedelta
import os
import urllib.parse

class AdminAuthTests(TestCase):
    def setUp(self):
        self.client = Client()
        from review.models import EditorUser
        EditorUser.objects.create(
            email='admin',
            password_hash='scrypt$16384$8$1$c2FsdA$YmFzZTY0aGFzaA',
            totp_secret='JBSWY3DPEHPK3PXP',
            platform_superuser=True
        )
        
    @patch.dict(os.environ, {
        'ADMIN_SESSION_SECRET': 'sessionsecret'
    })
    @patch('review.views.verify_password')
    def test_verify_password_does_not_leak_totp_uri(self, mock_verify_password):
        mock_verify_password.return_value = True
        
        response = self.client.post(
            '/api/admin/verify-password/',
            data=json.dumps({'username': 'admin', 'password': 'correct_password'}),
            content_type='application/json'
        )
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get('ok'))
        self.assertNotIn('totp_uri', data)
        self.assertNotIn('ADMIN_TOTP_SECRET', data)

    @patch.dict(os.environ, {
        'ADMIN_SESSION_SECRET': 'sessionsecret'
    })
    @patch('review.views.verify_password')
    @patch('review.views.verify_totp')
    def test_login_succeeds_with_configured_secret(self, mock_verify_totp, mock_verify_password):
        mock_verify_password.return_value = True
        mock_verify_totp.return_value = True
        
        response = self.client.post(
            '/api/admin/login/',
            data=json.dumps({
                'username': 'admin', 
                'password': 'correct_password',
                'totp': '123456'
            }),
            content_type='application/json'
        )
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get('ok'))
        self.assertEqual(data.get('username'), 'admin')
