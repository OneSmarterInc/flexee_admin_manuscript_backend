"""Claim this venue (build plan step 9): how an editor finds Flexee and becomes a subscriber.

    POST /api/journals/claim/              an editor asks to manage a journal (public, rate-limited)
    POST /api/journals/claim/verify/       they confirm their email address (signed link)
    GET  /api/admin/claims/                platform admin: the claims
    POST /api/admin/claims/<id>/approve/   creates/gives the editor account that owns the journal
    POST /api/admin/claims/<id>/reject/
    POST /api/admin/set-password/          a new editor sets their password from the emailed link

Approval gives the editor ownership of the journal (moved into its own organization, so a claim on
one journal never reaches the publisher's other journals). The journal shows as editor-confirmed
only once the editor saves their own rules (mark_editor_confirmed); until then it keeps the tier it
had, so Flexee's reading is never presented as the editor's word.
"""
import hashlib
import json
import logging
import os
import re
import unicodedata
from datetime import timedelta

from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .audit import record_audit_event
from .auth import hash_password, remote_hash, require_platform_superuser
from .models import (EditorUser, IndexedVenue, Membership, Organization, Venue, VenueAgentConfig, VenueClaim)

logger = logging.getLogger(__name__)

CLAIM_SALT = 'flexee.venue-claim'
SET_PASSWORD_SALT = 'flexee.editor.set-password'
CLAIM_LINK_DAYS = 3
SET_PASSWORD_DAYS = 7
EDITOR_PASSWORD_MIN_LENGTH = 12
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
OPEN = ('pending_email', 'pending_review')
RESEND_AFTER_MINUTES = 10
MAX_SENDS = 4
CONTROL = re.compile(r'[\x00-\x1f\x7f]+')


def _one_line(value, limit):
    """Names and roles go into emails: one line, no control characters."""
    return CONTROL.sub(' ', unicodedata.normalize('NFKC', str(value or ''))).strip()[:limit]


def normalize_email(value):
    return unicodedata.normalize('NFKC', str(value or '')).strip().lower()[:254]


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _json(request):
    try:
        data = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _iso(value):
    return value.isoformat() if value else None


def _origin(request):
    from .author_api import _author_portal_origin
    return _author_portal_origin(request)


def _send(to, subject, body):
    from .services.email_service import _send as send
    try:
        return send(to, subject, body)
    except Exception:  # email must never break the flow; the admin can resend from the list
        logger.exception('Claim email to %s failed', to)
        return {'sent': False}


def _domain(url_or_host):
    from .services.venue_discovery import canonical_host, registrable_domain
    host = canonical_host(url_or_host) if '://' in str(url_or_host) else str(url_or_host or '').lower()
    return registrable_domain(host) if host else ''


# ---------------------------------------------------------------------------
# Which journal is being claimed
# ---------------------------------------------------------------------------

class ClaimError(Exception):
    def __init__(self, detail, status=400):
        super().__init__(detail)
        self.detail, self.status = detail, status


def resolve_target(kind, key):
    """(venue, indexed_record) for a journal an author can see, or ClaimError."""
    from .journal_index_api import listed_records
    if kind == 'venue':
        venue = Venue.objects.author_visible().filter(slug=str(key)[:180]).first()
        if venue is None:
            raise ClaimError('Journal not found.', 404)
        return venue, IndexedVenue.objects.filter(venue=venue).first()
    if kind == 'indexed':
        try:
            record = listed_records().filter(id=key).first()
        except Exception:
            record = None
        if record is None:
            linked = IndexedVenue.objects.filter(id=key, venue__isnull=False).select_related('venue').first() \
                if re.fullmatch(r'[0-9a-fA-F-]{36}', str(key)) else None
            if linked and linked.venue.active and not linked.venue.excluded:
                return linked.venue, linked
            raise ClaimError('Journal not found.', 404)
        return None, record
    raise ClaimError('Unknown journal.', 400)


def journal_url(venue, record):
    urls = list(venue.source_urls or []) if venue else []
    if record and record.homepage_url:
        urls.append(record.homepage_url)
    return next((u for u in urls if u), '')


