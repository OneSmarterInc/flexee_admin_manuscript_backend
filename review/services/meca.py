from __future__ import annotations

import hashlib
import json
import mimetypes
import zipfile
from io import BytesIO
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

from review.storage_security import sanitize_original_filename


MECA_RECOMMENDATION_VERSION = '2.0.1'
MANIFEST_NS = 'https://manuscriptexchange.org/schema/manifest'
TRANSFER_NS = 'https://manuscriptexchange.org/schema/transfer'
REVIEWS_NS = 'https://manuscriptexchange.org/schema/reviews'
XLINK_NS = 'http://www.w3.org/1999/xlink'

MANIFEST_DOCTYPE = (
    '<!DOCTYPE manifest PUBLIC "-//MECA//DTD Manifest v1.0//en" '
    '"https://www.manuscriptexchange.org/schema/manifest-1.0.dtd">'
)
TRANSFER_DOCTYPE = (
    '<!DOCTYPE transfer PUBLIC "-//MECA//DTD Transfer v1.0//en" '
    '"https://www.manuscriptexchange.org/schema/transfer-1.0.dtd">'
)

ET.register_namespace('xlink', XLINK_NS)


class MecaPackageUnavailable(RuntimeError):
    pass


def _tag(namespace, name):
    return f'{{{namespace}}}{name}'


def _xml_bytes(root, namespace, *, doctype=None):
    ET.register_namespace('', namespace)
    payload = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    if not doctype:
        return payload
    declaration, body = payload.split(b'?>', 1)
    return declaration + b'?>\n' + doctype.encode('utf-8') + body


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def _publication(parent, venue):
    publication = ET.SubElement(
        parent,
        _tag(TRANSFER_NS, 'publication'),
        {'type': str(venue.venue_type or 'other')},
    )
    ET.SubElement(publication, _tag(TRANSFER_NS, 'publication-title')).text = str(venue.name)
    if venue.slug:
        ET.SubElement(publication, _tag(TRANSFER_NS, 'acronym')).text = str(venue.slug)[:120]
    return publication


def _service_provider(parent, venue):
    provider = ET.SubElement(parent, _tag(TRANSFER_NS, 'service-provider'))
    provider_name = getattr(getattr(venue, 'organization', None), 'name', '') or 'Flexee'
    ET.SubElement(provider, _tag(TRANSFER_NS, 'provider-name')).text = str(provider_name)
    return provider


def _transfer_xml(transfer):
    root = ET.Element(
        _tag(TRANSFER_NS, 'transfer'),
        {'transfer-version': '1.0'},
    )

    source = ET.SubElement(root, _tag(TRANSFER_NS, 'transfer-source'))
    _service_provider(source, transfer.from_submission.venue)
    _publication(source, transfer.from_submission.venue)

    destination = ET.SubElement(root, _tag(TRANSFER_NS, 'destination'))
    _service_provider(destination, transfer.to_submission.venue)
    _publication(destination, transfer.to_submission.venue)

    instructions = ET.SubElement(root, _tag(TRANSFER_NS, 'processing-instructions'))
    ET.SubElement(
        instructions,
        _tag(TRANSFER_NS, 'processing-instruction'),
        {'processing-sequence': '1'},
    ).text = 'Create a new venue-specific submission and preserve the manuscript lineage.'
    consent = 'yes' if transfer.share_review_history else 'no'
    consent_time = (
        transfer.review_history_consented_at.isoformat()
        if transfer.review_history_consented_at
        else 'not-recorded'
    )
    ET.SubElement(instructions, _tag(TRANSFER_NS, 'processing-comments')).text = (
        f'Flexee MECA {MECA_RECOMMENDATION_VERSION} transfer. '
        f'Prior review-history sharing consent: {consent}; consent timestamp: {consent_time}.'
    )

    return _xml_bytes(root, TRANSFER_NS, doctype=TRANSFER_DOCTYPE)


