"""Claude as the venue-discovery agent.

Claude decides what to search for, which results look official, and which pages
to open, using Anthropic's server-side web_search and web_fetch tools. Anthropic
performs the browsing, so this server never fetches arbitrary URLs in this mode.

Claude's answer is NOT trusted on its own: every venue is re-checked by
venue_discovery.validate_extraction against the page text that web_fetch actually
returned (quotes must appear on the page, accepting/closed needs official
evidence, only objective limits become automatic rules).
"""
import json
import os

from .venue_discovery import (
    DiscoveryConfigError, DiscoveryExtractionError, FetchedPage, THIRD_PARTY_DOMAINS,
    canonical_host, canonical_url, parse_ai_json, registrable_domain,
)


CATEGORY_BRIEFS = {
    'journal': 'academic and practitioner JOURNALS that currently accept manuscript submissions '
               '(research articles, review articles, case studies or practitioner articles)',
    'publisher': 'academic or professional BOOK PUBLISHERS that currently accept book proposals '
                 '(monographs, textbooks or professional books)',
    'conference': 'academic CONFERENCES with an open call for papers',
}


def agent_settings():
    def num(name, default, low, high):
        try:
            value = int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            value = default
        return max(low, min(value, high))

    return {
        'api_key': os.getenv('ANTHROPIC_API_KEY', '').strip(),
        'model': (os.getenv('VENUE_DISCOVERY_MODEL', '').strip()
                  or os.getenv('ANTHROPIC_MODEL', '').strip() or 'claude-sonnet-5-5'),
        'categories': [c for c in os.getenv('VENUE_DISCOVERY_CATEGORIES', 'journal,publisher,conference').split(',')
                       if c.strip() in CATEGORY_BRIEFS],
        'focus': os.getenv(
            'VENUE_DISCOVERY_FOCUS',
            'business and management, information systems, supply chain and operations, AI in organizations, '
            'and education or simulation-based learning',
        ).strip(),
        'venues_per_category': num('VENUE_DISCOVERY_VENUES_PER_CATEGORY', 6, 1, 20),
        'max_searches_per_request': num('VENUE_DISCOVERY_MAX_SEARCHES_PER_REQUEST', 8, 1, 30),
        'max_fetches_per_request': num('VENUE_DISCOVERY_MAX_FETCHES_PER_REQUEST', 12, 1, 40),
        'fetch_max_content_tokens': num('VENUE_DISCOVERY_FETCH_MAX_CONTENT_TOKENS', 6000, 1000, 50000),
        'max_output_tokens': num('VENUE_DISCOVERY_MAX_OUTPUT_TOKENS', 8000, 1000, 32000),
        'max_continuations': num('VENUE_DISCOVERY_MAX_CONTINUATIONS', 4, 0, 10),
        'recheck_batch_size': num('VENUE_DISCOVERY_RECHECK_BATCH_SIZE', 5, 1, 10),
        'timeout': float(num('VENUE_DISCOVERY_AGENT_TIMEOUT_SECONDS', 600, 30, 1800)),
    }


def tools_for(settings):
    blocked = sorted(THIRD_PARTY_DOMAINS)
    return [
        {'type': 'web_search_20250305', 'name': 'web_search',
         'max_uses': settings['max_searches_per_request'], 'blocked_domains': blocked},
        {'type': 'web_fetch_20250910', 'name': 'web_fetch',
         'max_uses': settings['max_fetches_per_request'], 'blocked_domains': blocked,
         'max_content_tokens': settings['fetch_max_content_tokens']},
    ]