def site_domains(venue, record):
    urls = (list(venue.source_urls or []) if venue else []) + ([record.homepage_url] if record else [])
    return {d for d in (_domain(u) for u in urls if u) if d}


# ---------------------------------------------------------------------------
# Public: claim and confirm
# ---------------------------------------------------------------------------

def _claim_token(claim):
    return signing.dumps({'claim': str(claim.id)}, salt=CLAIM_SALT)


def _send_claim_confirmation(request, claim):
    """Send (or re-send) the confirmation. Re-sends are spaced and capped, so a claim cannot be
    used to make Flexee's mail server send repeated emails to someone."""
    now = timezone.now()
    if claim.send_count >= MAX_SENDS or (claim.last_sent_at and
                                         now - claim.last_sent_at < timedelta(minutes=RESEND_AFTER_MINUTES)):
        return False
    claim.last_sent_at, claim.send_count = now, claim.send_count + 1
    claim.save(update_fields=['last_sent_at', 'send_count', 'updated_at'])
    link = f"{_origin(request)}/journals/claim/verify?token={_claim_token(claim)}"
    _send(claim.email, f'Confirm your claim for {claim.journal_name}',
          f'Hello {claim.name},\n\n'
          f'You asked to manage {claim.journal_name} on Flexee. Confirm that this is your email address:\n\n'
          f'{link}\n\nThe link works for {CLAIM_LINK_DAYS} days. After you confirm, a Flexee administrator '
          f'reviews the claim and writes to you.\n\nIf you did not ask for this, ignore this email.\n')
    return True


@require_POST
def claim_create(request):
    data = _json(request)
    if data is None:
        return JsonResponse({'detail': 'Invalid JSON'}, status=400)
    name = _one_line(data.get('name'), 200)
    email = normalize_email(data.get('email'))
    role_title = _one_line(data.get('role_title'), 200)
    evidence_url = str(data.get('evidence_url') or '').strip()[:500]
    message = str(data.get('message') or '').strip()[:2000]
    if not name or not role_title:
        return JsonResponse({'detail': 'Enter your name and your role at the journal.'}, status=400)
    if not EMAIL_RE.match(email):
        return JsonResponse({'detail': 'Enter a valid email address.', 'field': 'email'}, status=400)
    if evidence_url and not re.match(r'^https?://', evidence_url, re.I):
        return JsonResponse({'detail': 'The evidence link must start with http:// or https://.', 'field': 'evidence_url'},
                            status=400)
    if not data.get('confirm'):
        return JsonResponse({'detail': 'Confirm that you are an editor or staff member of this journal.'}, status=400)

    network = remote_hash(request)
    hour_ago, day_ago = timezone.now() - timedelta(hours=1), timezone.now() - timedelta(days=1)
    if VenueClaim.objects.filter(remote_hash=network, created_at__gte=hour_ago).count() >= _env_int('VENUE_CLAIMS_PER_HOUR', 5) \
            or VenueClaim.objects.filter(email=email, created_at__gte=day_ago).count() >= _env_int('VENUE_CLAIMS_PER_EMAIL_DAY', 3):
        return JsonResponse({'detail': 'Too many claims from here. Try again later.'}, status=429)

    try:
        venue, record = resolve_target(str(data.get('kind') or ''), str(data.get('key') or ''))
    except ClaimError as exc:
        return JsonResponse({'detail': exc.detail}, status=exc.status)
    if venue and venue.trust_tier == Venue.TIER_CLAIMED:
        return JsonResponse({'detail': 'This journal is already managed by its editors on Flexee. '
                                       'Ask its editorial office to add you.'}, status=409)

    target = {'venue': venue} if venue else {'indexed': record}
    existing = VenueClaim.objects.filter(email=email, status__in=OPEN, **target).first()
    if existing:
        if existing.status == 'pending_email':
            _send_claim_confirmation(request, existing)
        # The same answer as a new claim, so nobody can learn whether someone else confirmed theirs.
        return JsonResponse({'status': 'received', 'detail': _created_message(email)}, status=201)

    email_domain = email.rsplit('@', 1)[-1]
    claim = VenueClaim.objects.create(
        **target, journal_name=(venue.name if venue else record.title)[:300], journal_url=journal_url(venue, record)[:500],
        name=name, email=email, role_title=role_title, evidence_url=evidence_url, message=message,
        email_domain=email_domain, domain_matches=_domain(email_domain) in site_domains(venue, record),
        remote_hash=network,
    )
    _send_claim_confirmation(request, claim)
    return JsonResponse({'status': 'received', 'detail': _created_message(email)}, status=201)


