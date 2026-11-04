from tests.test_oscm_checks import manuscript
from review.services.profile_document_checks import check_profile


def profile(checks, **extra):
    return dict(id='test',source_url='https://example.org/authors',checks=checks,**extra)


def test_each_journal_uses_its_own_limits():
    m=manuscript()
    a=check_profile(m,profile({'abstract-length':[100,150]}))
    b=check_profile(m,profile({'abstract-length':[100,300]}))
    assert len(a)==len(b)==1
    assert a[0]['status']=='failed'
    assert b[0]['status']=='passed'
    assert '150' in a[0]['action']
    assert not any('oscm' in x['id'] for x in a+b)


def test_no_unconfigured_oscm_requirements_leak():
    assert check_profile(manuscript(),profile({})) == []
    rows=check_profile(manuscript(),profile({'keywords':[None,8]}))
    assert len(rows)==1 and rows[0]['status']=='passed'


def test_journal_specific_formatting():
    # The sample uses 10pt single spacing in its body, not OSCM's 12pt/1.5.
    rows=check_profile(manuscript(),profile({'font-size':10,'spacing':1}))
    assert len(rows)==2 and all(x['status']=='passed' for x in rows)


def test_article_applicability_does_not_impose_article_limits_on_books():
    m=manuscript();m.manuscript_type='book_manuscript'
    assert check_profile(m,profile({'abstract-length':[100,150]},manuscript_types=['research_article']))==[]


def test_corrected_document_rechecks_same_rules():
    from review.services.journal_checks import compare_checks
    p=profile({'abstract-length':[100,150]})
    rows=compare_checks(check_profile(manuscript(True),p),check_profile(manuscript(),p))
    assert rows[0]['change']=='resolved'

def test_article_label_difference_is_not_a_confirmed_failure():
    from types import SimpleNamespace
    from review.services.journal_checks import evaluate_rules
    m=manuscript();m.manuscript_type='research_article';m.TYPE_CHOICES=[('research_article','Research article')]
    config=SimpleNamespace(article_types=['Empirical research'],structured_desk_rejection_rules=[],required_submission_items=[],policies={})
    rows=evaluate_rules(m,config,'Readable text')
    assert next(x for x in rows if x['id']=='article_types')['status']=='not_checked'
