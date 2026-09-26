import json
import os
import re
from io import BytesIO
from docx import Document
from pypdf import PdfReader

from .ai_provider import ai_chat_json
from .local_llm import assert_prompt_fits_context, DEFAULT_OLLAMA_NUM_CTX, DEFAULT_OLLAMA_NUM_PREDICT

BOOK_STRUCTURE = {
    'chapters_required': 12,
    'total_words_min': 25000,
    'total_words_max': 30000,
    'opening_chapter_min': 1000,
    'opening_chapter_max': 1900,
    'body_chapter_ideal_min': 2100,
    'body_chapter_ideal_max': 2550,
    'body_chapter_hard_min': 1800,
    'body_chapter_hard_max': 2800,
    'figures_required': 40,
}
ARTICLE_STRUCTURE = {'total_words_min': 1500, 'total_words_max': 3000}

BOOK_JUDGMENT = [
    {
        'id': 'sim_fit', 'label': 'Pairs with the named simulation', 'advisory': False,
        'criterion': 'The book must be a companion to the specific Flexee simulation the author named, not a general textbook. A student running that sim should be taught by this book: it should reference the decisions, roles, and mechanics that sim actually presents. Pass if the pairing is real and specific. needs_work if it gestures at the sim but the link is thin. Fail if it is a generic treatment that would read the same with the sim removed.'
    },
    {
        'id': 'teaches_claim', 'label': 'Teaches what it claims', 'advisory': False,
        'criterion': "The chapters must deliver the learning the introduction and table of contents promise. Check that each chapter's content matches its stated aim and that the sequence builds. Pass if the book delivers on its own promises. needs_work if one or two chapters drift or thin out. Fail if the content does not teach what the front matter says it will."
    },
    {
        'id': 'ai_disclosure', 'label': 'AI-use disclosure present and specific', 'advisory': False,
        'criterion': 'Policy: AI assistance in writing is welcome; a fully AI-authored manuscript is not. The author must include a disclosure that states specifically how AI was used (for example: drafting, figure generation, editing, research). Pass if a specific disclosure is present. needs_work if a disclosure exists but is vague. Fail if there is no disclosure at all.'
    },
    {
        'id': 'ai_authored_signal', 'label': 'Human authorship signal (advisory)', 'advisory': True,
        'criterion': 'Advisory only. Note whether the manuscript reads as substantially human-authored with AI assistance or as largely AI-generated with a thin human hand. Report specific signals, but do not fail the book on this alone. pass = reads as human-led; needs_work = mixed signals worth a human look; fail is not used for this advisory item.'
    },
]

ARTICLE_JUDGMENT = [
    {
        'id': 'four_parts', 'label': 'The four parts are all present and substantive', 'advisory': False,
        'criterion': "The article must cover four things, each with real substance: (1) the business and its problem, (2) what was tried, (3) what happened, and (4) what didn't work. Pass if all four are present and carry weight. needs_work if one is thin. Fail if a part is missing or if 'what didn't work' is absent."
    },
    {
        'id': 'journal_fit', 'label': 'Fits the journal', 'advisory': False,
        'criterion': 'Field Notes Journal publishes practitioner accounts of how a business actually used AI, including the limits. Pass if this is a grounded, first-hand account. needs_work if it is grounded but drifts toward generality. Fail if it is vendor marketing, an abstract think-piece, or has no real business behind it.'
    },
    {
        'id': 'ai_disclosure', 'label': 'AI-use disclosure present and specific', 'advisory': False,
        'criterion': 'AI assistance is welcome, a fully AI-authored article is not, and the author must disclose specifically how AI was used in writing the piece. Pass if specific, needs_work if vague, fail if absent.'
    },
    {
        'id': 'ai_authored_signal', 'label': 'Human authorship signal (advisory)', 'advisory': True,
        'criterion': 'Advisory only: report whether the piece reads as human-led with AI help or as largely machine-generated, with specific signals, and leave the call to the human reviewer. Never fail on this alone.'
    },
]