def _created_message(email):
    return (f'Thank you. If {email} still needs confirming, we have sent it a link (it works for {CLAIM_LINK_DAYS} '
            f'days). After that, a Flexee administrator reviews the claim and writes to you.')


@require_POST
def claim_verify(request):
    data = _json(request) or {}
    try:
        payload = signing.loads(str(data.get('token') or ''), salt=CLAIM_SALT, max_age=CLAIM_LINK_DAYS * 86400)
    except signing.SignatureExpired:
        return JsonResponse({'detail': 'This link has expired. Claim the journal again to get a new one.'}, status=400)
    except signing.BadSignature:
        return JsonResponse({'detail': 'This link is not valid.'}, status=400)
    claim = VenueClaim.objects.filter(id=payload.get('claim')).first()
    if claim is None:
        return JsonResponse({'detail': 'This claim no longer exists.'}, status=404)
    if claim.status == 'pending_email':
        claim.email_verified_at = timezone.now()
        claim.status = 'pending_review'
        claim.save(update_fields=['email_verified_at', 'status', 'updated_at'])
        _notify_admins(claim)
    return JsonResponse({'status': claim.status, 'journal_name': claim.journal_name,
                         'detail': {'pending_review': 'Email confirmed. A Flexee administrator will review your claim '
                                                      'and write to you.',
                                    'approved': 'This claim was approved. Sign in to manage the journal.',
                                    'rejected': 'This claim was not approved.'}.get(claim.status, '')})


def _notify_admins(claim):
    from .models import SMTPSettings
    settings_row = SMTPSettings.objects.first()
    recipients = [e.strip() for e in (settings_row.admin_notification_emails if settings_row else '').split(',') if e.strip()]
    if not recipients:
        return
    _send(recipients[0], f'New venue claim: {claim.journal_name}',
          f'{claim.name} ({claim.role_title}, {claim.email}) claims {claim.journal_name}.\n'
          f'Email on the journal\'s own domain: {"yes" if claim.domain_matches else "no"}.\n'
          f'Evidence: {claim.evidence_url or "none given"}\n\nReview it in Flexee Admin -> Venue claims.\n')


# ---------------------------------------------------------------------------
# Admin: review
# ---------------------------------------------------------------------------

def claim_payload(claim):
    venue = claim.venue
    return {
        'id': str(claim.id), 'status': claim.status, 'created_at': _iso(claim.created_at),
        'journal': {'name': claim.journal_name, 'url': claim.journal_url,
                    'kind': 'venue' if claim.venue_id else 'indexed',
                    'key': venue.slug if venue else (str(claim.indexed_id) if claim.indexed_id else ''),
                    'tier': venue.trust_tier if venue else 'listed'},
        'claimant': {'name': claim.name, 'email': claim.email, 'role_title': claim.role_title,
                     'evidence_url': claim.evidence_url, 'message': claim.message},
        'checks': {'email_verified_at': _iso(claim.email_verified_at), 'email_domain': claim.email_domain,
                   'domain_matches': claim.domain_matches,
                   'other_open_claims': VenueClaim.objects.filter(status__in=OPEN).exclude(id=claim.id).filter(
                       **({'venue_id': claim.venue_id} if claim.venue_id else {'indexed_id': claim.indexed_id})).count()},
        'decision': {'by': claim.decided_by, 'at': _iso(claim.decided_at), 'note': claim.decision_note},
    }


@require_GET
@require_platform_superuser
def admin_claims(request):
    status = request.GET.get('status', 'pending_review')
    items = VenueClaim.objects.select_related('venue')
    if status in dict(VenueClaim.STATUS_CHOICES):
        items = items.filter(status=status)
    counts = {key: VenueClaim.objects.filter(status=key).count() for key, _label in VenueClaim.STATUS_CHOICES}
    return JsonResponse({'claims': [claim_payload(c) for c in items[:200]], 'counts': counts})


