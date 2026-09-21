import json
import os
import re
from io import BytesIO
from docx import Document
from pypdf import PdfReader

from .local_llm import ollama_chat_json

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
Based on the structural measurements and your criteria judgments, you must determine the final decision.
- If ANY structural measurement failed, the decision MUST be "RETURN_TO_AUTHOR".
- If ANY non-advisory criterion fails, the decision MUST be "RETURN_TO_AUTHOR".
- If ANY criterion needs work, or an advisory criterion fails, the decision MUST be "REFER_TO_HUMAN_WITH_FLAGS".
- Otherwise, the decision is "PASS_TO_HUMAN".

You must also write an `editor_summary` (a professional summary of the structural checks and your findings. Format it clearly using paragraphs or bullet points so it is highly readable, and add an empty line/space after every 2 points or concepts to keep it minimal and visually spaced out) and an `author_letter` (a polite letter to the author outlining the results, using the decision logic: "ready for human review" if pass, "needs human review or clarification" if refer, "revise and resubmit" if return).

Return ONLY a JSON object, no prose, no code fences. Shape:
{ 
  "decision": "PASS_TO_HUMAN|REFER_TO_HUMAN_WITH_FLAGS|RETURN_TO_AUTHOR",
  "editor_summary": "<full text of the editor summary>",
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
    has_needs_work = False
    for item in rubric_items:
        verdict = 'pass'
        evidence = 'Local mock-review mode is enabled; no live model judgment was performed.'
        gap = ''
        if item['id'] == 'ai_disclosure' and len(disclosure.strip()) < 20:
            verdict = 'needs_work'
            gap = 'Provide a more specific AI-use disclosure.'
            has_needs_work = True
        items.append({'id': item['id'], 'verdict': verdict, 'evidence': evidence, 'gap': gap})
        
    decision = DECISION_PASS
    if any(not check['passed'] for check in measured['checks']):
        decision = DECISION_FAIL
    elif has_needs_work:
        decision = DECISION_REFER

    return '(mock)', items, decision, "Mock editor summary.", "Mock author letter."


def judge_with_local_model(text, declared_sim, rubric_items, kind, disclosure, measured):
    if os.getenv('MOCK_AI_REVIEW', 'false').lower() in {'1', 'true', 'yes', 'on'}:
        return _mock_judgment(rubric_items, disclosure, measured)

    model, output = ollama_chat_json(
        _build_prompt(text, declared_sim, rubric_items, kind, measured),
        max_tokens=int(os.getenv('OLLAMA_NUM_PREDICT', '4000')),
    )
    parsed = _parse_model_json(output)
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
        })

    decision = parsed.get('decision', DECISION_FAIL)
    if decision not in {DECISION_PASS, DECISION_REFER, DECISION_FAIL}:
        decision = DECISION_FAIL

    editor_summary = parsed.get('editor_summary', 'No summary provided by local AI.')
    author_letter = parsed.get('author_letter', 'No letter provided by local AI.')
    if not isinstance(editor_summary, str):
        editor_summary = str(editor_summary)
    if not isinstance(author_letter, str):
        author_letter = str(author_letter)

    return model, items, decision, editor_summary, author_letter



from .field_agent import run_field_agent

def run_review(content, filename, kind, declared_sim='', disclosure=''):
    raw_text = extract_text(content, filename)
    measured = structural_checks(raw_text, kind)
    rubric_items = BOOK_JUDGMENT if kind == 'book' else ARTICLE_JUDGMENT
    
    model, judgments, decision, editor_summary, author_letter = judge_with_local_model(
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
    return {
        'decision': decision,
        'model': model,
        'total_words': measured['total_words'],
        'record': record,
        'editor_summary': editor_summary,
        'author_letter': author_letter,
    }
