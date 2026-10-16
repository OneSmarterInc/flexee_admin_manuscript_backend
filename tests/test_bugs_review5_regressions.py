"""Review 5, R5-11 and R5-13: two Review 4 changes that went too far."""
import os

import pytest

from review.services import index_rules as ir
from review.services import venue_discovery as vd


# R5-11 --------------------------------------------------------------------------------------------

def _item(deadlines, evidence_fields):
    return {'deadlines': deadlines,
            'source_evidence': [{'field': name, 'quote': 'quoted from the page', 'url': 'https://x.example/'}
                                for name in evidence_fields]}


def test_r5_11_a_deadline_quoted_from_a_call_for_papers_is_kept():
    """Review 4's Y9 published a deadline only when the model named a 'deadline'. A real call for papers
    names the field 'call_for_papers', so every verified call deadline was wiped."""
    item = _item({'Special issue': '2027-06-01'}, ['call_for_papers'])
    quoted, _ = ir.quoted_fields(item)
    assert 'deadlines' in quoted
    assert ir.keep_quoted_rules(item)['deadlines'] == {'Special issue': '2027-06-01'}


def test_r5_11_a_date_inside_another_field_name_is_not_a_deadline_quote():
    """'date' as a bare substring also sat inside 'last_updated', so evidence about when the page changed
    counted as a quote for a deadline the model had invented."""
    item = _item({'Manuscript deadline': '2027-06-01'}, ['last_updated'])
    quoted, _ = ir.quoted_fields(item)
    assert 'deadlines' not in quoted
    assert ir.keep_quoted_rules(item)['deadlines'] == {}


def test_r5_11_an_invented_deadline_is_still_dropped():
    """The Y9 protection itself must survive: no quote, no deadline."""
    item = _item({'Manuscript deadline': '2027-06-01'}, ['aims_scope'])
    assert ir.keep_quoted_rules(item)['deadlines'] == {}


def test_r5_11_a_type_quote_does_not_come_from_prototype():
    item = {'article_types': ['Research article'],
            'source_evidence': [{'field': 'prototype_notes', 'quote': 'q', 'url': 'https://x.example/'}]}
    quoted, _ = ir.quoted_fields(item)
    assert 'article_types' not in quoted


# R5-13 --------------------------------------------------------------------------------------------

# config.ai_provider == 'ollama' takes extract_with_local_ollama, a different path, so it is not listed.
@pytest.mark.parametrize('config_provider, env_provider, expected', [
    ('shared_qwen', '', 2),
    ('', 'shared_qwen', 2),        # the default production setup: the config field is not set
    ('', 'ollama', 2),
    ('', '', 2),                   # nothing configured at all reads as local, not as paid
    ('anthropic', '', 1),          # a paid provider still gets one call per escalation (Y10)
    ('', 'anthropic', 1),
])
def test_r5_13_the_free_retry_follows_the_provider_actually_in_use(monkeypatch, config_provider,
                                                                   env_provider, expected):
    """Review 4's Y10 read config.ai_provider, which is empty in the default production setup. Empty was
    treated as paid, so the free local retry was dropped everywhere — the opposite of the intent."""
    monkeypatch.setenv('AI_PROVIDER', env_provider)
    calls = {'n': 0}

    def never_parses(prompt, **kw):
        calls['n'] += 1
        raise vd.DiscoveryExtractionError('The AI did not return JSON.')

    monkeypatch.setattr(vd, 'build_extraction_prompt', lambda pages, config: 'prompt')
    monkeypatch.setattr('review.services.ai_provider.ai_chat_json', never_parses)
    config = vd.DiscoveryConfig.from_env()
    config = type(config)(**{**config.__dict__, 'ai_provider': config_provider})
    pages = [vd.FetchedPage(url='https://one.example/', title='One', text='Welcome.')]

    with pytest.raises(vd.DiscoveryExtractionError):
        vd.extract_with_ai(pages, config)
    assert calls['n'] == expected, f'{config_provider!r}/{env_provider!r} made {calls["n"]} call(s)'