def _venue_for(claim):
    """The live venue to hand over; a listed journal becomes a (still 'listed') venue first."""
    from .author_api import build_venue_config_fields, unique_venue_slug
    if claim.venue_id:
        return claim.venue
    record = claim.indexed
    if record.venue_id:
        return record.venue
    venue = Venue.objects.create(name=record.title[:300], slug=unique_venue_slug(record.title), venue_type='journal',
                                 description='', active=True, trust_tier=Venue.TIER_LISTED,
                                 source_urls=[record.homepage_url] if record.homepage_url else [])
    VenueAgentConfig.objects.create(venue=venue, version=1, active=True, **build_venue_config_fields({}))
    record.venue = venue
    record.save(update_fields=['venue', 'updated_at'])
    return venue


def _own_organization(venue):
    """The journal gets its own organization, so its new owner never reaches other journals."""
    current = venue.organization
    shared = current is None or Venue.objects.filter(organization=current).exclude(id=venue.id).exists() \
        or Membership.objects.filter(organization=current).exists()
    if not shared:
        return current
    org = Organization.objects.create(name=f'{venue.name} editorial office'[:300], organization_type='journal')
    venue.organization = org
    venue.save(update_fields=['organization', 'updated_at'])
    return org


def _password_fingerprint(user):
    return hashlib.sha256(f'{user.id}:{user.password_hash}'.encode('utf-8')).hexdigest()[:24]


def set_password_link(request, user):
    token = signing.dumps({'user': str(user.id), 'fp': _password_fingerprint(user)}, salt=SET_PASSWORD_SALT)
    return f'{_origin(request)}/admin/set-password?token={token}'


@require_POST
@require_platform_superuser
def admin_claim_approve(request, claim_id):
    data = _json(request) or {}
    note = str(data.get('note') or '').strip()[:2000]
    with transaction.atomic():
        claim = VenueClaim.objects.select_for_update().select_related('venue', 'indexed').filter(id=claim_id).first()
        if claim is None:
            return JsonResponse({'detail': 'Claim not found'}, status=404)
        if claim.status != 'pending_review':
            return JsonResponse({'detail': 'Only a claim with a confirmed email can be approved.'}, status=409)
        # Lock the journal, so two approvals for it cannot race, and never take a journal from its editor.
        if claim.indexed_id:
            IndexedVenue.objects.select_for_update().filter(id=claim.indexed_id).first()
        venue = _venue_for(claim)
        venue = Venue.objects.select_for_update().get(id=venue.id)
        if venue.excluded:
            return JsonResponse({'detail': 'This journal is excluded from the index.'}, status=409)
        same_journal = Q(venue=venue) | (Q(indexed_id=claim.indexed_id) if claim.indexed_id else Q(pk__in=[]))
        earlier = VenueClaim.objects.filter(same_journal, status='approved').exclude(id=claim.id)
        if venue.trust_tier == Venue.TIER_CLAIMED or earlier.exists():
            return JsonResponse({'detail': 'This journal is already managed by an editor. Reject this claim, or ask '
                                           'the current editor to add this person.'}, status=409)
        org = _own_organization(venue)
        email = normalize_email(claim.email)
        user = EditorUser.objects.filter(email=email).first() or (
            EditorUser.objects.filter(email__iexact=email).first() if email.isascii() else None)
        new_account = user is None
        if new_account:
            user = EditorUser.objects.create(email=email, password_hash='!unset')  # no password until set
        membership, _ = Membership.objects.get_or_create(user=user, organization=org, defaults={'role': 'owner'})
        if membership.role != 'owner':
            membership.role = 'owner'
            membership.save(update_fields=['role'])
        claim.status, claim.venue, claim.editor_user = 'approved', venue, user
        claim.decided_by, claim.decided_at, claim.decision_note = request.editor_user.email, timezone.now(), note
        claim.save()
        record_audit_event(request, 'venue_claim.approved', resource_type='venue_claim', resource_id=claim.id,
                           organization_id=org.id, venue_id=venue.id,
                           detail={'journal': claim.journal_name, 'email': claim.email, 'new_account': new_account,
                                   'domain_matches': claim.domain_matches})
    sign_in = f'{_origin(request)}/admin'
    access = (f'Set your password here (the link works for {SET_PASSWORD_DAYS} days):\n{set_password_link(request, user)}\n\n'
              f'Then sign in at {sign_in}. You will set up a sign-in code app (two-step sign-in) the first time.'
              if new_account else f'Sign in with your existing Flexee account at {sign_in}.')
    _send(claim.email, f'You now manage {venue.name} on Flexee',
          f'Hello {claim.name},\n\nYour claim for {venue.name} was approved.\n\n{access}\n\n'
          f'In Venue Agents, check the journal\'s rules and save them. From then on authors see the journal as '
          f'"Editor-confirmed", with the date you last confirmed its rules.\n' + (f'\nNote from Flexee: {note}\n' if note else ''))
    return JsonResponse({'claim': claim_payload(claim), 'venue': {'id': str(venue.id), 'slug': venue.slug},
                         'new_account': new_account})


