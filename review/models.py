import uuid
from django.utils import timezone
from django.db import models

class Author(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    email = models.EmailField(unique=True, db_index=True)
    password_hash = models.CharField(max_length=200)
    name = models.CharField(max_length=200)
    email_verified = models.BooleanField(default=False)
    # Sessions issued before this moment are rejected (set when the password changes).
    password_changed_at = models.DateTimeField(null=True, blank=True)

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
    # Sessions issued before this moment are rejected (set when the password changes).
    password_changed_at = models.DateTimeField(null=True, blank=True)

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


class VenueQuerySet(models.QuerySet):
    def author_visible(self):
        """Venues an author may see at all. Excluded venues simply do not appear."""
        return self.filter(active=True, excluded=False)

    def matchable(self):
        """Venues whose rules have been read, so a manuscript can be checked against them."""
        return self.author_visible().filter(trust_tier__in=Venue.RULE_TIERS)


class Venue(models.Model):
    TYPE_CHOICES = [
        ('journal', 'Journal'),
        ('conference', 'Conference'),
        ('publisher', 'Publisher'),
    ]

    # Who stands behind this record. These must never render identically to authors.
    TIER_CLAIMED = 'claimed'                # the venue's editor configured it themselves
    TIER_VERIFIED_INDEX = 'verified_index'  # an agent read the official pages and the quotes were validated
    TIER_LISTED = 'listed'                  # basic metadata only, no rules read yet
    TRUST_TIER_CHOICES = [
        (TIER_CLAIMED, 'Claimed by the editor'),
        (TIER_VERIFIED_INDEX, 'Verified from official pages'),
        (TIER_LISTED, 'Listed'),
    ]
    RULE_TIERS = (TIER_CLAIMED, TIER_VERIFIED_INDEX)

    objects = VenueQuerySet.as_manager()

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

    trust_tier = models.CharField(max_length=20, choices=TRUST_TIER_CHOICES, default=TIER_CLAIMED, db_index=True)
    # When the rules were last confirmed: by the editor (claimed) or by re-reading the official pages (verified_index).
    last_verified_at = models.DateTimeField(null=True, blank=True)
    # The official pages each claim came from.
    source_urls = models.JSONField(default=list, blank=True)
    # Excluded venues are never shown to authors. The reason is internal only and is a list of
    # failed criteria with evidence, never a label: {'criteria': [...], 'evidence_urls': [...],
    # 'decided_at': iso, 'decided_by': email, 'note': str}.
    excluded = models.BooleanField(default=False, db_index=True)
    exclusion_reason = models.JSONField(default=dict, blank=True)
    # Build plan step 7: open calls for papers, re-confirmed weekly from the official pages (no AI).
    # Each call: {'title', 'deadline', 'url', 'evidence_text', 'confirmed_at'}. A call that was not
    # re-confirmed inside its window, or whose deadline passed, is hidden from authors.
    open_calls = models.JSONField(default=list, blank=True)
    calls_checked_at = models.DateTimeField(null=True, blank=True)   # last successful check
    calls_attempted_at = models.DateTimeField(null=True, blank=True)
    calls_error = models.CharField(max_length=300, blank=True)

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
    # When the topical venue shortlist was last made (build plan step 5); venues changed after this
    # are checked against the shortlist when the author next opens their matches.
    shortlisted_at = models.DateTimeField(null=True, blank=True)
    # When the author last opened their venue matches; matches created later are shown as new.
    matches_seen_at = models.DateTimeField(null=True, blank=True)

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
    # Build plan step 5: how close the venue's scope is to the manuscript (cosine of local embeddings,
    # or keyword overlap when the embedding model is unavailable) and its place in the topical shortlist.
    topic_similarity = models.FloatField(null=True, blank=True)
    shortlist_rank = models.PositiveIntegerField(null=True, blank=True)

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
    venue_config = models.ForeignKey(
        VenueAgentConfig,
        on_delete=models.PROTECT,
        related_name='editor_feedback',
        null=True,
        blank=True,
        help_text='Venue Agent configuration the editor was correcting.',
    )
    applied_to_config = models.ForeignKey(
        VenueAgentConfig,
        on_delete=models.SET_NULL,
        related_name='feedback_sources',
        null=True,
        blank=True,
        help_text='Inactive draft configuration created from this feedback, if any.',
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

class StorageQuotaState(models.Model):
    """Singleton lock row used to serialize storage-quota checks."""
    key = models.CharField(max_length=32, primary_key=True, default='global', editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'storage quota state'
        verbose_name_plural = 'storage quota state'


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



class DiscoveredVenue(models.Model):
    """A venue found by the daily discovery agent.

    Staging only: authors never see these. The admin's one-click "Add to Venue
    Agent" turns a candidate into a live Venue plus an active VenueAgentConfig.
    """

    VENUE_TYPE_CHOICES = [('journal', 'Journal'), ('conference', 'Conference'), ('publisher', 'Publisher')]
    ACCEPTANCE_CHOICES = [('accepting', 'Accepting'), ('unclear', 'Unclear'), ('closed', 'Closed')]
    DISCOVERY_STATUS_CHOICES = [
        ('new', 'New'),
        ('added', 'Added'),
        ('ignored', 'Ignored'),
        ('changed', 'Changed'),
        ('error', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    name = models.CharField(max_length=300)
    normalized_name = models.CharField(max_length=300, db_index=True)
    organization_name = models.CharField(max_length=300, blank=True)
    normalized_organization_name = models.CharField(max_length=300, blank=True)
    venue_type = models.CharField(max_length=20, choices=VENUE_TYPE_CHOICES, db_index=True)
    description = models.TextField(blank=True)
    website_url = models.URLField(max_length=1000, blank=True)
    submission_url = models.URLField(max_length=1000, blank=True)
    canonical_domain = models.CharField(max_length=255, blank=True, db_index=True)
    canonical_submission_url = models.CharField(max_length=1000, blank=True, db_index=True)

    acceptance_status = models.CharField(max_length=20, choices=ACCEPTANCE_CHOICES, default='unclear', db_index=True)
    submission_types = models.JSONField(default=list, blank=True)

    # Same shapes as VenueAgentConfig so "Add" can copy them across.
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
    retention_days = models.PositiveIntegerField(null=True, blank=True)
    deadlines = models.JSONField(default=dict, blank=True)
    submission_capacity = models.JSONField(default=dict, blank=True)
    current_demand = models.JSONField(default=dict, blank=True)
    config_notes = models.TextField(blank=True)

    source_evidence = models.JSONField(default=list, blank=True)
    source_urls = models.JSONField(default=list, blank=True)
    confidence = models.PositiveSmallIntegerField(default=0)
    content_fingerprint = models.CharField(max_length=64, blank=True)

    first_discovered_at = models.DateTimeField(default=timezone.now)
    last_checked_at = models.DateTimeField(default=timezone.now, db_index=True)

    discovery_status = models.CharField(max_length=20, choices=DISCOVERY_STATUS_CHOICES, default='new', db_index=True)
    change_summary = models.TextField(blank=True)
    last_error = models.TextField(blank=True)

    added_at = models.DateTimeField(null=True, blank=True)
    # 'discovery' = found by the daily discovery run; 'index' = rules read for a venue index journal
    # (approved from the Venue Index page, so it is not listed in Venue Discovery).
    origin = models.CharField(max_length=20, default='discovery', db_index=True)
    added_venue = models.ForeignKey(Venue, null=True, blank=True, on_delete=models.SET_NULL, related_name='discovery_records')
    added_venue_config = models.ForeignKey(
        VenueAgentConfig, null=True, blank=True, on_delete=models.SET_NULL, related_name='discovery_records'
    )

    class Meta:
        ordering = ['-last_checked_at', 'name']
        indexes = [
            models.Index(fields=['discovery_status', 'acceptance_status'], name='disc_status_accept_idx'),
        ]

    def __str__(self):
        return self.name


class VenueDiscoveryRun(models.Model):
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued', db_index=True)
    trigger = models.CharField(max_length=20, default='schedule')  # schedule | manual
    requested_by = models.CharField(max_length=254, blank=True)
    queries_run = models.PositiveIntegerField(default=0)
    results_seen = models.PositiveIntegerField(default=0)
    official_pages_checked = models.PositiveIntegerField(default=0)
    candidates_created = models.PositiveIntegerField(default=0)
    candidates_updated = models.PositiveIntegerField(default=0)
    candidates_changed = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)
    summary = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']


class IndexedVenue(models.Model):
    """One journal in the venue index spine (build plan layer 1).

    Basic, structured facts from free catalogues (OpenAlex, Crossref, DOAJ) with no AI.
    A record stays 'listed' until the agent reads its rules (layer 2) and it is linked to a
    live Venue, whose own trust tier then applies.
    """

    TYPE_CHOICES = [('journal', 'Journal'), ('conference', 'Conference'), ('book_series', 'Book series')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    title = models.CharField(max_length=500)
    normalized_title = models.CharField(max_length=500, db_index=True)
    venue_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='journal')
    issn_l = models.CharField(max_length=9, blank=True, db_index=True)
    issns = models.JSONField(default=list, blank=True)
    publisher = models.CharField(max_length=300, blank=True)
    country_code = models.CharField(max_length=2, blank=True)
    homepage_url = models.URLField(max_length=1000, blank=True)
    open_access = models.BooleanField(default=False)
    doaj_listed = models.BooleanField(default=False, db_index=True)
    apc_usd = models.PositiveIntegerField(null=True, blank=True)

    # Which target field this record was imported for, and how much of its output is in scope.
    field_profile = models.CharField(max_length=40, db_index=True)
    primary_subfield = models.CharField(max_length=200, blank=True, db_index=True)
    subfields = models.JSONField(default=list, blank=True)  # [{'id': '1404', 'name': ..., 'share': 0.4}]
    scope_share = models.FloatField(default=0)

    metrics = models.JSONField(default=dict, blank=True)  # works_count, cited_by_count, h_index, is_core, ...
    first_publication_year = models.PositiveSmallIntegerField(null=True, blank=True)
    last_publication_year = models.PositiveSmallIntegerField(null=True, blank=True)

    crossref = models.JSONField(default=dict, blank=True)    # registered, total_dois, first_year, publisher
    doaj = models.JSONField(default=dict, blank=True)        # guideline/scope/board URLs, review weeks, APC
    issn_checks = models.JSONField(default=dict, blank=True)  # valid_checksums, crossref_agrees
    openalex_id = models.CharField(max_length=40, unique=True, null=True, blank=True)  # e.g. 'S9731383'
    source_ids = models.JSONField(default=dict, blank=True)  # other catalogue ids, e.g. {'doaj': '...'}
    checked = models.JSONField(default=dict, blank=True)     # last check time per source

    venue = models.ForeignKey(Venue, null=True, blank=True, on_delete=models.SET_NULL, related_name='index_records')

    first_imported_at = models.DateTimeField(default=timezone.now)
    last_refreshed_at = models.DateTimeField(default=timezone.now, db_index=True)  # last seen in the catalogue
    enriched_at = models.DateTimeField(null=True, blank=True, db_index=True)       # Crossref/DOAJ last checked
    missing_since = models.DateTimeField(null=True, blank=True)  # no longer returned by the catalogue: kept, flagged
    last_error = models.TextField(blank=True)

    # Exclusion screening (build plan step 3). Automated screening only ever flags; a person decides.
    SCREENING_CHOICES = [
        ('not_screened', 'Not screened'),
        ('clear', 'No concerns found'),
        ('flagged', 'Needs review'),
        ('kept', 'Reviewed and kept'),
        ('excluded', 'Excluded'),
    ]
    screening_status = models.CharField(max_length=20, choices=SCREENING_CHOICES, default='not_screened', db_index=True)
    # [{'code', 'label', 'kind': 'negative'|'positive', 'weight', 'detail', 'evidence_url', 'quote', 'source'}]
    screening_flags = models.JSONField(default=list, blank=True)
    screening_points = models.PositiveSmallIntegerField(default=0)
    screened_at = models.DateTimeField(null=True, blank=True)
    pages_checked_at = models.DateTimeField(null=True, blank=True)  # official pages scanned for evidence
    page_flags = models.JSONField(default=list, blank=True)  # evidence found on the journal's own pages
    kept_flag_codes = models.JSONField(default=list, blank=True)  # concerns a reviewer already saw and kept
    rereview_suggested = models.BooleanField(default=False)  # excluded, but the criteria are no longer detected
    # Never shown to authors, never published. Same shape as Venue.exclusion_reason.
    excluded = models.BooleanField(default=False, db_index=True)
    exclusion_reason = models.JSONField(default=dict, blank=True)

    # Rules read from the journal's own pages (build plan step 4, layer 2). Reading never publishes:
    # an admin approves each journal, which creates the live venue ('verified_index').
    RULES_CHOICES = [
        ('not_read', 'Not read'),
        ('ready', 'Rules ready for approval'),
        ('incomplete', 'Rules not found on the pages'),
        ('failed', 'Pages could not be read'),
        ('blocked', 'Site blocks automated reading'),
    ]
    rules_status = models.CharField(max_length=20, choices=RULES_CHOICES, default='not_read', db_index=True)
    rules_read_at = models.DateTimeField(null=True, blank=True)
    rules_error = models.CharField(max_length=300, blank=True)
    discovered = models.ForeignKey('DiscoveredVenue', null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='index_records')

    class Meta:
        ordering = ['title']
        indexes = [
            models.Index(fields=['field_profile', 'title'], name='index_profile_title_idx'),
        ]

    def __str__(self):
        return self.title

    @property
    def trust_tier(self):
        return self.venue.trust_tier if self.venue_id else Venue.TIER_LISTED


class VenueIndexRun(models.Model):
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    MODE_CHOICES = [('full', 'Catalogue refresh and enrichment'), ('enrich', 'Enrichment only'),
                    ('screen', 'Screening only'), ('rules', 'Read rules from official pages'),
                    ('calls', 'Re-confirm open calls for papers')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued', db_index=True)
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default='full')
    trigger = models.CharField(max_length=20, default='schedule')  # schedule | manual | command
    requested_by = models.CharField(max_length=254, blank=True)
    field_profile = models.CharField(max_length=40, blank=True)
    catalogue_method = models.CharField(max_length=20, blank=True)  # subfield_filter | keyword_search
    pages_fetched = models.PositiveIntegerField(default=0)
    records_seen = models.PositiveIntegerField(default=0)
    out_of_scope = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    linked_count = models.PositiveIntegerField(default=0)
    removed_count = models.PositiveIntegerField(default=0)  # no longer in scope, taken out of the index
    enriched_count = models.PositiveIntegerField(default=0)
    flagged_missing = models.PositiveIntegerField(default=0)
    pending_after = models.PositiveIntegerField(default=0)
    screened_count = models.PositiveIntegerField(default=0)
    flagged_count = models.PositiveIntegerField(default=0)
    pages_checked = models.PositiveIntegerField(default=0)
    rules_attempted = models.PositiveIntegerField(default=0)
    rules_ready = models.PositiveIntegerField(default=0)
    rules_failed = models.PositiveIntegerField(default=0)
    rules_retried = models.PositiveIntegerField(default=0)     # step 6: a second local attempt was needed
    rules_escalated = models.PositiveIntegerField(default=0)   # step 6: sent to the cloud model
    calls_checked = models.PositiveIntegerField(default=0)     # step 7: venues whose calls were re-confirmed
    calls_open = models.PositiveIntegerField(default=0)        # step 7: open calls found in this run
    calls_failed = models.PositiveIntegerField(default=0)
    catalogue_complete = models.BooleanField(default=False)
    # When VENUE_INDEX_MAX_RECORDS was reached: works count of the smallest journal kept.
    size_cutoff = models.PositiveIntegerField(null=True, blank=True)
    errors = models.JSONField(default=list, blank=True)
    summary = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']


class IndexReviewDecision(models.Model):
    """A person's decision on an index record: the evidence trail for every exclusion. Never deleted."""

    DECISION_CHOICES = [('exclude', 'Exclude'), ('keep', 'Keep'), ('restore', 'Restore')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(IndexedVenue, on_delete=models.CASCADE, related_name='review_decisions')
    decision = models.CharField(max_length=10, choices=DECISION_CHOICES)
    criteria = models.JSONField(default=list, blank=True)
    evidence_urls = models.JSONField(default=list, blank=True)
    note = models.TextField(blank=True)
    flags_snapshot = models.JSONField(default=list, blank=True)  # what the screening showed at decision time
    decided_by = models.CharField(max_length=254)
    decided_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-decided_at']


class BlockedPublisher(models.Model):
    """Internal publisher blocklist (build plan 4.2). Internal only; never published."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=300)
    normalized_name = models.CharField(max_length=300, unique=True)
    note = models.TextField(blank=True)
    added_by = models.CharField(max_length=254)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class VenueEmbedding(models.Model):
    """Topic vector of a venue's name, description and aims & scope (build plan step 5).

    Made by a local embedding model (Ollama, CPU). Re-made only when the text or the model changes."""
    venue = models.OneToOneField(Venue, on_delete=models.CASCADE, related_name='embedding')
    model = models.CharField(max_length=120)
    text_hash = models.CharField(max_length=64)
    vector = models.JSONField(default=list)  # unit length, so cosine similarity is a dot product
    updated_at = models.DateTimeField(auto_now=True)


class ManuscriptEmbedding(models.Model):
    """Topic vector of a manuscript's title, keywords, abstract and profile. Deleted with its content."""
    manuscript = models.OneToOneField(Manuscript, on_delete=models.CASCADE, related_name='embedding')
    model = models.CharField(max_length=120)
    text_hash = models.CharField(max_length=64)
    vector = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)


class RulesAttempt(models.Model):
    """One extraction attempt while reading a journal's rules (build plan step 6, section 8).

    Local model first; when the page validator finds the result inadequate (no quoted rule, or a
    required field missing), one local retry with the validator's feedback, then the cloud model.
    One row per attempt, so the escalation rate per field is measured on real evidence."""
    STAGE_CHOICES = [('local', 'Local model'), ('local_retry', 'Local retry'), ('cloud', 'Cloud model')]
    OUTCOME_CHOICES = [('adequate', 'Adequate'), ('inadequate', 'Inadequate'), ('error', 'Error')]

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    chain = models.UUIDField(db_index=True)  # the attempts of one read share a chain
    record = models.ForeignKey(IndexedVenue, on_delete=models.CASCADE, related_name='rules_attempts')
    run = models.ForeignKey(VenueIndexRun, null=True, blank=True, on_delete=models.SET_NULL, related_name='rules_attempts')
    job = models.CharField(max_length=40, default='rule_extraction')
    attempt = models.PositiveSmallIntegerField()
    stage = models.CharField(max_length=20, choices=STAGE_CHOICES)
    provider = models.CharField(max_length=40)
    model = models.CharField(max_length=200, blank=True)
    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES)
    reason = models.CharField(max_length=300, blank=True)
    missing_fields = models.JSONField(default=list, blank=True)
    escalated = models.BooleanField(default=False)  # another attempt followed this one

    class Meta:
        ordering = ['created_at', 'attempt']