DECISION_PASS = 'PASS_TO_HUMAN'
DECISION_REFER = 'REFER_TO_HUMAN_WITH_FLAGS'
DECISION_FAIL = 'RETURN_TO_AUTHOR'


def compute_decision(measured, judgments):
    """Compute the final review decision deterministically in Python.

    The model may explain criteria and provide evidence, but it must not own the
    final gate decision.  These rules intentionally mirror the acceptance policy:
    structural failure and required-criterion failure return to the author;
    needs-work findings refer to a human; only a clean pass advances.
    """
    checks = measured.get('checks', []) if isinstance(measured, dict) else []
    if any(not check.get('passed') for check in checks):
        return DECISION_FAIL

    for item in judgments or []:
        if item.get('verdict') == 'fail' and not item.get('advisory', False):
            return DECISION_FAIL

    if any(item.get('verdict') == 'needs_work' for item in judgments or []):
        return DECISION_REFER

    return DECISION_PASS


def normalize_text(text):
    return str(text or '').replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '').strip()


def word_count(text):
    return len(re.findall(r"[\w'-]+", str(text or ''), flags=re.UNICODE))


def extract_text(content, filename):
    name = str(filename or '').lower()
    if name.endswith('.docx'):
        doc = Document(BytesIO(content))
        return normalize_text('\n'.join(p.text for p in doc.paragraphs))
    if name.endswith('.pdf'):
        reader = PdfReader(BytesIO(content))
        return normalize_text('\n'.join((page.extract_text() or '') for page in reader.pages))
    if name.endswith('.md'):
        return normalize_text(content.decode('utf-8', errors='replace'))
    raise ValueError('Manuscript must be a .docx, .pdf, or .md file')


def _chapter_heading_candidate(value):
    """Return (chapter_number, title) for a heading-like Chapter 1..12 line.

    PDF text extraction often repeats the current chapter title as a running page
    header.  It can also contain a compact table of contents.  Keep candidates
    deliberately heading-like so ordinary prose such as "Chapter 2 explains..."
    is less likely to be treated as a boundary.
    """
    value = value.strip()
    if not value or len(value) > 180:
        return None
    match = re.match(r'^chapter\s+(\d{1,2})\b', value, re.I)
    if not match:
        return None
    number = int(match.group(1))
    if not 1 <= number <= BOOK_STRUCTURE['chapters_required']:
        return None
    # A normal prose sentence beginning with "Chapter N" is usually terminated
    # by a period.  Dot leaders in a TOC are intentionally retained; they are
    # handled later by choosing the sequence with the largest manuscript span.
    if value.endswith('.') and '...' not in value:
        return None
    if word_count(value) > 20:
        return None
    return number, value


def _best_numbered_chapter_sequence(lines):
    """Choose the real chapter run from noisy PDF/DOCX text.

    A book may contain two 1..12 runs: one tightly packed in the table of contents
    and one spread across the manuscript body.  It may also repeat "Chapter N" on
    every page.  Starting from every plausible Chapter 1 (or partial-run start),
    greedily build the monotonic sequence and prefer the run covering the most
    body words.  This makes the body beat the TOC and the first real chapter header
    beat later repeated running headers.
    """
    candidates = []
    for index, line in enumerate(lines):
        parsed = _chapter_heading_candidate(line)
        if parsed:
            number, title = parsed
            candidates.append({'index': index, 'number': number, 'title': title})
    if not candidates:
        return []

    sequences = []
    for start_pos, start in enumerate(candidates):
        sequence = [start]
        expected = start['number'] + 1
        cursor = start['index']
        for candidate in candidates[start_pos + 1:]:
            if expected > BOOK_STRUCTURE['chapters_required']:
                break
            if candidate['index'] <= cursor:
                continue
            if candidate['number'] == expected:
                sequence.append(candidate)
                cursor = candidate['index']
                expected += 1
        if sequence:
            first = sequence[0]['index']
            last = sequence[-1]['index']
            span_words = word_count(' '.join(lines[first:last + 1]))
            # Prefer a run that starts at Chapter 1, then the largest manuscript
            # span, then the most sequential chapters.  The span criterion is what
            # prevents a 12-line table of contents from winning over the body.
            starts_at_one = 1 if sequence[0]['number'] == 1 else 0
            sequences.append((starts_at_one, span_words, len(sequence), -first, sequence))

    if not sequences:
        return []
    sequences.sort(key=lambda item: item[:4], reverse=True)
    return sequences[0][4]