def _add_review_item(group, *, title, value, item_type='comments'):
    item = ET.SubElement(
        group,
        _tag(REVIEWS_NS, 'review-item'),
        {
            'is-confidential': 'no',
            'attended-for': 'editor',
            'review-item-type': item_type,
        },
    )
    question = ET.SubElement(item, _tag(REVIEWS_NS, 'review-item-question'))
    ET.SubElement(question, _tag(REVIEWS_NS, 'title')).text = str(title)
    response = ET.SubElement(item, _tag(REVIEWS_NS, 'review-item-response'))
    data = ET.SubElement(
        response,
        _tag(REVIEWS_NS, 'review-item-data'),
        {'review-item-data-type': 'text'},
    )
    data.text = value if isinstance(value, str) else _json_text(value)


def _review_history_xml(transfer):
    source = transfer.from_submission
    root = ET.Element(
        _tag(REVIEWS_NS, 'review-group'),
        {'content-version': '1.0'},
    )

    review_items = []
    if source.editorial_brief:
        review_items.append(('Prior editorial brief', source.editorial_brief, 'comments'))

    for finding in source.evidence_findings.all():
        review_items.append((
            f'Evidence: {finding.finding_type}',
            {
                'claim': finding.claim,
                'source_type': finding.source_type,
                'source_locator': finding.source_locator,
                'source_url': finding.source_url,
                'excerpt': finding.excerpt,
                'verification': finding.verification,
            },
            'comments',
        ))

    for feedback in source.editor_feedback.all():
        review_items.append((
            f'Editor feedback: {feedback.assessment_field}',
            {
                'assessment_field': feedback.assessment_field,
                'editor_value': feedback.editor_value,
                'reason': feedback.reason,
            },
            'comments',
        ))

    if review_items:
        review = ET.SubElement(
            root,
            _tag(REVIEWS_NS, 'review'),
            {
                'review-type': 'review',
                'permission-to-transfer': 'yes',
                'permission-to-publish': 'no',
                'sequence-id': '1',
            },
        )
        group = ET.SubElement(review, _tag(REVIEWS_NS, 'review-item-group'))
        for title, value, item_type in review_items:
            _add_review_item(group, title=title, value=value, item_type=item_type)

    if source.decision:
        decision = dict(source.decision)
        # Author consent to transfer history is not editor consent to disclose
        # an editor identity to a receiving system.
        decision.pop('decided_by', None)
        review = ET.SubElement(
            root,
            _tag(REVIEWS_NS, 'review'),
            {
                'review-type': 'decision',
                'permission-to-transfer': 'yes',
                'permission-to-publish': 'no',
                'sequence-id': '2',
            },
        )
        group = ET.SubElement(review, _tag(REVIEWS_NS, 'review-item-group'))
        _add_review_item(
            group,
            title='Prior editorial decision',
            value=decision,
            item_type='decision',
        )

    if not list(root):
        review = ET.SubElement(
            root,
            _tag(REVIEWS_NS, 'review'),
            {
                'review-type': 'review',
                'permission-to-transfer': 'yes',
                'permission-to-publish': 'no',
                'sequence-id': '1',
            },
        )
        group = ET.SubElement(review, _tag(REVIEWS_NS, 'review-item-group'))
        _add_review_item(
            group,
            title='Prior review history',
            value='No transferable prior review history was recorded.',
        )

    return _xml_bytes(root, REVIEWS_NS)


def _media_type(filename):
    extension = PurePosixPath(filename).suffix.lower()
    explicit = {
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.pdf': 'application/pdf',
        '.md': 'text/markdown',
        '.zip': 'application/zip',
    }
    return explicit.get(extension) or mimetypes.guess_type(filename)[0] or 'application/octet-stream'


