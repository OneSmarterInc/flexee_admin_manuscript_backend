import json
import uuid
from django.test import TestCase, Client
from django.urls import reverse
from review.models import Organization, EditorUser, Membership, Venue, Manuscript, VenueSubmission, Author
from review.auth import hash_password, issue_session

class EditorAPISecurityTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Org A
        self.org_a = Organization.objects.create(name="Org A", organization_type="journal")
        self.user_a = EditorUser.objects.create(
            email="user_a@example.com",
            password_hash=hash_password("password123"),
            platform_superuser=False
        )
        Membership.objects.create(user=self.user_a, organization=self.org_a, role="editor")
        self.venue_a = Venue.objects.create(name="Venue A", slug="venue-a", venue_type="journal", organization=self.org_a)
        
        author = Author.objects.create(email="author@example.com", password_hash="dummy")
        self.ms_a = Manuscript.objects.create(
            author_account=author,
            title="Manuscript A",
            manuscript_filename="file_a.md"
        )
        self.sub_a = VenueSubmission.objects.create(
            manuscript=self.ms_a,
            venue=self.venue_a,
            status="submitted"
        )

        # Org B
        self.org_b = Organization.objects.create(name="Org B", organization_type="journal")
        self.user_b = EditorUser.objects.create(
            email="user_b@example.com",
            password_hash=hash_password("password123"),
            platform_superuser=False
        )
        Membership.objects.create(user=self.user_b, organization=self.org_b, role="editor")
        self.venue_b = Venue.objects.create(name="Venue B", slug="venue-b", venue_type="journal", organization=self.org_b)
        
        self.ms_b = Manuscript.objects.create(
            author_account=author,
            title="Manuscript B",
            manuscript_filename="file_b.md"
        )
        self.sub_b = VenueSubmission.objects.create(
            manuscript=self.ms_b,
            venue=self.venue_b,
            status="submitted"
        )

        # Login User A
        import os
        os.environ['ADMIN_SESSION_SECRET'] = 'test-secret'
        token, _ = issue_session(self.user_a.email)
        self.client.cookies['flxee_admin_session'] = token

    def test_user_a_cannot_list_org_b_submissions(self):
        response = self.client.get('/api/admin/venue-submissions/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        # Should see sub_a but not sub_b
        ids = [item['id'] for item in data['items']]
        self.assertIn(str(self.sub_a.id), ids)
        self.assertNotIn(str(self.sub_b.id), ids)

    def test_user_a_cannot_access_submission_b_detail(self):
        response = self.client.get(f'/api/admin/venue-submissions/{self.sub_b.id}/')
        self.assertIn(response.status_code, [403, 404])

    def test_user_a_cannot_download_submission_b(self):
        response = self.client.get(f'/api/admin/venue-submissions/{self.sub_b.id}/download/')
        self.assertIn(response.status_code, [403, 404])

    def test_user_a_cannot_perform_decision_on_submission_b(self):
        response = self.client.post(
            f'/api/admin/venue-submissions/{self.sub_b.id}/decision/',
            data=json.dumps({"decision": "accepted", "note": "Great"}),
            content_type="application/json"
        )
        self.assertIn(response.status_code, [403, 404])

    def test_platform_superuser_can_access_all(self):
        superuser = EditorUser.objects.create(
            email="super@example.com",
            password_hash=hash_password("password123"),
            platform_superuser=True
        )
        token, _ = issue_session(superuser.email)
        client = Client()
        client.cookies['flxee_admin_session'] = token

        response = client.get('/api/admin/venue-submissions/')
        data = response.json()
        ids = [item['id'] for item in data['items']]
        self.assertIn(str(self.sub_a.id), ids)
        self.assertIn(str(self.sub_b.id), ids)

        response = client.get(f'/api/admin/venue-submissions/{self.sub_b.id}/')
        self.assertEqual(response.status_code, 200)
