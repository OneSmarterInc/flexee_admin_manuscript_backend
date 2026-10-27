"""Guidelines addresses on publisher hosts that answer us (27 October).

journals.sagepub.com returns 403 to our client and www.sagepub.com/docs/... serves the same author
guidelines with a 200, and the same split holds for INFORMS. Together that is 93 of the 849 ABDC
A* and A journals. Neither address is guessable from a journal title, so the journal's internal
code is read out of the homepage URL we already hold rather than invented.
"""
import pytest

from review.services import index_rules as ir
from review.services.publisher_routes import candidates_for


class Rec:
    def __init__(self, homepage='', doaj=None):
        self.homepage_url = homepage
        self.doaj = doaj or {}


# --- SAGE ------------------------------------------------------------------------------------

def test_a_sage_home_url_yields_the_guidelines_address():
    urls = candidates_for(Rec('https://journals.sagepub.com/home/hum'))
    assert urls[0] == 'https://www.sagepub.com/docs/default-source/msg/HUM'


def test_both_cases_are_offered_because_both_are_served():
    urls = candidates_for(Rec('https://journals.sagepub.com/home/oss'))
    assert urls == ['https://www.sagepub.com/docs/default-source/msg/OSS',
                    'https://www.sagepub.com/docs/default-source/msg/oss']


def test_a_table_of_contents_url_carries_the_code_too():
    urls = candidates_for(Rec('https://journals.sagepub.com/toc/etp/48/2'))
    assert urls[0].endswith('/msg/ETP')


def test_a_loi_url_carries_the_code():
    urls = candidates_for(Rec('https://journals.sagepub.com/loi/jsr'))
    assert urls[0].endswith('/msg/JSR')


def test_www_is_ignored_when_matching_the_host():
    assert candidates_for(Rec('https://www.journals.sagepub.com/home/cmr'))


def test_a_sage_url_with_no_journal_code_yields_nothing():
    """Never guess. A shop or marketing path carries no code, so there is no candidate."""
    assert candidates_for(Rec('https://www.sagepub.com/shop/subscribe-to-a-journal/human-relations-200870')) == []
    assert candidates_for(Rec('https://journals.sagepub.com/')) == []


# --- INFORMS ---------------------------------------------------------------------------------

def test_an_informs_journal_url_yields_its_submission_guidelines():
    urls = candidates_for(Rec('https://pubsonline.informs.org/journal/opre'))
    assert urls == ['https://pubsonline.informs.org/page/opre/submission-guidelines']


def test_an_informs_toc_url_carries_the_code():
    urls = candidates_for(Rec('https://pubsonline.informs.org/toc/trsc/59/4'))
    assert urls[0].endswith('/page/trsc/submission-guidelines')


def test_an_informs_url_with_no_code_yields_nothing():
    assert candidates_for(Rec('https://pubsonline.informs.org/')) == []


# --- everything else -------------------------------------------------------------------------

def test_another_publisher_gets_no_extra_candidate():
    """Elsevier, Wiley, Taylor and Francis and Oxford refuse us at every address we have tried,
    so there is nothing honest to add for them."""
    for homepage in ('https://www.sciencedirect.com/journal/research-policy',
                     'https://onlinelibrary.wiley.com/journal/14676486',
                     'https://academic.oup.com/qje',
                     'https://link.springer.com/journal/41267'):
        assert candidates_for(Rec(homepage)) == [], homepage


def test_no_homepage_means_no_candidates():
    assert candidates_for(Rec('')) == []
    assert candidates_for(Rec(None)) == []


# --- how the reader uses them ----------------------------------------------------------------

def test_the_publisher_route_is_tried_before_the_homepage():
    record = Rec('https://journals.sagepub.com/home/hum')
    urls = ir.entry_urls(record)
    assert urls == ['https://www.sagepub.com/docs/default-source/msg/HUM',
                    'https://www.sagepub.com/docs/default-source/msg/hum',
                    'https://journals.sagepub.com/home/hum']


def test_the_doaj_guidelines_url_still_comes_first():
    """DOAJ's address is the journal's own statement of where its guidelines are."""
    record = Rec('https://journals.sagepub.com/home/hum',
                 doaj={'guidelines_url': 'https://society.example/authors'})
    assert ir.entry_urls(record)[0] == 'https://society.example/authors'


def test_a_record_on_another_publisher_is_unchanged():
    record = Rec('https://www.sciencedirect.com/journal/research-policy')
    assert ir.entry_urls(record) == ['https://www.sciencedirect.com/journal/research-policy']


def test_duplicates_are_not_tried_twice():
    record = Rec('https://pubsonline.informs.org/journal/opre',
                 doaj={'guidelines_url': 'https://pubsonline.informs.org/page/opre/submission-guidelines'})
    urls = ir.entry_urls(record)
    assert urls.count('https://pubsonline.informs.org/page/opre/submission-guidelines') == 1


# --- SAGE's retired per-journal subdomains ---------------------------------------------------
# Most stored homepages are of this shape: abs.sagepub.com, cde.sagepub.com. The host no longer
# serves the journal, but the subdomain is the same code the guidelines path wants.

def test_a_per_journal_subdomain_carries_the_code():
    urls = candidates_for(Rec('http://abs.sagepub.com/'))
    assert urls[0] == 'https://www.sagepub.com/docs/default-source/msg/ABS'


def test_several_retired_subdomains_from_the_index():
    for homepage, code in (('http://ach.sagepub.com/', 'ACH'), ('http://cde.sagepub.com/', 'CDE'),
                           ('http://cnc.sagepub.com/', 'CNC'), ('http://adh.sagepub.com/', 'ADH')):
        assert candidates_for(Rec(homepage))[0].endswith('/msg/' + code), homepage


def test_a_subdomain_that_is_not_a_journal_yields_nothing():
    """www, journals, us, uk and the like are site sections, not journal codes."""
    for homepage in ('http://www.sagepub.com/', 'https://journals.sagepub.com/',
                     'https://us.sagepub.com/en-us/nam/journal/business-perspectives-and-research',
                     'https://info.emeraldinsight.com/', 'https://advertising.sagepub.com/'):
        assert candidates_for(Rec(homepage)) == [], homepage


def test_a_numeric_product_page_yields_nothing():
    """www.sagepub.com/journals/Journal201729/title has a product id, not a journal code."""
    assert candidates_for(Rec('http://www.sagepub.com/journals/Journal201729/title')) == []
    assert candidates_for(Rec('http://www.sagepub.com/journalsProdDesc.nav?prodId=Journal202004')) == []


def test_the_path_still_wins_over_the_subdomain():
    urls = candidates_for(Rec('https://journals.sagepub.com/home/alj'))
    assert urls[0].endswith('/msg/ALJ')
