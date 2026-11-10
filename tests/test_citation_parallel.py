import threading
import time
from review.services import author_agents as a

def test_citations_overlap_without_changing_order(monkeypatch):
    monkeypatch.setattr(a, '_extract_citations', lambda text: (2, ['first', 'second']))
    barrier = threading.Barrier(2)
    def verify(citation):
        barrier.wait(timeout=3)
        return {'status': 'verified', 'matched_title': citation}
    monkeypatch.setattr(a, '_verify_citation_crossref', verify)
    result = a._citation_checks('text', 'hash')
    assert result['checked'] == 2
    assert [r['citation'] for r in result['results']] == ['first', 'second']

def test_expired_deadline_starts_no_requests(monkeypatch):
    monkeypatch.setattr(a, '_extract_citations', lambda text: (2, ['first', 'second']))
    def unexpected(citation):
        raise AssertionError('Lookup started after deadline')
    monkeypatch.setattr(a, '_verify_citation_crossref', unexpected)
    assert a._citation_checks('text', 'hash', deadline=time.monotonic()-1)['checked'] == 0
