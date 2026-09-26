import hashlib
import json
import os
import random
import re
import zipfile
from io import BytesIO

from django.db import transaction
from django.utils import timezone

from ..models import EvidenceFinding, ReadinessAssessment, Venue, VenueMatch, VenueSubmission
from .field_agent import _extract_citations, _verify_citation_crossref
from .ai_provider import ai_chat_json, ai_available
from .review_engine import extract_text, word_count


AGENT_VERSION = 'author-agents-v1'
DEFAULT_CHUNK_CHARS = 5600
DEFAULT_MAX_CHUNKS = 8
DEFAULT_CROSSREF_CHECKS = 5
SUPPORTED_FILES = ('.docx', '.pdf', '.md')


class AgentInputError(ValueError):
    pass


class AgentExecutionError(RuntimeError):
    def __init__(self, message, *, assessment_id=None):
        super().__init__(message)
        self.assessment_id = assessment_id


def _env_int(name, default, minimum=1, maximum=None):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _clean_text(value, limit=1200):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if len(text) > limit:
        return text[:limit - 1].rstrip() + '…'
    return text


def _clean_string_list(value, *, limit=20, item_limit=300):
    if not isinstance(value, list):
        return []
    out = []
    seen = set()
    for item in value:
        text = _clean_text(item, item_limit)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _parse_json_object(raw):
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AgentExecutionError('The local model did not return valid JSON.') from exc
    if not isinstance(data, dict):
        raise AgentExecutionError('The local model returned JSON, but not the required object shape.')
    return data


def _agent_json(prompt, *, max_tokens=700, timeout=180, operation='author_agent'):
    model, raw = ai_chat_json(
        prompt,
        max_tokens=max_tokens,
        timeout=timeout,
        operation=operation,
    )
    return model, _parse_json_object(raw)


def _read_file_bytes(manuscript):
    manuscript.manuscript_file.open('rb')
    try:
        return manuscript.manuscript_file.read()
    finally:
        manuscript.manuscript_file.close()