def _strip_repeated_chapter_headers(lines, chapter_number):
    """Remove repeated running headers for the chapter being measured."""
    cleaned = []
    for line in lines:
        parsed = _chapter_heading_candidate(line)
        if parsed and parsed[0] == chapter_number:
            continue
        cleaned.append(line)
    return cleaned


def _chapter_data(text):
    lines = normalize_text(text).split('\n')
    sequence = _best_numbered_chapter_sequence(lines)

    if sequence:
        back_matter = re.compile(
            r'^\s*(references|bibliography|appendix(?:\s+[a-z0-9]+)?|index|glossary|'
            r'endnotes|acknowledg(?:e)?ments|about the author|ai[\s-]?use disclosure|disclosure)\b',
            re.I,
        )
        chapters = []
        for pos, item in enumerate(sequence):
            start = item['index']
            if pos + 1 < len(sequence):
                end = sequence[pos + 1]['index']
            else:
                end = len(lines)
                # Keep chapter 12 from absorbing references/index/back matter.
                for idx in range(start + 1, len(lines)):
                    if back_matter.match(lines[idx].strip()):
                        end = idx
                        break
            body_lines = _strip_repeated_chapter_headers(lines[start + 1:end], item['number'])
            chapters.append({
                'title': item['title'],
                'number': item['number'],
                'word_count': word_count(' '.join(body_lines)),
            })
        return chapters

    # Markdown fallback for manuscripts that use headings instead of explicit
    # "Chapter N" labels.  Prefer a single heading level rather than mixing H1/H2
    # sections, which would turn subsections into chapters.
    excluded = re.compile(
        r'\b(contents|foreword|preface|acknowledg|references|bibliography|appendix|'
        r'index|glossary|disclosure|ai use|use of ai)\b', re.I,
    )
    by_level = {1: [], 2: []}
    for i, line in enumerate(lines):
        value = line.strip()
        match = re.match(r'^(#{1,2})\s+(\S.*)$', value)
        if match and not excluded.search(value):
            level = len(match.group(1))
            by_level[level].append((i, match.group(2).strip()))

    # Choose the level closest to the expected 12 chapters.  If H1 has a single
    # document title and H2 has the chapter headings, H2 naturally wins.
    options = [items for items in by_level.values() if items]
    if not options:
        return []
    starts = min(options, key=lambda items: (abs(len(items) - BOOK_STRUCTURE['chapters_required']), -len(items)))
    # A common Markdown layout is one H1 book title followed by twelve same-level
    # chapter headings.  Drop only that obvious extra leading title.
    if len(starts) == BOOK_STRUCTURE['chapters_required'] + 1:
        starts = starts[1:]

    chapters = []
    for pos, (start, title) in enumerate(starts):
        end = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
        chapters.append({'title': title, 'word_count': word_count(' '.join(lines[start + 1:end]))})
    return chapters


def _figure_count(text):
    return sum(1 for line in normalize_text(text).split('\n') if re.match(r'^\s*(figure|fig\.?)\s+\d+', line, re.I))


