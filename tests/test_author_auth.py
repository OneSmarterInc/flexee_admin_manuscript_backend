import json
from unittest.mock import patch
from django.test import TestCase, override_settings
from review.models import Author, AuthorAuthEvent

class AuthorAuthTests(TestCase):
    def setUp(self):
        self.author = Author.objects.create(
            email='test@example.com',
            name='Test Author',
            password_hash='dummy_hash', # we mock verify_password anyway
            email_verified=False
        )

    @patch('review.author_api.hash_password')
    @patch('review.author_api._send_email')
    def test_author_register(self, mock_send, mock_hash):
        mock_hash.return_value = 'new_hash'
        response = self.client.post('/api/author/register/', json.dumps({
            'email': 'new@example.com',
            'name': 'New Author',
            'password': 'password123'
        }), content_type='application/json')
        
        self.assertEqual(response.status_code, 201)
        self.assertTrue(Author.objects.filter(email='new@example.com').exists())
        self.assertTrue(AuthorAuthEvent.objects.filter(detail__action='register', success=True).exists())
        self.assertTrue(mock_send.called)

    @patch('review.author_api.verify_password')
    def test_author_login_success(self, mock_verify):
        mock_verify.return_value = True
        response = self.client.post('/api/author/login/', json.dumps({
            'email': 'test@example.com',
            'password': 'password123'
        }), content_type='application/json')
        
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuthorAuthEvent.objects.filter(detail__action='login', success=True).exists())
        self.assertIn('flxee_author_session', response.cookies)

    @patch('review.author_api.verify_password')
    def test_author_login_lockout(self, mock_verify):
        mock_verify.return_value = False
        for _ in range(10):
            self.client.post('/api/author/login/', json.dumps({
                'email': 'test@example.com',
                'password': 'wrong'
            }), content_type='application/json')
            
        # 11th attempt should be rate limited
        response = self.client.post('/api/author/login/', json.dumps({
            'email': 'test@example.com',
            'password': 'wrong'
        }), content_type='application/json')
        
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['detail'], 'Too many failed login attempts. Try again later.')

    def test_author_upload_requires_verified_email(self):
        from review.auth import issue_author_session
        from django.core.files.uploadedfile import SimpleUploadedFile
        token, _ = issue_author_session(self.author.id)
        self.client.cookies['flxee_author_session'] = token
        
        upload = SimpleUploadedFile("test.docx", b"dummy content")
        response = self.client.post('/api/author/manuscripts/', {
            'title': 'Test',
            'author': 'Test Author',
            'email': 'test@example.com',
            'manuscript_type': 'research_article',
            'disclosure': 'Test',
            'attestation': 'true',
            'manuscript': upload,
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['detail'], 'Email verification required before upload')

    def test_author_upload_verified_success(self):
        from review.auth import issue_author_session
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.author.email_verified = True
        self.author.save()
        token, _ = issue_author_session(self.author.id)
        self.client.cookies['flxee_author_session'] = token
        
        upload = SimpleUploadedFile("test.docx", b"dummy content")
        response = self.client.post('/api/author/manuscripts/', {
            'title': 'Test',
            'author': 'Test Author',
            'email': 'test@example.com',
            'manuscript_type': 'research_article',
            'disclosure': 'Test',
            'attestation': 'true',
            'manuscript': upload,
        })
        self.assertEqual(response.status_code, 201)
        self.assertIn('manuscript', response.json())

