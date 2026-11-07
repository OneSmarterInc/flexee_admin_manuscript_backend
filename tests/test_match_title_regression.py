from types import SimpleNamespace
from review.match_score import compute_match_score

def score(title):
    manuscript = SimpleNamespace(title=title, abstract='Artificial intelligence retrieval evaluation',
        keywords=['retrieval'], parsed_profile={}, manuscript_type='research_article')
    config = SimpleNamespace(aims_scope='Artificial intelligence retrieval evaluation',
        reviewer_criteria=[], quality_threshold='', article_types=['Research article'],
        structured_desk_rejection_rules=[], accepted_methods=[])
    return compute_match_score(manuscript, config)['score']

def test_accidental_label_does_not_change_score():
    title = 'Artificial intelligence: retrieval evaluation'
    assert score('Manuscript title: ' + title) == score(title)

def test_subtitle_content_is_considered():
    assert score('Study: artificial intelligence retrieval evaluation') > score('Study')