def structural_checks(text, kind):
    total = word_count(text)
    chapters = _chapter_data(text) if kind == 'book' else []
    figures = _figure_count(text) if kind == 'book' else 0
    checks = []
    if kind == 'article':
        lo, hi = ARTICLE_STRUCTURE['total_words_min'], ARTICLE_STRUCTURE['total_words_max']
        passed = lo <= total <= hi
        if total < lo:
            detail = f'Article is {total:,} words, about {lo-total:,} short of the {lo:,}-{hi:,} range.'
        elif total > hi:
            detail = f'Article is {total:,} words, about {total-hi:,} over the {lo:,}-{hi:,} range.'
        else:
            detail = f'Article is {total:,} words, inside the {lo:,}-{hi:,} range.'
        checks.append({'id': 'total_words', 'label': 'Length', 'passed': passed, 'detail': detail, 'advisory': False})
        return {'total_words': total, 'chapters': chapters, 'figures': figures, 'checks': checks}

    s = BOOK_STRUCTURE
    checks.append({
        'id': 'chapter_count', 'label': 'Twelve chapters', 'passed': len(chapters) == s['chapters_required'],
        'detail': f"Found {len(chapters)} chapters; the Five Zero format is exactly {s['chapters_required']}.", 'advisory': False,
    })
    lo, hi = s['total_words_min'], s['total_words_max']
    if total < lo:
        detail = f'Total is {total:,} words, about {lo-total:,} short of the {lo:,}-{hi:,} range.'
    elif total > hi:
        detail = f'Total is {total:,} words, about {total-hi:,} over the {lo:,}-{hi:,} range.'
    else:
        detail = f'Total is {total:,} words, inside the {lo:,}-{hi:,} range.'
    checks.append({'id': 'total_words', 'label': 'Total length', 'passed': lo <= total <= hi, 'detail': detail, 'advisory': False})
    checks.append({
        'id': 'figures', 'label': 'At least 40 figures', 'passed': figures >= s['figures_required'],
        'detail': f"Found {figures} captioned figures; the format calls for at least {s['figures_required']}.", 'advisory': False,
    })
    balance_passed = True
    problems = []
    if chapters:
        opener = chapters[0]['word_count']
        if not s['opening_chapter_min'] <= opener <= s['opening_chapter_max']:
            balance_passed = False
            problems.append(f"the opening chapter is {opener:,} words (about {s['opening_chapter_min']:,}-{s['opening_chapter_max']:,} expected)")
        for idx, chapter in enumerate(chapters[1:], start=2):
            count = chapter['word_count']
            if count < s['body_chapter_hard_min'] or count > s['body_chapter_hard_max']:
                balance_passed = False
                problems.append(f'chapter {idx} is {count:,} words')
    detail = (
        f"Chapters are balanced (opener light, body chapters near {s['body_chapter_ideal_min']:,}-{s['body_chapter_ideal_max']:,})."
        if balance_passed else
        f"Chapter balance is off: {'; '.join(problems)}. Body chapters should sit near {s['body_chapter_ideal_min']:,}-{s['body_chapter_ideal_max']:,} words."
    )
    checks.append({'id': 'balance', 'label': 'Chapter balance', 'passed': balance_passed, 'detail': detail, 'advisory': False})
    return {'total_words': total, 'chapters': chapters, 'figures': figures, 'checks': checks}