RESULT_SHAPE = {
    'venues': [{
        'name': 'string', 'organization_name': 'string', 'venue_type': 'journal | conference | publisher',
        'acceptance_status': 'accepting | unclear | closed', 'website_url': 'url', 'submission_url': 'url or ""',
        'submission_types': ['e.g. Research article, Review article, Case study, Book proposal'],
        'description': 'one or two sentences', 'aims_scope': 'string', 'article_types': ['string'],
        'accepted_methods': ['string'], 'quality_threshold': 'string', 'reviewer_criteria': ['string'],
        'policies': {}, 'disclosures': ['string'], 'reporting_standards': ['string'], 'desk_rejection_rules': ['string'],
        'structured_desk_rejection_rules': [{'field': 'word_count | reference_count | required_sections',
                                             'operator': '> | >= | < | <= | missing_any', 'value': 'number or list',
                                             'message': 'string'}],
        'required_submission_items': [{'label': 'string', 'type': 'text | textarea | url | checkbox | file',
                                       'required': 'true only if the page says so', 'help_text': 'string'}],
        'retention_days': None, 'deadlines': {}, 'submission_capacity': {}, 'current_demand': {},
        'config_notes': 'string', 'conflicts': ['string'],
        'source_evidence': [{'field': 'string', 'claim': 'string', 'url': 'a URL you FETCHED',
                             'source_title': 'string', 'evidence_text': 'short exact quote from that fetched page'}],
    }],
}

RULES = (
    'Rules:\n'
    '- Use web_search to find candidates, then use web_fetch to open each venue\'s OFFICIAL website and its '
    'author guidelines / submission / call-for-papers / book-proposal page. Only report venues whose official '
    'pages you actually fetched.\n'
    '- Never rely on aggregators, blogs, social media, Wikipedia or third-party lists as evidence.\n'
    '- Report only what the fetched official pages say. If something is not stated use "" for text, [] for '
    'lists, {} for objects and null for numbers. Never guess.\n'
    '- acceptance_status is "accepting" only if a fetched official page shows an open way to submit now; '
    '"closed" if it says submissions are closed or suspended; otherwise "unclear".\n'
    '- structured_desk_rejection_rules: only explicit, objective limits (maximum word count, reference count, '
    'required named sections). Never turn subjective wording into a rule.\n'
    '- For book publishers, put proposal details in policies, e.g. '
    '{"book_submission_stage": "proposal", "accepted_book_types": [...]}.\n'
    '- source_evidence: cover acceptance status, accepted types, scope, required items, word limits, deadlines and '
    'desk-rejection conditions. evidence_text must be a short EXACT quote copied from the fetched page at url.\n'
    '- Finish with ONLY one JSON object of this shape (no prose after it):\n'
)


def discovery_prompt(category, settings, known_names):
    known = ', '.join(known_names[:60]) or 'none'
    return (
        f'You are a venue-discovery agent for Flexee, a scholarly publishing platform. Find up to '
        f'{settings["venues_per_category"]} {CATEGORY_BRIEFS[category]} in these subject areas: {settings["focus"]}.\n'
        f'Skip venues already known to us: {known}.\n'
        + RULES + json.dumps(RESULT_SHAPE)
    )


def recheck_prompt(records):
    lines = []
    for record in records:
        urls = [u for u in [record.submission_url, record.website_url] if u]
        lines.append(f'- {record.name} ({record.organization_name or "organization not stated"}): ' + ' '.join(urls))
    return (
        'You are a venue-discovery agent for Flexee. Re-check these known venues. For each one, use web_fetch on the '
        'official URLs listed (and web_search only if a page has moved) and report its CURRENT submission status '
        'and rules. Include every venue below in your answer, using the same name.\n'
        + '\n'.join(lines) + '\n' + RULES + json.dumps(RESULT_SHAPE)
    )


# ---------------------------------------------------------------------------
# Calling Claude (server tools, pause_turn continuation, usage tracking)
# ---------------------------------------------------------------------------

def _create_message(settings, messages):
    """One Messages API call. Returned as a plain dict so it can be sent back unchanged."""
    import anthropic
    client = anthropic.Anthropic(api_key=settings['api_key'], timeout=settings['timeout'])
    message = client.messages.create(
        model=settings['model'],
        max_tokens=settings['max_output_tokens'],
        messages=messages,
        tools=tools_for(settings),
    )
    return message.model_dump(exclude_none=True)


