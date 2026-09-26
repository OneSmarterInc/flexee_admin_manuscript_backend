import uuid
from django.db import models

class Author(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    email = models.EmailField(unique=True, db_index=True)
    password_hash = models.CharField(max_length=200)
    name = models.CharField(max_length=200)
    email_verified = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

class EditorUser(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    email = models.EmailField(unique=True, db_index=True)
    password_hash = models.CharField(max_length=200)
    totp_secret = models.CharField(max_length=64, blank=True)
    platform_superuser = models.BooleanField(default=False)

    class Meta:
        ordering = ['email']

    def __str__(self):
        return self.email

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




class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor_id = models.UUIDField(null=True, blank=True, db_index=True)
    actor_email = models.EmailField(max_length=320, blank=True, db_index=True)
    actor_role = models.CharField(max_length=40, blank=True)
    action = models.CharField(max_length=120, db_index=True)
    resource_type = models.CharField(max_length=80, blank=True, db_index=True)
    resource_id = models.CharField(max_length=100, blank=True, db_index=True)
    organization_id = models.UUIDField(null=True, blank=True, db_index=True)
    venue_id = models.UUIDField(null=True, blank=True, db_index=True)
    venue_submission_id = models.UUIDField(null=True, blank=True, db_index=True)
    manuscript_id = models.UUIDField(null=True, blank=True, db_index=True)
    remote_hash = models.CharField(max_length=64, blank=True)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-occurred_at', '-id']
        indexes = [
            models.Index(fields=['organization_id', '-occurred_at'], name='review_audit_org_time_idx'),
            models.Index(fields=['venue_id', '-occurred_at'], name='review_audit_venue_time_idx'),
            models.Index(fields=['venue_submission_id', '-occurred_at'], name='review_audit_sub_time_idx'),
        ]

class AdminAuthEvent(models.Model):
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    remote_hash = models.CharField(max_length=64, db_index=True)
    success = models.BooleanField()
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-occurred_at']


class AuthorAuthEvent(models.Model):
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

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    name = models.CharField(max_length=300)
    organization_type = models.CharField(max_length=30, choices=TYPE_CHOICES, default='other')
    active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Membership(models.Model):
    ROLE_CHOICES = [
        ('owner', 'Owner'),
        ('editor', 'Editor'),
        ('viewer', 'Viewer'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(EditorUser, on_delete=models.CASCADE, related_name='memberships')
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='memberships')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['organization__name', 'user__email']
        constraints = [
            models.UniqueConstraint(fields=['user', 'organization'], name='review_unique_membership'),
        ]

    def __str__(self):
        return f'{self.user.email} - {self.organization.name} ({self.role})'


class Venue(models.Model):
    TYPE_CHOICES = [
        ('journal', 'Journal'),
        ('conference', 'Conference'),
        ('publisher', 'Publisher'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name='venues',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    name = models.CharField(max_length=300)
    slug = models.SlugField(max_length=180, unique=True)
    venue_type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['active', 'venue_type'], name='review_venue_active_type_idx'),
        ]

    def __str__(self):
        return self.name


class VenueAgentConfig(models.Model):
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='agent_configs')
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    effective_at = models.DateTimeField(auto_now_add=True)
    active = models.BooleanField(default=True, db_index=True)
    aims_scope = models.TextField(blank=True)
    article_types = models.JSONField(default=list, blank=True)
    accepted_methods = models.JSONField(default=list, blank=True)
    quality_threshold = models.TextField(blank=True)
    reviewer_criteria = models.JSONField(default=list, blank=True)
    policies = models.JSONField(default=dict, blank=True)
    disclosures = models.JSONField(default=list, blank=True)
    reporting_standards = models.JSONField(default=list, blank=True)
    desk_rejection_rules = models.JSONField(default=list, blank=True)
    structured_desk_rejection_rules = models.JSONField(default=list, blank=True)
    required_submission_items = models.JSONField(default=list, blank=True)
    retention_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text='Days to retain venue submission content after formal submission. Blank disables automatic expiry.',
    )
    deadlines = models.JSONField(default=dict, blank=True)
    submission_capacity = models.JSONField(default=dict, blank=True)
    current_demand = models.JSONField(default=dict, blank=True)
    config_notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-version', '-created_at']
        constraints = [
            models.UniqueConstraint(fields=['venue', 'version'], name='review_unique_venue_config_version'),
        ]
        indexes = [
            models.Index(fields=['venue', 'active', '-version'], name='review_venue_config_active_idx'),
        ]

    def __str__(self):
        return f'{self.venue.name} v{self.version}'


class Manuscript(models.Model):
    TYPE_CHOICES = [
        ('research_article', 'Research article'),
        ('practitioner_article', 'Practitioner article'),
        ('review_article', 'Review article'),
        ('case_study', 'Case study'),
        ('conference_paper', 'Conference paper'),
        ('book', 'Book manuscript'),
        ('other', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    author_account = models.ForeignKey(Author, on_delete=models.CASCADE, related_name='manuscripts', null=True, blank=True)
    author_name = models.CharField(max_length=200)
    author_email = models.EmailField(max_length=320, blank=True, db_index=True)
    coauthors = models.TextField(blank=True)
    title = models.CharField(max_length=500)
    manuscript_type = models.CharField(max_length=40, choices=TYPE_CHOICES, default='other')
    abstract = models.TextField(blank=True)
    keywords = models.JSONField(default=list, blank=True)
    disclosure = models.TextField()
    notes = models.TextField(blank=True)
    attestation = models.BooleanField(default=True)
    manuscript_filename = models.CharField(max_length=500)
    manuscript_file = models.FileField(upload_to='author_manuscripts/')
    manuscript_bytes = models.BigIntegerField(default=0)
    manuscript_sha256 = models.CharField(max_length=64, db_index=True)
    access_token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    parsed_profile = models.JSONField(default=dict, blank=True)
    content_purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['author_email', '-created_at'], name='review_ms_email_created_idx'),
        ]

    def __str__(self):
        return self.title


class ReadinessAssessment(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.CASCADE, related_name='readiness_assessments')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
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
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='manuscript_matches')
    venue_config = models.ForeignKey(
        VenueAgentConfig,
        on_delete=models.PROTECT,
        related_name='matches',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    eligibility = models.CharField(max_length=20, choices=ELIGIBILITY_CHOICES, default='needs_changes')
    fit_summary = models.TextField(blank=True)
    reasons = models.JSONField(default=list, blank=True)
    gaps = models.JSONField(default=list, blank=True)
    evidence = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['created_at', 'id']
        constraints = [
            models.UniqueConstraint(fields=['manuscript', 'venue'], name='review_unique_ms_venue_match'),
        ]


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
        ('transferred', 'Transferred'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.PROTECT, related_name='venue_submissions')
    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name='submissions')
    venue_config = models.ForeignKey(
        VenueAgentConfig,
        on_delete=models.PROTECT,
        related_name='submissions',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='draft', db_index=True)
    packet = models.JSONField(default=dict, blank=True)
    editorial_brief = models.JSONField(default=dict, blank=True)
    decision = models.JSONField(default=dict, blank=True)
    retention_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    retention_purged_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['venue', 'status', '-created_at'], name='review_vsub_venue_status_idx'),
        ]