def _build_prompt(manuscript_text, declared_sim, rubric_items, kind, measured):
    lines = [
        f"You are the first-gate reviewer for {'a Five Zero Book' if kind == 'book' else 'a Field Notes Journal article'}. Rule on each criterion below strictly and independently. Judge only the criteria given — not style, taste, or anything else."
    ]
    if declared_sim:
        lines.append(f'\nThe author says this pairs with the simulation: {declared_sim}')
    
    lines.append('\nSTRUCTURAL MEASUREMENTS:')
    lines.append(f"Total Words: {measured['total_words']}")
    if kind == 'book':
        lines.append(f"Chapters Detected: {len(measured['chapters'])}")
        lines.append(f"Figures Detected: {measured['figures']}")
    for check in measured['checks']:
        lines.append(f"- {check['label']}: {'PASS' if check['passed'] else 'FAIL'} - {check['detail']}")
        
    lines.append('\nCRITERIA:')
    for item in rubric_items:
        lines.append(f"\n[{item['id']}] {item['label']}\n{item['criterion']}")
        
    lines.append('''
The application code will compute the final decision from the structural measurements and normalized criterion verdicts. Focus on judging each criterion and explaining the evidence. Include the `decision` field for compatibility only; it will not be trusted as the authoritative final decision.

Decision rules applied by Python after your response:
- If ANY structural measurement failed, the decision MUST be "RETURN_TO_AUTHOR".
- If ANY non-advisory criterion fails, the decision MUST be "RETURN_TO_AUTHOR".
- If ANY criterion needs work, the decision MUST be "REFER_TO_HUMAN_WITH_FLAGS".
- Otherwise, the decision is "PASS_TO_HUMAN".

You must also write an `editor_summary` using exactly these four numbered sections:
1. Structural Findings
2. Rubric Findings
3. Key Gaps / Issues
4. Overall Review Conclusion

Return ONLY a JSON object, no prose, no code fences. Shape:
{ 
  "decision": "PASS_TO_HUMAN|REFER_TO_HUMAN_WITH_FLAGS|RETURN_TO_AUTHOR",
  "editor_summary": "Editor Summary\n\n1. Structural Findings\n   - ...\n\n2. Rubric Findings\n   - ...\n\n3. Key Gaps / Issues\n   - ...\n\n4. Overall Review Conclusion\n   - ...",
  "author_letter": "<full text of the author letter>",
  "items": [ {"id": "<id>", "verdict": "pass|needs_work|fail", "evidence": "<1-2 sentences citing the manuscript>", "gap": "<what the author must change; empty string if pass>"} ] 
}''')
    lines.append('\nMANUSCRIPT:\n')
    lines.append(manuscript_text)
    return '\n'.join(lines)


def _parse_model_json(raw):
    text = str(raw or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.I)
        text = re.sub(r'\s*```$', '', text)
    return json.loads(text)


def _mock_judgment(rubric_items, disclosure, measured):
    items = []
    for item in rubric_items:
        verdict = 'pass'
        evidence = 'Local mock-review mode is enabled; no live model judgment was performed.'
        gap = ''
        if item['id'] == 'ai_disclosure' and len(disclosure.strip()) < 20:
            verdict = 'needs_work'
            gap = 'Provide a more specific AI-use disclosure.'
        items.append({
            'id': item['id'],
            'verdict': verdict,
            'evidence': evidence,
            'gap': gap,
            'advisory': bool(item.get('advisory', False)),
        })

    decision = compute_decision(measured, items)
    summary = _format_editor_summary(measured, items, decision, "Mock editor summary.")
    return '(mock)', items, decision, summary, "Mock author letter."


def _criterion_name(item):
    return str(item.get('id') or 'criterion').replace('_', ' ').title()


def _list_criteria(items):
    if not items:
        return 'None.'
    return ', '.join(_criterion_name(item) for item in items) + '.'


def _clean_inline(value, limit=280):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + '…'
    return text