def _estimated_input_tokens(settings, prompt):
    # Conservative upper bound used to reserve AI budget before the call.
    search_tokens = settings['max_searches_per_request'] * 4000
    fetch_tokens = settings['max_fetches_per_request'] * settings['fetch_max_content_tokens']
    return (len(prompt) // 3) + search_tokens + fetch_tokens


def run_agent(prompt, settings, *, create_message=None, operation='venue_discovery_agent'):
    """Run Claude with web search/fetch until it finishes. Returns (final_text, pages, stats)."""
    if not settings['api_key']:
        raise DiscoveryConfigError('ANTHROPIC_API_KEY is not set, so the Claude discovery agent cannot run.')
    create_message = create_message or _create_message

    from ..ai_usage import complete_ai_call, fail_ai_call, reserve_ai_call
    estimated = _estimated_input_tokens(settings, prompt)
    reservation = reserve_ai_call(provider='anthropic', model=settings['model'], operation=operation,
                                  estimated_input_tokens=estimated,
                                  max_output_tokens=settings['max_output_tokens'] * (settings['max_continuations'] + 1))

    messages = [{'role': 'user', 'content': prompt}]
    stats = {'searches': 0, 'fetches': 0, 'input_tokens': 0, 'output_tokens': 0, 'tool_errors': []}
    pages, final_text = {}, ''
    try:
        for _turn in range(settings['max_continuations'] + 1):
            response = create_message(settings, messages)
            usage = response.get('usage') or {}
            stats['input_tokens'] += int(usage.get('input_tokens') or 0) + int(usage.get('cache_read_input_tokens') or 0)
            stats['output_tokens'] += int(usage.get('output_tokens') or 0)
            server_use = usage.get('server_tool_use') or {}
            stats['searches'] += int(server_use.get('web_search_requests') or 0)
            stats['fetches'] += int(server_use.get('web_fetch_requests') or 0)

            content = response.get('content') or []
            _collect_pages(content, pages, stats)
            text_after_tools = []
            for block in content:
                if block.get('type') in {'server_tool_use', 'web_search_tool_result', 'web_fetch_tool_result'}:
                    text_after_tools = []
                elif block.get('type') == 'text':
                    text_after_tools.append(block.get('text', ''))
            if text_after_tools:
                final_text = ''.join(text_after_tools)

            if response.get('stop_reason') == 'pause_turn':
                messages = messages + [{'role': 'assistant', 'content': content}]
                continue
            break
    except Exception as exc:
        fail_ai_call(reservation, exc, provider='anthropic', model=settings['model'], operation=operation,
                     estimated_input_tokens=estimated)
        raise
    complete_ai_call(reservation, provider='anthropic', model=settings['model'], operation=operation,
                     input_tokens=stats['input_tokens'], output_tokens=stats['output_tokens'])
    return final_text, list(pages.values()), stats


def _collect_pages(content, pages, stats):
    """Turn web_fetch results into FetchedPage objects (the text we verify claims against)."""
    for block in content:
        if block.get('type') == 'web_search_tool_result' and isinstance(block.get('content'), dict):
            stats['tool_errors'].append(f"search: {block['content'].get('error_code', 'error')}")
        if block.get('type') != 'web_fetch_tool_result':
            continue
        result = block.get('content') or {}
        if result.get('type') != 'web_fetch_result':
            stats['tool_errors'].append(f"fetch: {result.get('error_code', 'error')}")
            continue
        document = result.get('content') or {}
        source = document.get('source') or {}
        if source.get('type') != 'text':
            continue  # PDFs come back as base64; their text can't be verified here
        url = str(result.get('url') or '')
        if not canonical_url(url):
            continue
        pages[canonical_url(url)] = FetchedPage(url=url, title=str(document.get('title') or '')[:300],
                                                text=str(source.get('data') or '')[:200_000])


def parse_venues(final_text):
    data = parse_ai_json(final_text)
    venues = data.get('venues') if isinstance(data, dict) else None
    if not isinstance(venues, list):
        raise DiscoveryExtractionError('The agent did not return a "venues" list.')
    return [v for v in venues if isinstance(v, dict)]


def pages_for_venue(raw, pages):
    """Official pages for one venue: those on the venue's own website/submission domains."""
    domains = {registrable_domain(canonical_host(u)) for u in (raw.get('website_url'), raw.get('submission_url')) if u}
    evidence_urls = {canonical_url(str(e.get('url', ''))) for e in raw.get('source_evidence') or [] if isinstance(e, dict)}
    picked = [p for p in pages if registrable_domain(canonical_host(p.url)) in domains or canonical_url(p.url) in evidence_urls]
    return picked
