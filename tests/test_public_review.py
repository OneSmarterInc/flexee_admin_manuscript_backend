import pytest
from django.test import Client
from django.core.files.uploadedfile import SimpleUploadedFile
import os
from unittest.mock import patch
from review.models import Submission, ReviewJob
from review.tasks import run_public_review_task

@pytest.mark.django_db
@patch.dict(os.environ, {"AI_PROVIDER": "mock"})
class TestPublicReview:
    def setup_method(self):
        self.client = Client()

    def test_public_review_flow(self):
        upload = SimpleUploadedFile("test.md", b"Test manuscript content here. Needs a few words.")
        data = {
            'type': 'article',
            'author': 'Test Author',
            'email': 'test@example.com',
            'title': 'Test Title',
            'disclosure': 'None',
            'attestation': 'human-authored-with-ai-assistance',
            'manuscript': upload
        }
        
        response = self.client.post('/api/submissions/', data=data)
        
        assert response.status_code == 202
        resp_data = response.json()
        assert 'job_id' in resp_data
        assert 'id' in resp_data
        
        job_id = resp_data['job_id']
        submission_id = resp_data['id']
        
        # Check initial status
        status_resp = self.client.get(f'/api/submissions/{submission_id}/status/')
        assert status_resp.status_code == 200
        assert status_resp.json()['status'] == 'processing'

        # Execute run_public_review_task directly
        run_public_review_task(job_id, submission_id)

        # GET public status endpoint
        status_resp = self.client.get(f'/api/submissions/{submission_id}/status/')
        assert status_resp.status_code == 200
        
        status_data = status_resp.json()
        assert status_data['status'] == 'completed'
        assert 'decision' in status_data
        assert 'total_words' in status_data
        assert 'author_letter' in status_data
        assert 'email_warning' in status_data
        
        assert 'editor_summary' not in status_data
        assert 'manuscript' not in status_data

    @patch('review.tasks.run_review', side_effect=Exception('Simulated review failure'))
    def test_public_review_failure(self, mock_run_review):
        upload = SimpleUploadedFile("test2.md", b"Test manuscript content here.")
        data = {
            'type': 'article',
            'author': 'Test Author',
            'email': 'test2@example.com',
            'title': 'Test Title 2',
            'disclosure': 'None',
            'attestation': 'human-authored-with-ai-assistance',
            'manuscript': upload
        }
        
        response = self.client.post('/api/submissions/', data=data)
        assert response.status_code == 202
        resp_data = response.json()
        job_id = resp_data['job_id']
        submission_id = resp_data['id']
        
        run_public_review_task(job_id, submission_id)
        
        job = ReviewJob.objects.get(id=job_id)
        assert job.status == 'failed'
        assert 'Simulated review failure' in job.error_message
        
        submission = Submission.objects.get(id=submission_id)
        assert submission.status == 'failed'
        assert submission.error is not None