def _format_editor_summary(measured, judgments, decision, model_summary=''):
    """Return the fixed four-section editor summary shown in the admin portal."""
    checks = measured.get('checks', []) if isinstance(measured, dict) else []
    chapters = measured.get('chapters', []) if isinstance(measured, dict) else []
    figures = measured.get('figures', None) if isinstance(measured, dict) else None
    total_words = measured.get('total_words', 0) if isinstance(measured, dict) else 0

    passed_items = [item for item in judgments if item.get('verdict') == 'pass']
    needs_work_items = [item for item in judgments if item.get('verdict') == 'needs_work']
    failed_items = [item for item in judgments if item.get('verdict') == 'fail']

    lines = [
        'Editor Summary',
        '',
        '1. Structural Findings',
        f'   - Total word count: {total_words:,}',
        f"   - Chapter count: {len(chapters) if chapters else 'Not applicable'}",
        f"   - Figure count: {figures if figures is not None else 'Not applicable'}",
    ]
    if checks:
        for check in checks:
            status = 'PASS' if check.get('passed') else 'FAIL'
            label = check.get('label') or check.get('id') or 'Structural check'
            detail = _clean_inline(check.get('detail'))
            lines.append(f'   - {label}: {status} - {detail}')
    else:
        lines.append('   - Structural checks/results: No structural checks were recorded.')

    lines.extend([
        '',
        '2. Rubric Findings',
        f'   - Criteria that passed: {_list_criteria(passed_items)}',
        f'   - Criteria needing work: {_list_criteria(needs_work_items)}',
        f'   - Criteria that failed: {_list_criteria(failed_items)}',
    ])
    evidence_items = [item for item in judgments if _clean_inline(item.get('evidence'))]
    if evidence_items:
        for item in evidence_items:
            lines.append(f"   - Supporting evidence ({_criterion_name(item)}): {_clean_inline(item.get('evidence'))}")
    else:
        lines.append('   - Supporting evidence: No criterion-level evidence was returned by the model.')

    gap_details = []
    for check in checks:
        if not check.get('passed'):
            gap_details.append(_clean_inline(check.get('detail')))
    for item in judgments:
        if item.get('verdict') in {'needs_work', 'fail'} and _clean_inline(item.get('gap')):
            gap_details.append(f"{_criterion_name(item)}: {_clean_inline(item.get('gap'))}")

    lines.extend([
        '',
        '3. Key Gaps / Issues',
    ])
    if gap_details:
        for detail in gap_details:
            lines.append(f'   - {detail}')
    else:
        lines.append('   - No major gaps were identified by the automated review.')
    lines.append('   - What needs attention or correction: address the failed structural checks and any rubric items marked needs_work or fail.')

    if decision == DECISION_PASS:
        conclusion = 'The manuscript is ready to move to human review based on the recorded structural and rubric findings.'
    elif decision == DECISION_REFER:
        conclusion = 'The manuscript should be reviewed by a human editor because one or more findings require clarification or judgment.'
    else:
        conclusion = 'The manuscript should be returned to the author for revision before it proceeds.'
    model_note = _clean_inline(model_summary, 500)
    if model_note and not model_note.lower().startswith('editor summary'):
        conclusion = f'{conclusion} Model note: {model_note}'

    lines.extend([
        '',
        '4. Overall Review Conclusion',
        f'   - Decision: {decision}',
        f'   - {conclusion}',
    ])
    return '\n'.join(lines)


def _fallback_editor_summary(measured, judgments, decision=None):
    return _format_editor_summary(measured, judgments, decision or DECISION_REFER)


def _fallback_author_letter(decision, measured, judgments):
    if decision == DECISION_PASS:
        opening = "The manuscript is ready for human review."
    elif decision == DECISION_REFER:
        opening = "The manuscript needs human review or clarification before a final decision."
    else:
        opening = "The manuscript should be revised and resubmitted."
    return f"{opening} The automated review measured {measured['total_words']:,} words and recorded the current structural and rubric findings in the review record."


