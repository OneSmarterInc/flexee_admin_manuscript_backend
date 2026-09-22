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


# ---------------------------------------------------------------------------
# Agentic Scholarly Submission Network
#
# These models intentionally live alongside the legacy Submission model while
# the new multi-venue author workflow is introduced. The existing book/article
# review pipeline therefore remains backward compatible.
# ---------------------------------------------------------------------------

class Manuscript(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('readiness_pending', 'Readiness pending'),
        ('needs_updates', 'Needs updates'),
        ('ready', 'Ready'),
        ('venue_selected', 'Venue selected'),
        ('submitted', 'Submitted'),
        ('archived', 'Archived'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='draft', db_index=True)
    title = models.CharField(max_length=500)
    manuscript_type = models.CharField(max_length=100)
    primary_author_name = models.CharField(max_length=200)
    primary_author_email = models.EmailField(max_length=320, blank=True)
    coauthors = models.JSONField(default=list, blank=True)
    abstract = models.TextField(blank=True)
    keywords = models.JSONField(default=list, blank=True)
    ai_disclosure = models.TextField()
    authorship_attested = models.BooleanField(default=False)
    author_notes = models.TextField(blank=True)
    manuscript_filename = models.CharField(max_length=500)
    manuscript_file = models.FileField(upload_to='network_manuscripts/')
    manuscript_bytes = models.BigIntegerField()
    manuscript_sha256 = models.CharField(max_length=64, db_index=True)
    access_key_hash = models.CharField(max_length=64, editable=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at'], name='manuscript_status_idx'),
            models.Index(fields=['primary_author_email', '-created_at'], name='manuscript_email_idx'),
        ]


class Venue(models.Model):
    TYPE_CHOICES = [
        ('journal', 'Journal'),
        ('conference', 'Conference'),
        ('publisher', 'Publisher'),
        ('book_series', 'Book series'),
        ('other', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    name = models.CharField(max_length=300)
    slug = models.SlugField(max_length=180, unique=True)
    venue_type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    subscriber_name = models.CharField(max_length=300, blank=True)
    description = models.TextField(blank=True)
    website = models.URLField(blank=True)
    active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ['name']


class VenueAgentConfig(models.Model):
    venue = models.OneToOneField(Venue, on_delete=models.CASCADE, related_name='agent_config')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)
    aims_scope = models.TextField(blank=True)
    accepted_article_types = models.JSONField(default=list, blank=True)
    accepted_methods = models.JSONField(default=list, blank=True)
    quality_threshold = models.TextField(blank=True)
    policies = models.JSONField(default=dict, blank=True)
    disclosures = models.JSONField(default=list, blank=True)
    reporting_standards = models.JSONField(default=list, blank=True)
    desk_rejection_rules = models.JSONField(default=list, blank=True)
    deadline_notes = models.TextField(blank=True)
    submission_capacity = models.PositiveIntegerField(null=True, blank=True)
    current_demand = models.JSONField(default=dict, blank=True)
    reviewer_criteria = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['venue__name']


class ReadinessAssessment(models.Model):
    STATUS_CHOICES = [('queued', 'Queued'), ('completed', 'Completed'), ('failed', 'Failed')]
    OVERALL_CHOICES = [
        ('not_ready', 'Not ready'),
        ('needs_updates', 'Needs updates'),
        ('ready', 'Ready'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.CASCADE, related_name='readiness_assessments')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued', db_index=True)
    overall_state = models.CharField(max_length=30, choices=OVERALL_CHOICES, blank=True)
    checks = models.JSONField(default=list, blank=True)
    summary = models.TextField(blank=True)
    warnings_count = models.PositiveIntegerField(default=0)
    blocking_count = models.PositiveIntegerField(default=0)
    engine_version = models.CharField(max_length=100, blank=True)
    error = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['manuscript', '-created_at'], name='readiness_manuscript_idx'),
        ]


class VenueMatch(models.Model):
    FIT_CHOICES = [
        ('strong', 'Strong fit'),
        ('possible', 'Possible fit'),
        ('low', 'Low fit'),
        ('not_fit', 'Not a fit'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.CASCADE, related_name='venue_matches')
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='manuscript_matches')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    fit_level = models.CharField(max_length=20, choices=FIT_CHOICES)
    explanation = models.TextField(blank=True)
    reasons = models.JSONField(default=list, blank=True)
    gaps = models.JSONField(default=list, blank=True)
    required_changes = models.JSONField(default=list, blank=True)
    matching_metadata = models.JSONField(default=dict, blank=True)
    is_current = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ['-created_at', 'venue__name']
        constraints = [
            models.UniqueConstraint(
                fields=['manuscript', 'venue'],
                condition=models.Q(is_current=True),
                name='unique_current_venue_match',
            ),
        ]


class VenueAssessment(models.Model):
    STATUS_CHOICES = [('queued', 'Queued'), ('completed', 'Completed'), ('failed', 'Failed')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.CASCADE, related_name='venue_assessments')
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='assessments')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued', db_index=True)
    editorial_brief = models.JSONField(default=dict, blank=True)
    unresolved_risks = models.JSONField(default=list, blank=True)
    reviewer_expertise = models.JSONField(default=list, blank=True)
    agent_config_version = models.PositiveIntegerField(null=True, blank=True)
    engine_version = models.CharField(max_length=100, blank=True)
    error = models.JSONField(null=True, blank=True)
    is_current = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['manuscript', 'venue'],
                condition=models.Q(is_current=True),
                name='unique_current_venue_assess',
            ),
        ]


