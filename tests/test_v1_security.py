import pytest
from django.test import Client, RequestFactory
from django.core.files.uploadedfile import SimpleUploadedFile
import zipfile
import io
import os
from unittest.mock import patch
from review.models import EditorUser, SMTPSettings
from review.auth import issue_session, remote_hash
from review.services.email_service import build_html_email

@pytest.mark.django_db
@patch.dict(os.environ, {"AI_PROVIDER": "mock"})
class TestV1Security:
    def setup_method(self):
        self.client = Client()
        self.admin = EditorUser.objects.create(
            email='admin@example.com',
            password_hash='dummy',
            platform_superuser=True,
        )
        token, _ = issue_session('admin@example.com')
        self.client.cookies['flxee_admin_session'] = token
        
    def test_smtp_password_not_in_response(self):
        SMTPSettings.objects.create(id=1, username='test', password='secretpassword')
        response = self.client.get('/api/admin/smtp/')
        assert response.status_code == 200
        data = response.json()
        assert 'secretpassword' not in str(data)
        assert 'password' not in data

    def test_html_escaping_in_emails(self):
        subject = '<script>alert(1)</script>'
        body = 'Hello <img src=x onerror=alert(1)>'
        html = build_html_email(subject, body)
        assert '<script>' not in html
        assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
        assert '&lt;img src=x onerror=alert(1)&gt;' in html
        
    @patch.dict(os.environ, {"TEST_BYPASS_ORIGIN": "0", "FRONTEND_ORIGINS": "http://localhost:5173"}, clear=False)
    def test_admin_post_origin_protection(self):
        # Valid origin
        response = self.client.post(
            '/api/admin/smtp/',
            data='{}',
            content_type='application/json',
            HTTP_ORIGIN='http://localhost:5173'
        )
        assert response.status_code != 403
        
        # Invalid origin
        response = self.client.post(
            '/api/admin/smtp/',
            data='{}',
            content_type='application/json',
            HTTP_ORIGIN='http://evil.com'
        )
        assert response.status_code == 403
        
    def test_public_submit_rate_limit(self):
        # We need to simulate 3 requests to public submit
        data = {
            'type': 'article',
            'author': 'Test',
            'email': 'test@test.com',
            'title': 'Test',
            'disclosure': 'Test',
            'attestation': 'human-authored-with-ai-assistance',
        }
        
        for _ in range(3):
            upload = SimpleUploadedFile("test.md", b"test content")
            req_data = data.copy()
            req_data['manuscript'] = upload
            resp = self.client.post('/api/submissions/', data=req_data)
            assert resp.status_code in [200, 201, 202]

        # 4th should fail
        upload = SimpleUploadedFile("test.md", b"test content")
        req_data = data.copy()
        req_data['manuscript'] = upload
        resp = self.client.post('/api/submissions/', data=req_data)
        assert resp.status_code == 429
        
    def test_spoofed_x_forwarded_for_does_not_bypass_limits(self):
        # Make a request using spoofed X-Forwarded-For to see if it bypasses
        # Since we reached limit, sending different X-Forwarded-For should still fail
        # because REMOTE_ADDR is 127.0.0.1 and not trusted proxy, so it ignores X-Forwarded-For.
        data = {
            'type': 'article',
            'author': 'Test',
            'email': 'test@test.com',
            'title': 'Test',
            'disclosure': 'Test',
            'attestation': 'human-authored-with-ai-assistance',
        }
        req_data = data.copy()
        for _ in range(3):
            req_data['manuscript'] = SimpleUploadedFile("test.md", b"test content")
            self.client.post('/api/submissions/', data=req_data)
        
        # Now try with spoofed IP
        req_data['manuscript'] = SimpleUploadedFile("test.md", b"test content")
        resp = self.client.post('/api/submissions/', data=req_data, HTTP_X_FORWARDED_FOR='9.9.9.9')
        assert resp.status_code == 429
        
    def test_oversized_zip_rejected(self):
        # Create a tiny zip with many files or large extracted size
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i in range(1005): # Over 1000 limit
                zf.writestr(f"file_{i}.txt", b"test")
                
        upload = SimpleUploadedFile("test.zip", buf.getvalue())
        data = {
            'type': 'article',
            'author': 'Test',
            'email': 'test@test.com',
            'title': 'Test',
            'disclosure': 'Test',
            'attestation': 'human-authored-with-ai-assistance',
            'manuscript': upload
        }
        resp = self.client.post('/api/submissions/', data=data, REMOTE_ADDR='2.2.2.2') # Use fresh IP
        assert resp.status_code == 400
        assert 'too many files' in str(resp.content)
