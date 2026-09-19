import uuid
from django.db import models


class Submission(models.Model):
    STATUS_CHOICES = [('processing', 'Processing'), ('completed', 'Completed'), ('failed', 'Failed')]
    KIND_CHOICES = [('book', 'Book'), ('article', 'Article')]
    DECISION_CHOICES = [
        ('PASS_TO_HUMAN', 'Pass to human'),
        ('REFER_TO_HUMAN_WITH_FLAGS', 'Refer with flags'),
        ('RETURN_TO_AUTHOR', 'Return to author'),
    ]
    ADMIN_DECISION_CHOICES = [
        ('ACCEPTED', 'Accepted'),
        ('REJECTED', 'Rejected'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='processing', db_index=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    author_name = models.CharField(max_length=200)
    author_email = models.EmailField(max_length=320, blank=True)
    coauthors = models.TextField(blank=True)
    title = models.CharField(max_length=500)
    declared_sim = models.CharField(max_length=300, blank=True)
    disclosure = models.TextField()
    notes = models.TextField(blank=True)
    attestation = models.BooleanField(default=True)
    manuscript_filename = models.CharField(max_length=500)
    manuscript_file = models.FileField(upload_to='manuscripts/', null=True, blank=True)
    manuscript_bytes = models.BigIntegerField()
    manuscript_sha256 = models.CharField(max_length=64)
    decision = models.CharField(max_length=40, choices=DECISION_CHOICES, blank=True)
    model = models.CharField(max_length=200, blank=True)
    total_words = models.IntegerField(null=True, blank=True)
    review_record = models.JSONField(null=True, blank=True)
    editor_summary = models.TextField(blank=True)
    author_letter = models.TextField(blank=True)
    error = models.JSONField(null=True, blank=True)
    notification_status = models.CharField(max_length=30, blank=True)
    notification_detail = models.JSONField(null=True, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    admin_decision = models.CharField(max_length=20, choices=ADMIN_DECISION_CHOICES, blank=True)
    rejection_reason = models.TextField(blank=True)
    acceptance_message = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at'], name='review_status_created_idx'),
            models.Index(fields=['author_email', '-created_at'], name='review_email_created_idx'),
        ]


class ReviewEvent(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name='events')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    event_type = models.CharField(max_length=100)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['created_at', 'id']


class AdminAuthEvent(models.Model):
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    remote_hash = models.CharField(max_length=64, db_index=True)
    success = models.BooleanField()
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-occurred_at']


class SMTPSettings(models.Model):
    sender_name = models.CharField(max_length=255, blank=True)
    sender_email = models.CharField(max_length=255, blank=True)
    reply_to_email = models.CharField(max_length=255, blank=True)
    host = models.CharField(max_length=255, blank=True)
    port = models.IntegerField(default=587)
    username = models.CharField(max_length=255, blank=True)
    password = models.CharField(max_length=255, blank=True)
    use_tls = models.BooleanField(default=True)
    use_ssl = models.BooleanField(default=False)
    admin_notification_emails = models.TextField(blank=True, help_text="Comma-separated emails to BCC on accept/reject")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "SMTP Settings"
