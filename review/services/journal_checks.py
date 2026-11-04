"""Measured checks only. Unsupported rules remain unknown; never infer a pass from silence."""
import hashlib
import json
import re

ENGINE = 'journal-checks-v3'
NUMERIC = {'>', '>=', '<', '<=', '==', '!='}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def evaluate_rules(manuscript, config, document):
    from ..author_api import _numeric_rule_triggered, _section_present
    from .review_engine import word_count
    from .field_agent import _extract_citations
    readable = bool(document and document.strip())
    rows = []
    rules = config.structured_desk_rejection_rules or []
    if config.article_types:
        normalize = lambda x: re.sub(r'[^a-z0-9]', '', str(x).lower())
        label = dict(manuscript.TYPE_CHOICES).get(manuscript.manuscript_type, manuscript.manuscript_type)
        ok = bool({normalize(label), normalize(manuscript.manuscript_type)} & {normalize(x) for x in config.article_types})
        rows.append(dict(id='article_types', title='Accepted article type', status='passed' if ok else 'not_checked',
                         actual=label, expected=config.article_types, location='Submission metadata',
                         action='Confirm the article type is accepted; consider another journal if it is not.',
                         rule_source='article_types'))
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            rule = {'message': 'Unrecognized rule format'}
        field, op, expected = rule.get('field'), rule.get('operator'), rule.get('value')
        actual, triggered = None, None
        location = 'Manuscript text'
        action = 'Review the recorded journal requirement manually.'
        try:
            if readable and field in {'word_count', 'reference_count'} and op in NUMERIC:
                actual = word_count(document) if field == 'word_count' else _extract_citations(document)[0]
                triggered = _numeric_rule_triggered(actual, op, int(expected))
                location = 'Extracted document text (all text counted)' if field == 'word_count' else 'Detected references'
                if op in {'>', '>='}:
                    limit = int(expected) - (1 if op == '>=' else 0)
                    action = f'Reduce the count from {actual} to {limit} or fewer, using the journal’s counting rules.'
                elif op in {'<', '<='}:
                    limit = int(expected) + (1 if op == '<=' else 0)
                    action = f'Address the shortfall: detected {actual}; required minimum {limit}. Do not add filler.'
            elif readable and field == 'required_sections' and op in {'missing_any', 'missing_all'} and isinstance(expected, list) and expected:
                missing = [str(x) for x in expected if not _section_present(document, x)]
                actual = {'missing': missing}
                triggered = bool(missing) if op == 'missing_any' else len(missing) == len(expected)
                location = 'Document section headings'
                action = 'Add or clearly label the missing sections: ' + ', '.join(missing)
            elif field == 'manuscript_type' and op in {'==', '!=', 'in', 'not_in'}:
                actual = manuscript.manuscript_type
                location = 'Submission metadata'
                if op == '==': triggered = actual == expected
                elif op == '!=': triggered = actual != expected
                elif isinstance(expected, list): triggered = (actual in expected) if op == 'in' else (actual not in expected)
                action = 'Confirm the accepted article types. Changing a label alone does not change the study type.'
            elif field == 'disclosure' and op in {'contains', 'not_contains', 'empty', 'not_empty'}:
                actual = manuscript.disclosure or ''
                location = 'Author disclosure metadata (not the document)'
                if op == 'contains': triggered = str(expected).lower() in actual.lower()
                elif op == 'not_contains': triggered = str(expected).lower() not in actual.lower()
                elif op == 'empty': triggered = not actual.strip()
                else: triggered = bool(actual.strip())
                actual = 'Disclosure provided' if actual.strip() else 'Disclosure missing'
                action = 'Update the disclosure in your manuscript details truthfully; uploading a file does not change this metadata.'
        except (ValueError, TypeError, OverflowError):
            triggered = None
        rows.append(dict(id='rule-' + digest(rule), title=rule.get('message') or str(field or 'Journal rule').replace('_', ' '),
                         status='not_checked' if triggered is None else 'failed' if triggered else 'passed',
                         actual=actual, expected=expected, operator=op, location=location, action=action,
                         rule_source=f'structured_desk_rejection_rules[{index}]'))
    for index, item in enumerate(config.required_submission_items or []):
        if not isinstance(item, dict): continue
        rows.append(dict(id='item-' + digest(item), title=item.get('label') or 'Submission item', status='not_checked',
                         actual=None, expected='Required' if item.get('required') is True else 'Check applicability',
                         location='Submission packet', action='Prepare and review this submission item. Its presence is not verified by this document check.',
                         rule_source=f'required_submission_items[{index}]'))
    # Coverage is explicit even when a journal has no machine-readable rules.
    rows.append(dict(id='manual-coverage', title='Other journal requirements', status='not_checked', actual=None,
                     expected='Review the complete official instructions', location='Official guidelines',
                     action='Formatting, anonymization, abstract/keyword limits stored as prose, ethics and qualitative requirements need manual review unless separately verified.',
                     rule_source='Official guidelines'))
    if not readable:
        rows.append(dict(id='file-readability', title='Document text unavailable', status='not_checked', actual=None,
                         expected='Readable manuscript text', location=manuscript.manuscript_filename,
                         action='Upload a readable document; unmeasurable rules have not passed.', rule_source='Document extraction'))
    if (config.policies or {}).get('document_check_profile') == 'oscm-2026-03-01':
        from .oscm_checks import check_oscm
        rows.extend(check_oscm(manuscript))
    else:
        profile = (config.policies or {}).get('verified_document_checks')
        if isinstance(profile, dict) and profile.get('source_url') and profile.get('id'):
            from .profile_document_checks import check_profile
            rows.extend(check_profile(manuscript, profile))
    return rows


def compare_checks(rows, previous):
    old = {x['id']: x for x in previous or []}
    for row in rows:
        before = old.get(row['id'])
        row['previous_status'] = before.get('status') if before else None
        row['change'] = ('resolved' if before and before['status'] == 'failed' and row['status'] == 'passed'
                         else 'still_needs_changes' if before and before['status'] == 'failed' and row['status'] == 'failed'
                         else 'new_issue' if row['status'] == 'failed' else None)
    return rows