@require_POST
@require_platform_superuser
def admin_claim_reject(request, claim_id):
    data = _json(request) or {}
    note = str(data.get('note') or '').strip()[:2000]
    claim = VenueClaim.objects.filter(id=claim_id).first()
    if claim is None:
        return JsonResponse({'detail': 'Claim not found'}, status=404)
    if claim.status not in OPEN:
        return JsonResponse({'detail': 'This claim was already decided.'}, status=409)
    claim.status = 'rejected'
    claim.decided_by, claim.decided_at, claim.decision_note = request.editor_user.email, timezone.now(), note
    claim.save()
    record_audit_event(request, 'venue_claim.rejected', resource_type='venue_claim', resource_id=claim.id,
                       venue_id=claim.venue_id, detail={'journal': claim.journal_name, 'email': claim.email})
    if claim.email_verified_at:
        _send(claim.email, f'Your claim for {claim.journal_name}',
              f'Hello {claim.name},\n\nWe could not confirm that you manage {claim.journal_name}, so the claim was '
              f'not approved.' + (f'\n\n{note}' if note else '') +
              '\n\nIf this is a mistake, reply with a link to a page on the journal\'s own site that lists you, '
              'or claim again from an email address on the journal\'s domain.\n')
    return JsonResponse({'claim': claim_payload(claim)})


# ---------------------------------------------------------------------------
# A new editor sets their password
# ---------------------------------------------------------------------------

@require_POST
def editor_set_password(request):
    data = _json(request) or {}
    try:
        payload = signing.loads(str(data.get('token') or ''), salt=SET_PASSWORD_SALT, max_age=SET_PASSWORD_DAYS * 86400)
    except signing.SignatureExpired:
        return JsonResponse({'detail': 'This link has expired. Ask Flexee for a new one.'}, status=400)
    except signing.BadSignature:
        return JsonResponse({'detail': 'This link is not valid.'}, status=400)
    user = EditorUser.objects.filter(id=payload.get('user')).first()
    if user is None or payload.get('fp') != _password_fingerprint(user):
        return JsonResponse({'detail': 'This link was already used. Sign in, or ask Flexee for a new one.'}, status=400)
    password, confirm = str(data.get('password') or ''), str(data.get('confirm') or '')
    if len(password) < EDITOR_PASSWORD_MIN_LENGTH:
        return JsonResponse({'detail': f'Use at least {EDITOR_PASSWORD_MIN_LENGTH} characters.', 'field': 'password'},
                            status=400)
    if len(password) > 256 or password != confirm:
        return JsonResponse({'detail': 'The two passwords do not match.', 'field': 'confirm'}, status=400)
    user.password_hash = hash_password(password)
    user.password_changed_at = timezone.now()
    user.save(update_fields=['password_hash', 'password_changed_at', 'updated_at'])
    from .models import AuditEvent
    AuditEvent.objects.create(actor_email=user.email, actor_role='editor', action='admin.password_set_from_invite',
                              resource_type='admin_session', resource_id=str(user.id))
    return JsonResponse({'ok': True, 'email': user.email})