def _repair_missing_outputs(parsed, decision, measured, judgments, kind):
    """Repair missing small output fields without resending the manuscript.

    A small local-model repair request is based only on the already computed
    measurements/judgments. If local inference fails, deterministic text is
    returned so the admin portal never displays an empty AI result.
    """
    summary = parsed.get('editor_summary') if isinstance(parsed, dict) else None
    letter = parsed.get('author_letter') if isinstance(parsed, dict) else None
    summary_ok = isinstance(summary, str) and bool(summary.strip())
    letter_ok = isinstance(letter, str) and bool(letter.strip())
    if summary_ok and letter_ok:
        return summary.strip(), letter.strip()

    fallback_summary = _fallback_editor_summary(measured, judgments, decision)
    fallback_letter = _fallback_author_letter(decision, measured, judgments)
    if os.getenv('MOCK_AI_REVIEW', 'false').lower() in {'1', 'true', 'yes', 'on'}:
        return (
            summary.strip() if summary_ok else fallback_summary,
            letter.strip() if letter_ok else fallback_letter,
        )

    missing = []
    if not summary_ok:
        missing.append('editor_summary')
    if not letter_ok:
        missing.append('author_letter')
    compact = {
        'decision': decision,
        'kind': kind,
        'total_words': measured.get('total_words', 0),
        'structural_checks': measured.get('checks', []),
        'judgments': judgments,
        'missing_fields': missing,
    }
    prompt = (
        "Repair the missing fields in this manuscript review. Return JSON only. "
        "Do not invent facts and use only the supplied review data. "
        "editor_summary must use exactly these sections: 1. Structural Findings; "
        "2. Rubric Findings; 3. Key Gaps / Issues; 4. Overall Review Conclusion. "
        "author_letter must be 2-4 polite sentences appropriate to the decision. "
        f"Review data:\n{json.dumps(compact, ensure_ascii=False)}"
    )
    try:
        _, output = ai_chat_json(
            prompt,
            max_tokens=300,
            timeout=90,
            operation='review_output_repair',
        )
        repaired = _parse_model_json(output)
        repaired_summary = repaired.get('editor_summary') if isinstance(repaired, dict) else None
        repaired_letter = repaired.get('author_letter') if isinstance(repaired, dict) else None
        if not isinstance(repaired_summary, str) or not repaired_summary.strip():
            repaired_summary = fallback_summary
        if not isinstance(repaired_letter, str) or not repaired_letter.strip():
            repaired_letter = fallback_letter
        return repaired_summary.strip(), repaired_letter.strip()
    except (RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        return (
            summary.strip() if summary_ok else fallback_summary,
            letter.strip() if letter_ok else fallback_letter,
        )



def _judge_chunked(text, declared_sim, rubric_items, kind, disclosure, measured, num_ctx, max_tokens):
    available_tokens = num_ctx - max_tokens - 1000
    max_chars = max(1000, available_tokens * 4)
    chunks = []
    current = 0
    while current < len(text):
        end = min(current + max_chars, len(text))
        if end < len(text):
            last_break = text.rfind('\n\n', current, end)
            if last_break > current + max_chars // 2:
                end = last_break + 2
        chunks.append(text[current:end])
        current = end
        
    all_judgments = []
    models_used = set()
    for chunk in chunks:
        chunk_prompt = _build_prompt(chunk, declared_sim, rubric_items, kind, measured)
        model, output = ai_chat_json(
            chunk_prompt,
            max_tokens=max_tokens,
            operation='manuscript_review_chunk',
        )
        models_used.add(model)
        parsed = _parse_model_json(output)
        if isinstance(parsed, dict):
            by_id = {item.get('id'): item for item in parsed.get('items', []) if isinstance(item, dict)}
            allowed = {'pass', 'needs_work', 'fail'}
            items = []
            for rubric in rubric_items:
                got = by_id.get(rubric['id'], {})
                verdict = got.get('verdict') if got.get('verdict') in allowed else 'needs_work'
                if rubric.get('advisory') and verdict == 'fail':
                    verdict = 'needs_work'
                items.append({
                    'id': rubric['id'],
                    'verdict': verdict,
                    'evidence': str(got.get('evidence', '')),
                    'gap': str(got.get('gap', '')),
                    'advisory': bool(rubric.get('advisory', False)),
                })
            all_judgments.append(items)
            
    verdict_rank = {'pass': 0, 'needs_work': 1, 'fail': 2}
    final_items = []
    for rubric in rubric_items:
        worst_verdict = 'pass'
        best_evidence = ''
        best_gap = ''
        for items in all_judgments:
            for item in items:
                if item['id'] == rubric['id']:
                    if verdict_rank[item['verdict']] > verdict_rank[worst_verdict]:
                        worst_verdict = item['verdict']
                        best_evidence = item['evidence']
                        best_gap = item['gap']
                    elif verdict_rank[item['verdict']] == verdict_rank[worst_verdict] and not best_evidence:
                        best_evidence = item['evidence']
                        best_gap = item['gap']
        final_items.append({
            'id': rubric['id'],
            'verdict': worst_verdict,
            'evidence': best_evidence,
            'gap': best_gap,
            'advisory': bool(rubric.get('advisory', False)),
        })
        
    decision = compute_decision(measured, final_items)
    editor_summary = _format_editor_summary(measured, final_items, decision, "Chunked review combined.")
    author_letter = _fallback_author_letter(decision, measured, final_items)
    metadata = {"mode": "chunked", "chunk_count": len(chunks)}
    return ",".join(models_used) or "unknown", final_items, decision, editor_summary, author_letter, metadata

def judge_with_local_model(text, declared_sim, rubric_items, kind, disclosure, measured):
    if os.getenv('MOCK_AI_REVIEW', 'false').lower() in {'1', 'true', 'yes', 'on'}:
        res = _mock_judgment(rubric_items, disclosure, measured)
        return res[0], res[1], res[2], res[3], res[4], {"mode": "mock"}

    prompt = _build_prompt(text, declared_sim, rubric_items, kind, measured)
    max_tokens = int(os.getenv('OLLAMA_NUM_PREDICT', '4000'))
    num_ctx = int(os.getenv('OLLAMA_NUM_CTX', str(DEFAULT_OLLAMA_NUM_CTX)))
    
    force_provider = None
    metadata = {"mode": "standard"}
    
    try:
        assert_prompt_fits_context(prompt, num_ctx=num_ctx, num_predict=max_tokens)
    except RuntimeError as exc:
        if "too large" in str(exc).lower():
            if os.getenv('ENABLE_CLOUD_FALLBACK', 'false').lower() in {'1', 'true', 'yes', 'on'}:
                force_provider = 'anthropic'
                metadata = {"mode": "cloud-full", "provider": "anthropic"}
            else:
                return _judge_chunked(text, declared_sim, rubric_items, kind, disclosure, measured, num_ctx, max_tokens)
        else:
            raise

    model, output = ai_chat_json(
        prompt,
        max_tokens=max_tokens,
        force_provider=force_provider,
        operation='manuscript_review',
    )
    parsed = _parse_model_json(output)
    if not isinstance(parsed, dict):
        raise RuntimeError("Local AI returned JSON, but the review payload was not an object.")

    by_id = {item.get('id'): item for item in parsed.get('items', []) if isinstance(item, dict)}
    allowed = {'pass', 'needs_work', 'fail'}
    items = []
    for rubric in rubric_items:
        got = by_id.get(rubric['id'], {})
        verdict = got.get('verdict') if got.get('verdict') in allowed else 'needs_work'
        if rubric.get('advisory') and verdict == 'fail':
            verdict = 'needs_work'
        items.append({
            'id': rubric['id'],
            'verdict': verdict,
            'evidence': got.get('evidence', '') if isinstance(got.get('evidence', ''), str) else '',
            'gap': got.get('gap', '') if isinstance(got.get('gap', ''), str) else '',
            'advisory': bool(rubric.get('advisory', False)),
        })

    decision = compute_decision(measured, items)

    editor_summary, author_letter = _repair_missing_outputs(
        parsed, decision, measured, items, kind
    )
    editor_summary = _format_editor_summary(measured, items, decision, editor_summary)
    return model, items, decision, editor_summary, author_letter, metadata



from .field_agent import run_field_agent

def run_review(content, filename, kind, declared_sim='', disclosure=''):
    raw_text = extract_text(content, filename)
    measured = structural_checks(raw_text, kind)
    rubric_items = BOOK_JUDGMENT if kind == 'book' else ARTICLE_JUDGMENT
    
    model, judgments, decision, editor_summary, author_letter, metadata = judge_with_local_model(
        f"{raw_text}\n\nAI-Use Disclosure (submitted with the manuscript):\n{disclosure}",
        declared_sim,
        rubric_items,
        kind,
        disclosure,
        measured
    )
    
    # --- Field Agent Logic ---
    if kind == 'book':
        # Append the field briefing to the editor summary
        field_briefing = run_field_agent(raw_text)
        editor_summary += field_briefing
    
    record = {
        'version': 'django-sqlite-v1',
        'decision': decision,
        'kind': kind,
        'model': model,
        'measured': measured,
        'structural': measured['checks'],
        'judgment': judgments,
        'declared_sim': declared_sim,
    }
    record.update(metadata)
    return {
        'decision': decision,
        'model': model,
        'total_words': measured['total_words'],
        'record': record,
        'editor_summary': editor_summary,
        'author_letter': author_letter,
    }
