import pytest
from django.test import Client
import os
from unittest.mock import patch
from review.models import EditorUser
from review.auth import issue_session
from django_q.models import OrmQ
from django.utils import timezone
from datetime import timedelta

@pytest.mark.django_db
class TestQueueHealth:
    def setup_method(self):
        self.client = Client()
        self.admin = EditorUser.objects.create(
            email='admin@example.com',
            password_hash='dummy',
            platform_superuser=True
        )
        token, _ = issue_session('admin@example.com')
        self.client.cookies['flxee_admin_session'] = token

    def test_queue_health_empty(self):
        response = self.client.get('/api/admin/queue-health/')
        assert response.status_code == 200
        data = response.json()
        assert data['queued_job_count'] == 0
        assert data['oldest_queued_job_age_seconds'] == 0
        
    def test_queue_health_with_jobs(self):
        past = timezone.now() - timedelta(minutes=5)
        OrmQ.objects.create(key='test', payload='test', lock=past)
        
        response = self.client.get('/api/admin/queue-health/')
        assert response.status_code == 200
        data = response.json()
        assert data['queued_job_count'] == 1
        assert data['oldest_queued_job_age_seconds'] >= 300

    def test_queue_health_unauthorized(self):
        self.client.cookies.clear()
        response = self.client.get('/api/admin/queue-health/')
        assert response.status_code == 401