class EvidenceFinding(models.Model):
    SOURCE_CHOICES = [
        ('manuscript', 'Manuscript'),
        ('venue_policy', 'Venue policy'),
        ('external', 'External source'),
    ]
    VERIFY_CHOICES = [
        ('not_applicable', 'Not applicable'),
        ('verified', 'Verified'),
        ('weak_match', 'Weak match'),
        ('not_found', 'Not found'),
        ('pending', 'Pending'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.CASCADE, related_name='evidence_findings')
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='evidence_findings', null=True, blank=True)
    readiness_assessment = models.ForeignKey(
        ReadinessAssessment,
        on_delete=models.CASCADE,
        related_name='evidence',
        null=True,
        blank=True,
    )
    venue_assessment = models.ForeignKey(
        VenueAssessment,
        on_delete=models.CASCADE,
        related_name='evidence',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finding_type = models.CharField(max_length=100)
    finding = models.TextField()
    source_type = models.CharField(max_length=30, choices=SOURCE_CHOICES)
    source_reference = models.CharField(max_length=500)
    source_excerpt = models.TextField(blank=True)
    external_url = models.URLField(blank=True)
    verification_status = models.CharField(
        max_length=30,
        choices=VERIFY_CHOICES,
        default='not_applicable',
    )
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['created_at', 'id']


class VenueSubmission(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('packet_ready', 'Packet ready'),
        ('submitted', 'Submitted'),
        ('under_review', 'Under review'),
        ('revision_requested', 'Revision requested'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
        ('withdrawn', 'Withdrawn'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.CASCADE, related_name='venue_submissions')
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='submissions')
    parent_submission = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        related_name='transfers',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    selected_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='draft', db_index=True)
    packet = models.JSONField(default=dict, blank=True)
    decision_note = models.TextField(blank=True)
    is_current = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['manuscript', '-created_at'], name='venue_submit_man_idx'),
            models.Index(fields=['venue', 'status'], name='venue_submit_status_idx'),
        ]


class VenueSubmissionEvent(models.Model):
    submission = models.ForeignKey(VenueSubmission, on_delete=models.CASCADE, related_name='events')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    event_type = models.CharField(max_length=100)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['created_at', 'id']


class EditorFeedback(models.Model):
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='editor_feedback')
    manuscript = models.ForeignKey(
        Manuscript,
        on_delete=models.CASCADE,
        related_name='editor_feedback',
        null=True,
        blank=True,
    )
    venue_assessment = models.ForeignKey(
        VenueAssessment,
        on_delete=models.SET_NULL,
        related_name='editor_feedback',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    category = models.CharField(max_length=100)
    original_finding = models.TextField(blank=True)
    correction = models.TextField()
    reason = models.TextField(blank=True)
    applied_to_agent = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']
