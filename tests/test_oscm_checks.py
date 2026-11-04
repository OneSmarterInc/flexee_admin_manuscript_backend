from io import BytesIO
from types import SimpleNamespace
from docx import Document
from docx.shared import Inches, Pt
from django.core.files.base import ContentFile
from review.services.oscm_checks import check_oscm


def manuscript(correct=False):
    d = Document()
    d.styles['Normal'].font.name = 'Times New Roman'
    d.styles['Normal'].font.size = Pt(12)
    d.styles['Normal'].paragraph_format.line_spacing = 1.5
    for s in d.sections:
        s.top_margin = s.bottom_margin = s.left_margin = s.right_margin = Inches(1)
    d.add_paragraph('Abstract')
    d.add_paragraph(' '.join(['word'] * (100 if correct else 251)))
    d.add_paragraph('Keywords: inventory; learning; simulation' if correct else 'Keywords: inventory; learning')
    d.add_paragraph('Introduction')
    p = d.add_paragraph('Some body text without identifying information.')
    if not correct:
        p.paragraph_format.line_spacing = 1
        p.runs[0].font.size = Pt(10)
        d.sections[0].left_margin = Inches(.5)
    d.core_properties.author = '' if correct else 'Test Author'
    d.core_properties.last_modified_by = ''
    b = BytesIO(); d.save(b)
    return SimpleNamespace(manuscript_filename='paper.docx',manuscript_file=ContentFile(b.getvalue()),author_name='Test Author',author_email='test@example.com')


def test_oscm_detects_measured_failures():
    checks = {x['id']:x for x in check_oscm(manuscript())}
    for key in ['abstract-length','keywords','margins','metadata','font-size','spacing']:
        assert checks['oscm-' + key]['status'] == 'failed', key
    assert checks['oscm-abstract-length']['actual'] == 251


def test_oscm_corrected_file_clears_same_failures():
    from review.services.journal_checks import compare_checks
    previous = check_oscm(manuscript())
    checks = {x['id']:x for x in compare_checks(check_oscm(manuscript(True)),previous)}
    for key in ['abstract-length','keywords','margins','metadata','font-size','spacing']:
        assert checks['oscm-' + key]['change'] == 'resolved', key
    assert checks['oscm-identity']['status'] == 'not_checked'


def test_unsupported_format_not_a_pass():
    m = manuscript(); m.manuscript_filename = 'paper.pdf'
    assert all(x['status'] == 'not_checked' for x in check_oscm(m))


def test_no_abstract_boundary_is_unknown():
    d=Document();d.add_paragraph('Abstract');d.add_paragraph(' '.join(['word']*300))
    b=BytesIO();d.save(b);m=manuscript();m.manuscript_file=ContentFile(b.getvalue())
    checks={x['id']:x for x in check_oscm(m)}
    assert checks['oscm-abstract-length']['status']=='not_checked'