class EvidenceFinding(models.Model):
    SOURCE_CHOICES = [
        ('manuscript', 'Manuscript'),
        ('venue_policy', 'Venue policy'),
        ('external', 'External source'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.CASCADE, related_name='evidence_findings')
    venue_submission = models.ForeignKey(
        VenueSubmission,
        on_delete=models.CASCADE,
        related_name='evidence_findings',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finding_type = models.CharField(max_length=100)
    claim = models.TextField()
    source_type = models.CharField(max_length=30, choices=SOURCE_CHOICES)
    source_locator = models.CharField(max_length=500, blank=True)
    source_url = models.URLField(max_length=1000, blank=True)
    excerpt = models.TextField(blank=True)
    verification = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['created_at', 'id']


class EditorFeedback(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='editor_feedback')
    venue_submission = models.ForeignKey(
        VenueSubmission,
        on_delete=models.SET_NULL,
        related_name='editor_feedback',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    assessment_field = models.CharField(max_length=120)
    agent_value = models.JSONField(null=True, blank=True)
    editor_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']


class SubmissionRequirementFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    venue_submission = models.ForeignKey(
        VenueSubmission,
        on_delete=models.CASCADE,
        related_name='requirement_files',
    )
    requirement_key = models.SlugField(max_length=120)
    original_filename = models.CharField(max_length=500)
    file = models.FileField(upload_to='venue_requirement_files/')
    file_bytes = models.BigIntegerField(default=0)
    file_sha256 = models.CharField(max_length=64)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['requirement_key']
        constraints = [
            models.UniqueConstraint(
                fields=['venue_submission', 'requirement_key'],
                name='review_unique_requirement_file',
            ),
        ]


class SubmissionTransfer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    manuscript = models.ForeignKey(Manuscript, on_delete=models.PROTECT, related_name='transfers')
    from_submission = models.ForeignKey(
        VenueSubmission,
        on_delete=models.PROTECT,
        related_name='outgoing_transfers',
    )
    to_submission = models.ForeignKey(
        VenueSubmission,
        on_delete=models.PROTECT,
        related_name='incoming_transfers',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']


class ReviewJob(models.Model):
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed')
    ]
    job_type = models.CharField(max_length=50) # 'semantic_readiness', 'semantic_matches', 'venue_assessment'
    reference_id = models.CharField(max_length=36, db_index=True) # UUID string
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued', db_index=True)
    progress = models.IntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['job_type', 'reference_id'],
                condition=models.Q(status__in=['queued', 'processing']),
                name='review_unique_active_job',
            ),
        ]

class AIBudgetState(models.Model):
    """Singleton lock row used to serialize cloud-AI budget reservations."""
    key = models.CharField(max_length=32, primary_key=True, default='global', editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'AI budget state'
        verbose_name_plural = 'AI budget state'


class AIUsageEvent(models.Model):
    STATUS_CHOICES = [
        ('reserved', 'Reserved'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('blocked', 'Blocked'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    provider = models.CharField(max_length=40, db_index=True)
    model = models.CharField(max_length=200, blank=True, db_index=True)
    operation = models.CharField(max_length=100, default='ai_chat', db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, db_index=True)
    input_tokens = models.BigIntegerField(default=0)
    output_tokens = models.BigIntegerField(default=0)
    total_tokens = models.BigIntegerField(default=0)
    estimated_max_cost_usd = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    actual_cost_usd = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    priced = models.BooleanField(default=False)
    usage_estimated = models.BooleanField(default=False)
    error_type = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['provider', '-created_at'], name='review_ai_provider_time_idx'),
            models.Index(fields=['status', '-created_at'], name='review_ai_status_time_idx'),
            models.Index(fields=['operation', '-created_at'], name='review_ai_operation_time_idx'),
        ]