def load_manuscript_text(manuscript):
    """Extract one normalized text stream, including safely bounded ZIP uploads."""
    content = _read_file_bytes(manuscript)
    filename = str(manuscript.manuscript_filename or '')

    if not filename.lower().endswith('.zip'):
        return extract_text(content, filename)

    max_uncompressed = _env_int(
        'AUTHOR_ZIP_MAX_UNCOMPRESSED_BYTES',
        50 * 1024 * 1024,
        minimum=1024 * 1024,
        maximum=250 * 1024 * 1024,
    )
    max_entries = _env_int('AUTHOR_ZIP_MAX_FILES', 40, minimum=1, maximum=200)
    parts = []
    total_uncompressed = 0

    try:
        archive = zipfile.ZipFile(BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise AgentInputError('The uploaded ZIP archive is not valid.') from exc

    with archive:
        entries = []
        for info in archive.infolist():
            name = info.filename
            base = os.path.basename(name)
            if (
                info.is_dir()
                or name.startswith('__MACOSX/')
                or base.startswith('.')
                or not name.lower().endswith(SUPPORTED_FILES)
            ):
                continue
            entries.append(info)

        if not entries:
            raise AgentInputError('No .docx, .pdf, or .md manuscript files were found inside the ZIP archive.')
        if len(entries) > max_entries:
            raise AgentInputError(
                f'The ZIP contains {len(entries)} manuscript files; the configured limit is {max_entries}.'
            )

        for info in entries:
            total_uncompressed += int(info.file_size or 0)
            if total_uncompressed > max_uncompressed:
                raise AgentInputError('The ZIP expands beyond the configured safe manuscript size limit.')
            raw = archive.read(info)
            try:
                extracted = extract_text(raw, info.filename)
            except Exception as exc:
                raise AgentInputError(f'Could not extract {info.filename}: {exc}') from exc
            parts.append(f'\n\n===== FILE: {info.filename} =====\n{extracted}')

    return '\n'.join(parts).strip()


def _line_records(text):
    return [(idx, line.rstrip()) for idx, line in enumerate(str(text or '').splitlines(), 1)]


def _chunk_numbered_lines(text, *, max_chars=None):
    max_chars = max_chars or _env_int(
        'AUTHOR_AGENT_CHUNK_CHARS',
        DEFAULT_CHUNK_CHARS,
        minimum=2500,
        maximum=12000,
    )
    records = _line_records(text)
    chunks = []
    current = []
    current_chars = 0

    for line_no, line in records:
        rendered = f'[L{line_no}] {line}'
        if current and current_chars + len(rendered) + 1 > max_chars:
            chunks.append({
                'index': len(chunks) + 1,
                'start_line': current[0][0],
                'end_line': current[-1][0],
                'text': '\n'.join(item[1] for item in current),
            })
            current = []
            current_chars = 0
        current.append((line_no, rendered))
        current_chars += len(rendered) + 1

    if current:
        chunks.append({
            'index': len(chunks) + 1,
            'start_line': current[0][0],
            'end_line': current[-1][0],
            'text': '\n'.join(item[1] for item in current),
        })
    return chunks


def _select_chunks(chunks):
    max_chunks = _env_int('AUTHOR_AGENT_MAX_CHUNKS', DEFAULT_MAX_CHUNKS, minimum=1, maximum=24)
    if len(chunks) <= max_chunks:
        return chunks
    if max_chunks == 1:
        return [chunks[0]]

    # Even coverage across the manuscript so long papers do not become "first pages only".
    indexes = {
        round(i * (len(chunks) - 1) / (max_chunks - 1))
        for i in range(max_chunks)
    }
    return [chunks[i] for i in sorted(indexes)]


def _line_excerpt(text, start, end, limit=900):
    records = _line_records(text)
    if not records:
        return ''
    start = max(1, int(start or 1))
    end = max(start, int(end or start))
    end = min(end, records[-1][0])
    selected = [line for line_no, line in records if start <= line_no <= end]
    return _clean_text(' '.join(selected), limit)


def _normalise_line_range(item, chunk):
    try:
        start = int(item.get('line_start'))
        end = int(item.get('line_end', start))
    except (TypeError, ValueError, AttributeError):
        return None
    if start < chunk['start_line'] or start > chunk['end_line']:
        return None
    end = min(max(start, end), chunk['end_line'])
    return start, end


def _chunk_prompt(manuscript, chunk):
    return f"""You are the semantic readiness agent for a scholarly submission network.

Analyze ONLY the supplied manuscript chunk. Do not judge publication acceptance, do not invent facts,
and do not infer evidence outside the numbered lines. This is advisory readiness analysis; a semantic
finding can be "pass" or "warning", never a final editorial rejection.

MANUSCRIPT METADATA
Title: {manuscript.title}
Declared manuscript type: {manuscript.manuscript_type}
Author-supplied abstract: {_clean_text(manuscript.abstract, 1200)}
Author AI-use disclosure: {_clean_text(manuscript.disclosure, 800)}

CHUNK {chunk['index']} — manuscript lines {chunk['start_line']}-{chunk['end_line']}
{chunk['text']}

Return JSON only in this exact shape:
{{
  "summary": "1-3 concise sentences",
  "topics": ["topic"],
  "methods": ["method or study design actually visible in this chunk"],
  "contributions": ["claim about contribution visible in this chunk"],
  "limitations": ["limitation or unresolved issue visible in this chunk"],
  "evidence_points": [
    {{
      "kind": "topic, method, contribution, or limitation",
      "text": "short manuscript-grounded observation",
      "line_start": {chunk['start_line']},
      "line_end": {chunk['start_line']}
    }}
  ],
  "findings": [
    {{
      "code": "short_machine_code",
      "label": "short human label",
      "status": "pass or warning",
      "detail": "specific readiness observation",
      "line_start": {chunk['start_line']},
      "line_end": {chunk['start_line']}
    }}
  ]
}}

Every finding must cite line numbers from this chunk. If there is no support for a category, use an empty list.
Do not output a score, ranking, accept/reject recommendation, or claims about author intent.
"""


def _sanitize_chunk_result(data, chunk, full_text):
    result = {
        'summary': _clean_text(data.get('summary'), 900),
        'topics': _clean_string_list(data.get('topics'), limit=12, item_limit=120),
        'methods': _clean_string_list(data.get('methods'), limit=12, item_limit=220),
        'contributions': _clean_string_list(data.get('contributions'), limit=12, item_limit=260),
        'limitations': _clean_string_list(data.get('limitations'), limit=12, item_limit=260),
        'evidence_points': [],
        'findings': [],
    }
    for item in data.get('evidence_points', []) if isinstance(data.get('evidence_points'), list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get('kind', '')).strip().lower()
        if kind not in {'topic', 'method', 'contribution', 'limitation'}:
            continue
        lines = _normalise_line_range(item, chunk)
        if not lines:
            continue
        start, end = lines
        point_text = _clean_text(item.get('text'), 500)
        if not point_text:
            continue
        result['evidence_points'].append({
            'kind': kind,
            'text': point_text,
            'source': {
                'type': 'manuscript',
                'locator': f'lines {start}-{end}',
                'line_start': start,
                'line_end': end,
                'excerpt': _line_excerpt(full_text, start, end),
            },
        })
    for item in data.get('findings', []) if isinstance(data.get('findings'), list) else []:
        if not isinstance(item, dict):
            continue
        status = str(item.get('status', '')).strip().lower()
        if status not in {'pass', 'warning'}:
            continue
        lines = _normalise_line_range(item, chunk)
        if not lines:
            continue
        start, end = lines
        detail = _clean_text(item.get('detail'), 500)
        if not detail:
            continue
        result['findings'].append({
            'code': re.sub(r'[^a-z0-9_]+', '_', str(item.get('code') or 'semantic_finding').lower()).strip('_')[:80] or 'semantic_finding',
            'label': _clean_text(item.get('label') or 'Semantic readiness', 120),
            'status': status,
            'detail': detail,
            'source': {
                'type': 'manuscript',
                'locator': f'lines {start}-{end}',
                'line_start': start,
                'line_end': end,
                'excerpt': _line_excerpt(full_text, start, end),
            },
        })
    return result


def _unique(values, limit):
    out = []
    seen = set()
    for value in values:
        text = _clean_text(value, 500)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _aggregate_profile(chunk_results, *, total_chunks, analyzed_chunks):
    evidence_points = []
    for result in chunk_results:
        for point in result.get('evidence_points', []):
            evidence_points.append({
                'id': f'M{len(evidence_points) + 1:03d}',
                **point,
            })
    return {
        'summary': _clean_text(' '.join(r['summary'] for r in chunk_results if r['summary']), 2600),
        'topics': _unique((x for r in chunk_results for x in r['topics']), 24),
        'methods': _unique((x for r in chunk_results for x in r['methods']), 24),
        'contributions': _unique((x for r in chunk_results for x in r['contributions']), 24),
        'limitations': _unique((x for r in chunk_results for x in r['limitations']), 24),
        'evidence_points': evidence_points[:120],
        'coverage': {
            'total_chunks': total_chunks,
            'analyzed_chunks': analyzed_chunks,
            'complete': total_chunks == analyzed_chunks,
            'chunks_analyzed': analyzed_chunks,
            'chunks_total': total_chunks,
            'coverage_percent': int(round((analyzed_chunks / total_chunks) * 100)) if total_chunks > 0 else 0,
        },
    }


def _persist_readiness_evidence(manuscript, assessment, findings, model):
    for finding in findings:
        source = finding.get('source') if isinstance(finding, dict) else None
        if not isinstance(source, dict) or source.get('type') != 'manuscript':
            continue
        EvidenceFinding.objects.create(
            manuscript=manuscript,
            finding_type=f"agent:readiness:{finding.get('code', 'finding')}"[:100],
            claim=_clean_text(finding.get('detail'), 1200),
            source_type='manuscript',
            source_locator=_clean_text(source.get('locator'), 500),
            excerpt=_clean_text(source.get('excerpt'), 1500),
            verification={
                'assessment_id': str(assessment.id),
                'agent_version': AGENT_VERSION,
                'model': model,
                'grounding': 'validated manuscript line range',
            },
        )


def run_semantic_readiness(manuscript):
    mechanical = manuscript.readiness_assessments.filter(
        status='completed',
        engine_version__startswith='mechanical-',
    ).first()
    if not mechanical:
        raise AgentInputError('Run the deterministic readiness check before semantic readiness.')
    if not mechanical.summary.get('ready_for_matching', False):
        raise AgentInputError('Resolve blocking deterministic readiness issues before semantic readiness.')

    assessment = ReadinessAssessment.objects.create(
        manuscript=manuscript,
        status='pending',
        engine_version=f'{AGENT_VERSION}:semantic-readiness',
    )

    try:
        text = load_manuscript_text(manuscript)
        all_chunks = _chunk_numbered_lines(text)
        if not all_chunks:
            raise AgentInputError('No extractable manuscript text is available for semantic readiness.')
        selected = _select_chunks(all_chunks)

        ai_failed = False
        models = []
        results = []
        if not ai_available():
            ai_failed = True
        else:
            try:
                for chunk in selected:
                    model, raw = _agent_json(
                        _chunk_prompt(manuscript, chunk),
                        max_tokens=650,
                        operation='semantic_readiness',
                    )
                    models.append(model)
                    results.append(_sanitize_chunk_result(raw, chunk, text))
            except Exception:
                ai_failed = True

        if ai_failed:
            model = 'deterministic-fallback'
            profile = {
                'summary': 'Semantic analysis unavailable',
                'topics': [],
                'methods': [],
                'contributions': [],
                'limitations': [],
                'evidence_points': [],
                'coverage': {
                    'total_chunks': len(all_chunks), 
                    'analyzed_chunks': 0, 
                    'complete': False,
                    'chunks_analyzed': 0,
                    'chunks_total': len(all_chunks),
                    'coverage_percent': 0,
                },
            }
            semantic_findings = []
            combined_findings = list(mechanical.findings or [])
            warning_count = sum(1 for item in combined_findings if item.get('status') == 'warning')
        else:
            profile = _aggregate_profile(
                results,
                total_chunks=len(all_chunks),
                analyzed_chunks=len(selected),
            )
            semantic_findings = [item for result in results for item in result['findings']]
            combined_findings = list(mechanical.findings or []) + semantic_findings
            warning_count = sum(1 for item in combined_findings if item.get('status') == 'warning')
            model = models[0] if models else ''

        # Citation integrity is useful to the author before venue selection.
        # It remains advisory: deterministic readiness continues to control the
        # ready_for_matching gate, and a Crossref outage cannot fail readiness.
        try:
            citations = _citation_checks(text, manuscript.manuscript_sha256)
        except Exception:
            citations = {
                'total_references': 0,
                'checked': 0,
                'results': [],
                'unavailable': True,
            }

        unverified = [
            item for item in citations.get('results', [])
            if item.get('status') != 'verified'
        ]
        if citations.get('checked'):
            combined_findings = combined_findings + [{
                'code': 'citation_integrity',
                'label': 'Citation integrity (sampled)',
                'status': 'warning' if unverified else 'pass',
                'detail': (
                    f"{len(unverified)} of {citations['checked']} sampled references "
                    f"could not be verified against Crossref."
                    if unverified else
                    f"All {citations['checked']} sampled references were verified against Crossref."
                ),
                'source': {'type': 'external', 'locator': 'Crossref'},
            }]
            warning_count = sum(
                1 for item in combined_findings
                if item.get('status') == 'warning'
            )

        summary = {
            'word_count': mechanical.summary.get('word_count', word_count(text)),
            'blocking_issues': mechanical.summary.get('blocking_issues', 0),
            'warnings': warning_count,
            'citation_integrity': citations,
            'ready_for_matching': mechanical.summary.get('ready_for_matching', False),
            'semantic_advisory_only': True,
            'semantic_profile': profile,
            'coverage': profile['coverage'],
            'chunks_analyzed': profile['coverage'].get('chunks_analyzed', 0),
            'chunks_total': profile['coverage'].get('chunks_total', 0),
            'coverage_percent': profile['coverage'].get('coverage_percent', 0),
            'mechanical_assessment_id': str(mechanical.id),
            'model': model,
            'note': (
                'Deterministic checks remain the blocking readiness gate. Semantic findings are advisory '
                'and are grounded to validated manuscript line ranges.'
            ),
        }

        manuscript.parsed_profile = {
            **(manuscript.parsed_profile or {}),
            'semantic': profile,
            'semantic_model': model,
            'semantic_agent_version': AGENT_VERSION,
        }
        manuscript.save(update_fields=['parsed_profile', 'updated_at'])

        assessment.status = 'completed'
        assessment.completed_at = timezone.now()
        assessment.engine_version = f'{AGENT_VERSION}:semantic-readiness:{model}'[:100]
        assessment.summary = summary
        assessment.findings = combined_findings
        assessment.save(update_fields=['status', 'completed_at', 'engine_version', 'summary', 'findings'])
        _persist_readiness_evidence(manuscript, assessment, semantic_findings, model)
        return assessment
    except AgentInputError:
        assessment.status = 'failed'
        assessment.completed_at = timezone.now()
        assessment.error = {'detail': 'Semantic readiness input validation failed.'}
        assessment.save(update_fields=['status', 'completed_at', 'error'])
        raise
    except Exception as exc:
        assessment.status = 'failed'
        assessment.completed_at = timezone.now()
        assessment.error = {'detail': str(exc)}
        assessment.save(update_fields=['status', 'completed_at', 'error'])
        raise AgentExecutionError(str(exc), assessment_id=str(assessment.id)) from exc


def _bounded(value, *, depth=0):
    if depth > 4:
        return _clean_text(value, 300)
    if isinstance(value, dict):
        return {str(k)[:80]: _bounded(v, depth=depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [_bounded(v, depth=depth + 1) for v in value[:40]]
    if isinstance(value, str):
        return _clean_text(value, 1500)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _clean_text(value, 500)


def _config_context(config):
    return {
        'version': config.version,
        'aims_scope': config.aims_scope,
        'article_types': config.article_types,
        'accepted_methods': config.accepted_methods,
        'quality_threshold': config.quality_threshold,
        'reviewer_criteria': config.reviewer_criteria,
        'policies': config.policies,
        'disclosures': config.disclosures,
        'reporting_standards': config.reporting_standards,
        'desk_rejection_rules': config.desk_rejection_rules,
        'structured_desk_rejection_rules': config.structured_desk_rejection_rules,
        'required_submission_items': config.required_submission_items,
        'deadlines': config.deadlines,
        'submission_capacity': config.submission_capacity,
        'current_demand': config.current_demand,
    }


def _feedback_context(venue):
    rows = venue.editor_feedback.order_by('-created_at')[:6]
    return [
        {
            'assessment_field': _clean_text(row.assessment_field, 120),
            'agent_value': _bounded(row.agent_value),
            'editor_value': _bounded(row.editor_value),
            'reason': _clean_text(row.reason, 350),
        }
        for row in rows
    ]


def _profile_context(manuscript):
    profile = (manuscript.parsed_profile or {}).get('semantic', {})
    if not isinstance(profile, dict):
        return {}
    return {
        'summary': _clean_text(profile.get('summary'), 1400),
        'topics': _clean_string_list(profile.get('topics'), limit=10, item_limit=140),
        'methods': _clean_string_list(profile.get('methods'), limit=10, item_limit=180),
        'contributions': _clean_string_list(profile.get('contributions'), limit=10, item_limit=220),
        'limitations': _clean_string_list(profile.get('limitations'), limit=10, item_limit=220),
        'coverage': profile.get('coverage') if isinstance(profile.get('coverage'), dict) else {},
    }


def _compact_config_context(config):
    raw = _config_context(config)
    return {
        'version': raw['version'],
        'aims_scope': _clean_text(raw['aims_scope'], 1200),
        'article_types': _clean_string_list(raw['article_types'], limit=15, item_limit=120),
        'accepted_methods': _clean_string_list(raw['accepted_methods'], limit=15, item_limit=140),
        'quality_threshold': _clean_text(raw['quality_threshold'], 700),
        'reviewer_criteria': _clean_string_list(raw['reviewer_criteria'], limit=15, item_limit=160),
        'policies': _bounded(raw['policies']),
        'disclosures': _clean_string_list(raw['disclosures'], limit=15, item_limit=180),
        'reporting_standards': _clean_string_list(raw['reporting_standards'], limit=15, item_limit=180),
        'desk_rejection_rules': _clean_string_list(raw['desk_rejection_rules'], limit=15, item_limit=180),
        'structured_desk_rejection_rules': _bounded(raw['structured_desk_rejection_rules']),
        'required_submission_items': _bounded(raw['required_submission_items']),
        'deadlines': _bounded(raw['deadlines']),
        'submission_capacity': _bounded(raw['submission_capacity']),
        'current_demand': _bounded(raw['current_demand']),
    }


def _match_prompt(manuscript, match, config):
    profile = _profile_context(manuscript)
    context = _compact_config_context(config)
    feedback = _feedback_context(match.venue)
    return f"""You are the venue matching agent for a scholarly submission network.

Your job is to explain compatibility between ONE manuscript and ONE venue. Do not rank this venue
against other venues, do not output a numeric score, and do not choose a destination for the author.
The deterministic policy gate below is authoritative for eligibility; you may explain it but must not
change it. Use only the provided manuscript profile, venue configuration, and venue-specific editor feedback.

MANUSCRIPT
Title: {manuscript.title}
Type: {manuscript.manuscript_type}
Abstract: {_clean_text(manuscript.abstract, 1200)}
Semantic profile:
{json.dumps(profile, ensure_ascii=False)}

VENUE
Name: {match.venue.name}
Type: {match.venue.venue_type}
Deterministic eligibility: {match.eligibility}
Deterministic reasons: {json.dumps(match.reasons, ensure_ascii=False)}
Deterministic gaps: {json.dumps(match.gaps, ensure_ascii=False)}
Venue configuration:
{json.dumps(context, ensure_ascii=False)}

VENUE-SPECIFIC EDITOR FEEDBACK (may be empty; never applies to another venue):
{json.dumps(feedback, ensure_ascii=False)}

Return JSON only:
{{
  "fit_summary": "plain-language explanation",
  "reasons": [
    {{
      "text": "why the manuscript aligns",
      "venue_fields": ["aims_scope"],
      "manuscript_evidence_ids": ["M001"]
    }}
  ],
  "gaps": [
    {{
      "text": "specific missing or conflicting requirement",
      "venue_fields": ["reporting_standards"],
      "manuscript_evidence_ids": ["M001"]
    }}
  ]
}}

venue_fields may only name fields present in the supplied venue configuration.
manuscript_evidence_ids may only reference IDs that exist in semantic_profile.evidence_points.
Do not invent manuscript quotations or line numbers; matching operates on the grounded semantic profile.
Do not recommend accept/reject and do not claim the venue has selected the manuscript.
"""


def _sanitize_match_items(value, valid_evidence_ids):
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        text = _clean_text(item.get('text'), 500)
        if not text:
            continue
        out.append({
            'text': text,
            'venue_fields': _clean_string_list(item.get('venue_fields'), limit=8, item_limit=80),
            'manuscript_evidence_ids': [
                evidence_id for evidence_id in _clean_string_list(
                    item.get('manuscript_evidence_ids'), limit=8, item_limit=20
                )
                if evidence_id in valid_evidence_ids
            ],
        })
    return out


def _config_field_excerpt(config, field):
    allowed = {
        'aims_scope', 'article_types', 'accepted_methods', 'quality_threshold', 'reviewer_criteria',
        'policies', 'disclosures', 'reporting_standards', 'desk_rejection_rules',
        'structured_desk_rejection_rules', 'required_submission_items', 'deadlines',
        'submission_capacity', 'current_demand',
    }
    if field not in allowed:
        return None
    return _clean_text(json.dumps(getattr(config, field), ensure_ascii=False), 900)


def _profile_evidence_map(manuscript):
    profile = (manuscript.parsed_profile or {}).get('semantic', {})
    points = profile.get('evidence_points') if isinstance(profile, dict) else []
    return {
        str(point.get('id')): point
        for point in points or []
        if isinstance(point, dict) and point.get('id')
    }


def _match_evidence(match, config, reasons, gaps):
    evidence = []
    manuscript_evidence = _profile_evidence_map(match.manuscript)
    for kind, items in [('reason', reasons), ('gap', gaps)]:
        for item in items:
            for field in item['venue_fields']:
                excerpt = _config_field_excerpt(config, field)
                if excerpt is None:
                    continue
                evidence.append({
                    'finding': kind,
                    'claim': item['text'],
                    'source_type': 'venue_policy',
                    'source_locator': f'venue config v{config.version} · {field}',
                    'excerpt': excerpt,
                })
            for evidence_id in item['manuscript_evidence_ids']:
                point = manuscript_evidence.get(evidence_id)
                if not point:
                    continue
                source = point.get('source') if isinstance(point.get('source'), dict) else {}
                evidence.append({
                    'finding': kind,
                    'claim': item['text'],
                    'source_type': 'manuscript',
                    'source_locator': source.get('locator', 'manuscript'),
                    'excerpt': source.get('excerpt', ''),
                    'manuscript_evidence_id': evidence_id,
                })
    return evidence[:60]


def run_semantic_matching(manuscript, *, venue_ids=None):
    profile = (manuscript.parsed_profile or {}).get('semantic')
    if not isinstance(profile, dict) or not profile:
        raise AgentInputError('Run semantic readiness before semantic venue matching.')

    queryset = manuscript.venue_matches.select_related('venue', 'venue__organization', 'venue_config')
    if venue_ids:
        queryset = queryset.filter(venue_id__in=venue_ids)
    matches = list(queryset)
    if not matches:
        raise AgentInputError('Run the deterministic venue policy gate before semantic venue matching.')

    models = set()
    errors = []
    updated = []

    for match in matches:
        config = match.venue_config or match.venue.agent_configs.filter(active=True).order_by('-version').first()
        if not config:
            errors.append({'venue_id': str(match.venue_id), 'detail': 'No active venue configuration.'})
            updated.append(match)
            continue
        try:
            if not ai_available() or profile.get('summary') == 'Semantic analysis unavailable':
                raise RuntimeError('AI unavailable')
            model, data = _agent_json(
                _match_prompt(manuscript, match, config),
                max_tokens=700,
                operation='semantic_matching',
            )
            models.add(model)
            evidence_ids = set(_profile_evidence_map(manuscript))
            reasons = _sanitize_match_items(data.get('reasons'), evidence_ids)
            gaps = _sanitize_match_items(data.get('gaps'), evidence_ids)
            semantic_reason_text = [item['text'] for item in reasons]
            semantic_gap_text = [item['text'] for item in gaps]
            
            fit_summary = _clean_text(data.get('fit_summary'), 1200)
            evidence = _match_evidence(match, config, reasons, gaps)
        except Exception as exc:
            errors.append({'venue_id': str(match.venue_id), 'detail': str(exc)})
            models.add('deterministic-fallback')
            fit_summary = 'Semantic analysis unavailable'
            semantic_reason_text = []
            semantic_gap_text = []
            evidence = []

        # Preserve deterministic gate reasons/gaps; semantic output can explain, never override the gate.
        match.fit_summary = fit_summary
        match.reasons = _unique(list(match.reasons or []) + semantic_reason_text, 40)
        match.gaps = _unique(list(match.gaps or []) + semantic_gap_text, 40)
        match.evidence = evidence
        match.venue_config = config
        match.save(update_fields=['fit_summary', 'reasons', 'gaps', 'evidence', 'venue_config'])
        updated.append(match)

    return {
        'matches': updated,
        'models': sorted(models),
        'errors': errors,
        'agent_version': AGENT_VERSION,
    }


def _citation_checks(text, manuscript_sha):
    total, citations = _extract_citations(text)
    if not citations:
        return {'total_references': total, 'checked': 0, 'results': []}

    count = min(
        _env_int('AUTHOR_CROSSREF_CHECKS', DEFAULT_CROSSREF_CHECKS, minimum=1, maximum=12),
        len(citations),
    )
    rng = random.Random(str(manuscript_sha or ''))
    sampled = rng.sample(citations, count) if len(citations) > count else list(citations)
    results = []
    for citation in sampled:
        result = _verify_citation_crossref(citation)
        results.append({
            'citation': _clean_text(citation, 700),
            'status': result.get('status', 'not found'),
            'matched_title': _clean_text(result.get('matched_title'), 400) or None,
            'score': result.get('score', 0.0),
            'title_similarity': result.get('title_similarity', 0.0),
            'doi': result.get('doi'),
        })
    return {'total_references': total, 'checked': len(results), 'results': results}


def _assessment_prompt(submission, config, citation_checks):
    manuscript = submission.manuscript
    profile = _profile_context(manuscript)
    match = manuscript.venue_matches.filter(venue=submission.venue).first()
    match_context = {
        'eligibility': match.eligibility if match else None,
        'fit_summary': match.fit_summary if match else '',
        'reasons': match.reasons if match else [],
        'gaps': match.gaps if match else [],
    }
    return f"""You are the venue-specific assessment agent for a scholarly submission network.

Prepare an evidence-oriented editorial brief for HUMAN editors. Do not make or imply a publication
decision, do not recommend accept/reject, and do not invent manuscript quotations. The author has
already selected this venue. Evaluate only against this venue's own configuration.

MANUSCRIPT
Title: {manuscript.title}
Type: {manuscript.manuscript_type}
Abstract: {_clean_text(manuscript.abstract, 1200)}
Grounded semantic profile:
{json.dumps(profile, ensure_ascii=False)}

VENUE
Name: {submission.venue.name}
Configuration version: {config.version}
Configuration:
{json.dumps(_compact_config_context(config), ensure_ascii=False)}

MATCH CONTEXT
{json.dumps(_bounded(match_context), ensure_ascii=False)}

VENUE-SPECIFIC EDITOR FEEDBACK
{json.dumps(_feedback_context(submission.venue), ensure_ascii=False)}

DETERMINISTIC EXTERNAL REFERENCE CHECK
{json.dumps(_bounded(citation_checks), ensure_ascii=False)}

Return JSON only:
{{
  "editor_summary": "<concise brief for a human editor>",
  "outlet_fit": {{"summary": "<evaluate how well the manuscript matches the venue aims and scope>", "venue_fields": ["aims_scope"], "manuscript_evidence_ids": ["M001"]}},
  "policy_compliance": {{"summary": "<evaluate adherence to venue policies>", "venue_fields": ["policies"], "manuscript_evidence_ids": ["M001"]}},
  "contribution": {{"summary": "<evaluate the novelty and significance>", "venue_fields": ["quality_threshold"], "manuscript_evidence_ids": ["M001"]}},
  "methods": {{"summary": "<evaluate the methodology used>", "venue_fields": ["accepted_methods"], "manuscript_evidence_ids": ["M001"]}},
  "citation_integrity": {{"summary": "<evaluate the references and citations>", "venue_fields": [], "manuscript_evidence_ids": ["M001"]}},
  "unresolved_risks": [
    {{"risk": "<describe a specific risk or note none found>", "venue_fields": ["reporting_standards"], "manuscript_evidence_ids": ["M001"]}}
  ],
  "reviewer_expertise": ["<specific expertise area>"]
}}

You MUST populate every section (outlet_fit, policy_compliance, contribution, methods, citation_integrity) with a meaningful summary replacing the <...> placeholders. If there are no issues, describe why it complies rather than leaving it empty. Unresolved risks must list any concerns; if none exist, you must still provide at least one item explaining that no major risks were found. Reviewer expertise must suggest 1-3 specific areas based on the manuscript.
The Crossref check is deterministic input: summarize it accurately and do not upgrade "weak match" or
"not found" to "verified". If a venue rule is not configured, say that it is not configured rather
than inventing one. Editor feedback is outlet-specific guidance and must not be generalized to other venues.
"""


def _assessment_section(value, valid_evidence_ids, key='summary'):
    if not isinstance(value, dict):
        value = {}
    return {
        key: _clean_text(value.get(key), 1200),
        'venue_fields': _clean_string_list(value.get('venue_fields'), limit=10, item_limit=80),
        'manuscript_evidence_ids': [
            evidence_id for evidence_id in _clean_string_list(
                value.get('manuscript_evidence_ids'), limit=10, item_limit=20
            )
            if evidence_id in valid_evidence_ids
        ],
    }


def _persist_assessment_evidence(submission, config, brief, citation_checks, model):
    manuscript = submission.manuscript
    created = []
    manuscript_evidence = _profile_evidence_map(manuscript)

    EvidenceFinding.objects.filter(
        venue_submission=submission,
        finding_type__startswith='agent:',
    ).delete()

    section_names = ['outlet_fit', 'policy_compliance', 'contribution', 'methods', 'citation_integrity']
    for section_name in section_names:
        section = brief.get(section_name, {})
        claim = section.get('summary', '')
        for field in section.get('venue_fields', []):
            excerpt = _config_field_excerpt(config, field)
            if excerpt is None:
                continue
            created.append(EvidenceFinding.objects.create(
                manuscript=manuscript,
                venue_submission=submission,
                finding_type=f'agent:{section_name}:venue_policy'[:100],
                claim=claim,
                source_type='venue_policy',
                source_locator=f'venue config v{config.version} · {field}',
                excerpt=excerpt,
                verification={'agent_version': AGENT_VERSION, 'model': model},
            ))
        for evidence_id in section.get('manuscript_evidence_ids', []):
            point = manuscript_evidence.get(evidence_id)
            if not point:
                continue
            source = point.get('source') if isinstance(point.get('source'), dict) else {}
            created.append(EvidenceFinding.objects.create(
                manuscript=manuscript,
                venue_submission=submission,
                finding_type=f'agent:{section_name}:manuscript'[:100],
                claim=claim,
                source_type='manuscript',
                source_locator=_clean_text(source.get('locator') or 'manuscript', 500),
                excerpt=_clean_text(source.get('excerpt'), 1500),
                verification={
                    'agent_version': AGENT_VERSION,
                    'model': model,
                    'manuscript_evidence_id': evidence_id,
                    'grounding': 'validated manuscript line range',
                },
            ))

    for risk in brief.get('unresolved_risks', []):
        claim = risk.get('risk', '')
        for field in risk.get('venue_fields', []):
            excerpt = _config_field_excerpt(config, field)
            if excerpt is None:
                continue
            created.append(EvidenceFinding.objects.create(
                manuscript=manuscript,
                venue_submission=submission,
                finding_type='agent:unresolved_risk:venue_policy',
                claim=claim,
                source_type='venue_policy',
                source_locator=f'venue config v{config.version} · {field}',
                excerpt=excerpt,
                verification={'agent_version': AGENT_VERSION, 'model': model},
            ))

    for result in citation_checks.get('results', []):
        doi = result.get('doi')
        created.append(EvidenceFinding.objects.create(
            manuscript=manuscript,
            venue_submission=submission,
            finding_type='agent:citation_integrity:crossref',
            claim=f"Crossref check: {result.get('status', 'not found')}",
            source_type='external',
            source_locator='Crossref',
            source_url=f'https://doi.org/{doi}' if doi else '',
            excerpt=result.get('citation', ''),
            verification={
                'status': result.get('status'),
                'matched_title': result.get('matched_title'),
                'score': result.get('score'),
                'title_similarity': result.get('title_similarity'),
                'doi': doi,
            },
        ))
    return created


def _anonymization_prompt(text):
    return f"""You are an editorial assistant checking a manuscript for double-blind review compliance.
Analyze the text for ANY identifying information. Look specifically for:
1. Author names
2. Author emails
3. Affiliations (universities, companies, labs)
4. Self-referential statements/citations (e.g., 'In our previous work (Smith et al. 2023)')

Return a JSON object with:
{{
  "status": "passed|blocked",
  "issues": [
    {{
      "type": "name|email|affiliation|self_citation",
      "location": "...",
      "evidence": "..."
    }}
  ]
}}
If you find ANY identifying information, status MUST be 'blocked'. Otherwise 'passed'.

MANUSCRIPT TEXT:
{text[:12000]}
"""


def run_venue_assessment(submission):
    if submission.status not in {'draft', 'packet_ready'}:
        raise AgentInputError(f'Venue assessment cannot run from status {submission.status}.')
    config = submission.venue_config or submission.venue.agent_configs.filter(active=True).order_by('-version').first()
    if not config:
        raise AgentInputError('The selected venue does not have an active venue-agent configuration.')
    profile = (submission.manuscript.parsed_profile or {}).get('semantic')
    if not isinstance(profile, dict) or not profile:
        raise AgentInputError('Run semantic readiness before the venue-specific assessment.')

    text = load_manuscript_text(submission.manuscript)

    policies = config.policies or {}
    if policies.get('blind_review', False) and not (submission.packet or {}).get('anonymization_override', False):
        if ai_available() and profile.get('summary') != 'Semantic analysis unavailable':
            model_anon, data_anon = _agent_json(
                _anonymization_prompt(text),
                max_tokens=1000,
                timeout=120,
                operation='anonymization_check',
            )
            status_anon = data_anon.get('status', 'passed')
            if status_anon == 'blocked':
                packet = dict(submission.packet or {})
                packet['anonymization'] = data_anon
                packet['editorial_brief_ready'] = False
                submission.packet = packet
                submission.save(update_fields=['packet'])
                return submission
            else:
                packet = dict(submission.packet or {})
                packet['anonymization'] = {"status": "passed", "issues": []}
                submission.packet = packet
                submission.save(update_fields=['packet'])

    # Reuse the citation result produced during semantic readiness. This avoids
    # repeating external Crossref calls after the author chooses a venue. Older
    # manuscripts without stored citation data keep the previous fallback.
    semantic_assessment = submission.manuscript.readiness_assessments.filter(
        status='completed',
        engine_version__contains='semantic-readiness',
    ).first()
    citations = (
        (semantic_assessment.summary or {}).get('citation_integrity')
        if semantic_assessment else None
    )
    if not isinstance(citations, dict):
        try:
            citations = _citation_checks(text, submission.manuscript.manuscript_sha256)
        except Exception:
            citations = {
                'total_references': 0,
                'checked': 0,
                'results': [],
                'unavailable': True,
            }

    try:
        if not ai_available() or profile.get('summary') == 'Semantic analysis unavailable':
            raise RuntimeError('AI unavailable')
        model, data = _agent_json(
            _assessment_prompt(submission, config, citations),
            max_tokens=1100,
            timeout=240,
            operation='venue_assessment',
        )
    except Exception as exc:
        model = 'deterministic-fallback'
        data = {
            'editor_summary': 'Semantic analysis unavailable. The manuscript was processed with deterministic checks only.',
            'outlet_fit': {'summary': 'Semantic analysis unavailable'},
            'policy_compliance': {'summary': 'Semantic analysis unavailable'},
            'contribution': {'summary': 'Semantic analysis unavailable'},
            'methods': {'summary': 'Semantic analysis unavailable'},
            'citation_integrity': {'summary': 'Semantic analysis unavailable'},
            'unresolved_risks': [],
            'reviewer_expertise': []
        }

    valid_evidence_ids = set(_profile_evidence_map(submission.manuscript))
    brief = {
        'agent_version': AGENT_VERSION,
        'model': model,
        'generated_at': timezone.now().isoformat(),
        'venue_config_version': config.version,
        'editor_summary': _clean_text(data.get('editor_summary'), 1800),
        'outlet_fit': _assessment_section(data.get('outlet_fit'), valid_evidence_ids),
        'policy_compliance': _assessment_section(data.get('policy_compliance'), valid_evidence_ids),
        'contribution': _assessment_section(data.get('contribution'), valid_evidence_ids),
        'methods': _assessment_section(data.get('methods'), valid_evidence_ids),
        'citation_integrity': _assessment_section(data.get('citation_integrity'), valid_evidence_ids),
        'unresolved_risks': [],
        'reviewer_expertise': _clean_string_list(data.get('reviewer_expertise'), limit=16, item_limit=180),
        'external_reference_check': citations,
        'analysis_coverage': profile.get('coverage', {}),
        'human_decision_required': True,
        'decision_authority': (
            'This brief is advisory. The agent does not accept, reject, or desk-reject '
            'a manuscript. Every editorial decision is made by a human editor at the venue.'
        ),
    }

    risks = data.get('unresolved_risks') if isinstance(data.get('unresolved_risks'), list) else []
    for item in risks[:20]:
        if not isinstance(item, dict):
            continue
        risk = _clean_text(item.get('risk'), 700)
        if not risk:
            continue
        brief['unresolved_risks'].append({
            'risk': risk,
            'venue_fields': _clean_string_list(item.get('venue_fields'), limit=8, item_limit=80),
            'manuscript_evidence_ids': [
                evidence_id for evidence_id in _clean_string_list(
                    item.get('manuscript_evidence_ids'), limit=8, item_limit=20
                )
                if evidence_id in valid_evidence_ids
            ],
        })

    with transaction.atomic():
        submission = VenueSubmission.objects.select_for_update().get(id=submission.id)
        evidence = _persist_assessment_evidence(submission, config, brief, citations, model)
        brief['evidence_ids'] = [str(item.id) for item in evidence]
        submission.venue_config = config
        submission.editorial_brief = brief
        if submission.status == 'draft':
            submission.status = 'packet_ready'
        packet = dict(submission.packet or {})
        packet['venue_config_version'] = config.version
        packet['editorial_brief_ready'] = True
        packet['evidence_count'] = len(evidence)
        submission.packet = packet
        submission.save(update_fields=['venue_config', 'editorial_brief', 'status', 'packet', 'updated_at'])

    return submission