def _manifest_xml(*, manuscript_path, manuscript_media_type, manuscript_sha256, include_reviews):
    root = ET.Element(
        _tag(MANIFEST_NS, 'manifest'),
        {'manifest-version': '1'},
    )

    def add_item(item_id, item_type, description, href, media_type):
        item = ET.SubElement(
            root,
            _tag(MANIFEST_NS, 'item'),
            {'id': item_id, 'item-type': item_type},
        )
        ET.SubElement(item, _tag(MANIFEST_NS, 'item-description')).text = description
        instance = ET.SubElement(
            item,
            _tag(MANIFEST_NS, 'instance'),
            {'media-type': media_type},
        )
        instance.set(_tag(XLINK_NS, 'href'), href)
        return item

    add_item(
        'transfer-metadata',
        'transfer-metadata',
        'MECA source and destination transfer metadata',
        'transfer.xml',
        'application/xml',
    )

    if include_reviews:
        add_item(
            'review-metadata',
            'review-metadata',
            'Prior editorial and review history shared with explicit author consent',
            'reviews.xml',
            'application/xml',
        )

    manuscript_item = add_item(
        'manuscript-source',
        'manuscript',
        'Latest manuscript source file',
        manuscript_path,
        manuscript_media_type,
    )
    metadata = ET.SubElement(manuscript_item, _tag(MANIFEST_NS, 'item-metadata'))
    ET.SubElement(
        metadata,
        _tag(MANIFEST_NS, 'metadata'),
        {'metadata-name': 'SHA-256'},
    ).text = manuscript_sha256

    return _xml_bytes(root, MANIFEST_NS, doctype=MANIFEST_DOCTYPE)


def build_meca_package(transfer):
    """Build a MECA 2.0.1-oriented package for one recorded submission transfer.

    The package is generated on demand. Prior review/editorial history is only
    included when the transfer row records explicit author consent.
    """
    manuscript = transfer.manuscript
    if manuscript.content_purged_at:
        raise MecaPackageUnavailable('Manuscript content has expired under the retention policy')
    if not manuscript.manuscript_file:
        raise MecaPackageUnavailable('Manuscript file is unavailable')

    safe_name = sanitize_original_filename(
        manuscript.manuscript_filename or manuscript.manuscript_file.name,
        default='manuscript',
    )
    manuscript_path = f'SourceFiles/{safe_name}'

    try:
        manuscript.manuscript_file.open('rb')
        manuscript_bytes = manuscript.manuscript_file.read()
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise MecaPackageUnavailable('Manuscript file is unavailable') from exc
    finally:
        try:
            manuscript.manuscript_file.close()
        except Exception:
            pass

    digest = hashlib.sha256(manuscript_bytes).hexdigest()
    if manuscript.manuscript_sha256 and digest != manuscript.manuscript_sha256:
        raise MecaPackageUnavailable('Manuscript integrity check failed')

    include_reviews = bool(transfer.share_review_history)
    files = {
        'transfer.xml': _transfer_xml(transfer),
        manuscript_path: manuscript_bytes,
    }
    if include_reviews:
        files['reviews.xml'] = _review_history_xml(transfer)

    files['manifest.xml'] = _manifest_xml(
        manuscript_path=manuscript_path,
        manuscript_media_type=_media_type(safe_name),
        manuscript_sha256=digest,
        include_reviews=include_reviews,
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        # MECA requires manifest.xml at the package root; write metadata first
        # and keep all generated paths relative and deterministic.
        archive.writestr('manifest.xml', files['manifest.xml'])
        archive.writestr('transfer.xml', files['transfer.xml'])
        if include_reviews:
            archive.writestr('reviews.xml', files['reviews.xml'])
        archive.writestr(manuscript_path, manuscript_bytes)

    return {
        'filename': f'{transfer.id}-meca.zip',
        'content': buffer.getvalue(),
        'included_review_history': include_reviews,
        'entries': ['manifest.xml', 'transfer.xml']
        + (['reviews.xml'] if include_reviews else [])
        + [manuscript_path],
        'meca_recommendation_version': MECA_RECOMMENDATION_VERSION,
    }
