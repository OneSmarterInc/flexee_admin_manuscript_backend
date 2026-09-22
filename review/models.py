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


class Organization(models.Model):
    TYPE_CHOICES = [
        ('journal', 'Journal publisher'),
        ('conference', 'Conference organizer'),
        ('publisher', 'Publisher'),
        ('other', 'Other'),
    ]
    STATUS_CHOICES = [('active', 'Active'), ('inactive', 'Inactive')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=300)
    slug = models.SlugField(max_length=180, unique=True)
    organization_type = models.CharField(max_length=30, choices=TYPE_CHOICES, default='other')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']


class Venue(models.Model):
    TYPE_CHOICES = [('journal', 'Journal'), ('conference', 'Conference'), ('publisher', 'Publisher')]
    STATUS_CHOICES = [('active', 'Active'), ('inactive', 'Inactive')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='venues')
    name = models.CharField(max_length=300)
    slug = models.SlugField(max_length=180, unique=True)
    venue_type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active', db_index=True)
    website = models.URLField(blank=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']


class VenueAgentConfig(models.Model):
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='agent_configs')
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=False, db_index=True)
    aims_scope = models.TextField(blank=True)
    article_types = models.JSONField(default=list, blank=True)
    accepted_methods = models.JSONField(default=list, blank=True)
    quality_threshold = models.TextField(blank=True)
    reviewer_criteria = models.JSONField(default=list, blank=True)
    operating_rules = models.JSONField(default=dict, blank=True)
    current_demand = models.JSONField(default=dict, blank=True)
    editor_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['venue_id', '-version']
        constraints = [
            models.UniqueConstraint(fields=['venue', 'version'], name='review_venue_config_version_uniq'),
        ]


class Manuscript(models.Model):
    STATUS_CHOICES = [('draft', 'Draft'), ('ready', 'Ready'), ('archived', 'Archived')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', db_index=True)
    author_name = models.CharField(max_length=200)
    author_email = models.EmailField(max_length=320, blank=True)
    coauthors = models.TextField(blank=True)
    title = models.CharField(max_length=500)
    manuscript_type = models.CharField(max_length=100, blank=True)
    abstract = models.TextField(blank=True)
    keywords = models.JSONField(default=list, blank=True)
    ai_disclosure = models.TextField()
    notes = models.TextField(blank=True)
    manuscript_filename = models.CharField(max_length=500)
    manuscript_file = models.FileField(upload_to='author_manuscripts/')
    manuscript_bytes = models.BigIntegerField()
    manuscript_sha256 = models.CharField(max_length=64, db_index=True)
    profile = models.JSONField(default=dict, blank=True)
    access_token_hash = models.CharField(max_length=64, editable=False)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['author_email', '-created_at'], name='review_ms_email_created_idx'),
            models.Index(fields=['status', '-created_at'], name='review_ms_status_created_idx'),
        ]


class ReadinessAssessment(models.Model):
    STATUS_CHOICES = [('pending', 'Pending'), ('completed', 'Completed'), ('failed', 'Failed')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.CASCADE, related_name='readiness_assessments')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    engine_version = models.CharField(max_length=100, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    findings = models.JSONField(default=list, blank=True)
    error = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class VenueMatch(models.Model):
    ELIGIBILITY_CHOICES = [
        ('eligible', 'Eligible'),
        ('needs_changes', 'Needs changes'),
        ('ineligible', 'Ineligible'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.CASCADE, related_name='venue_matches')
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='matches')
    venue_config = models.ForeignKey(VenueAgentConfig, on_delete=models.SET_NULL, null=True, blank=True, related_name='matches')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    eligibility = models.CharField(max_length=30, choices=ELIGIBILITY_CHOICES)
    fit_summary = models.TextField(blank=True)
    reasons = models.JSONField(default=list, blank=True)
    gaps = models.JSONField(default=list, blank=True)
    evidence = models.JSONField(default=list, blank=True)
    engine_version = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['manuscript', '-created_at'], name='review_match_ms_created_idx'),
        ]


class VenueSubmission(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('packet_ready', 'Packet ready'),
        ('submitted', 'Submitted'),
        ('under_review', 'Under review'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
        ('withdrawn', 'Withdrawn'),
        ('transferred', 'Transferred'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.CASCADE, related_name='venue_submissions')
    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name='submissions')
    venue_config = models.ForeignKey(VenueAgentConfig, on_delete=models.SET_NULL, null=True, blank=True, related_name='submissions')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='draft', db_index=True)
    packet = models.JSONField(default=dict, blank=True)
    editorial_brief = models.JSONField(default=dict, blank=True)
    decision = models.CharField(max_length=50, blank=True)
    decision_detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['manuscript', '-created_at'], name='review_vsub_ms_created_idx'),
            models.Index(fields=['venue', 'status', '-created_at'], name='review_vsub_venue_status_idx'),
        ]


class EvidenceFinding(models.Model):
    SOURCE_TYPE_CHOICES = [
        ('manuscript', 'Manuscript'),
        ('venue_policy', 'Venue policy'),
        ('external', 'External source'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.CASCADE, related_name='evidence_findings')
    venue_match = models.ForeignKey(VenueMatch, on_delete=models.CASCADE, null=True, blank=True, related_name='evidence_findings')
    venue_submission = models.ForeignKey(VenueSubmission, on_delete=models.CASCADE, null=True, blank=True, related_name='evidence_findings')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    source_type = models.CharField(max_length=30, choices=SOURCE_TYPE_CHOICES)
    source_locator = models.CharField(max_length=500)
    claim = models.TextField()
    excerpt = models.TextField(blank=True)
    external_url = models.URLField(blank=True)
    verification = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['created_at']


class EditorFeedback(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='editor_feedback')
    venue_submission = models.ForeignKey(VenueSubmission, on_delete=models.CASCADE, null=True, blank=True, related_name='editor_feedback')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    assessment_field = models.CharField(max_length=120)
    agent_value = models.JSONField(null=True, blank=True)
    editor_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']


class Transfer(models.Model):
    STATUS_CHOICES = [('prepared', 'Prepared'), ('completed', 'Completed'), ('cancelled', 'Cancelled')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.CASCADE, related_name='transfers')
    from_submission = models.ForeignKey(VenueSubmission, on_delete=models.PROTECT, related_name='outgoing_transfers')
    to_submission = models.OneToOneField(VenueSubmission, on_delete=models.PROTECT, related_name='incoming_transfer')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='prepared', db_index=True)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']
