"""Source-backed, opt-in initial-submission document checks.
Only explicit, measurable failures are returned. Missing style/extraction evidence stays unknown.
"""
from io import BytesIO
import re
from docx import Document
from docx.oxml.ns import qn
from ..storage_security import validate_upload



def check_profile(manuscript, profile):
    """Evaluate only explicitly enabled, source-backed initial-submission constraints."""
    if profile.get('manuscript_types') and getattr(manuscript, 'manuscript_type', None) not in profile['manuscript_types']:
        return []
    spec = profile.get('checks', {})
    source = profile['source_url']
    prefix = profile['id']
    allowed = set(spec)

    rows = []
    def add(code, title, failed, actual, expected, action, location):
        if code != 'document' and code not in allowed: return
        rows.append(dict(id=prefix + '-' + code, title=title,
                         status='not_checked' if failed is None else 'failed' if failed else 'passed',
                         actual=actual, expected=expected, action=action, location=location,
                         rule_source=source, source_url=source))
    if not str(manuscript.manuscript_filename).lower().endswith('.docx'):
        add('document', 'Document checks unavailable', None, None, 'Readable DOCX for these automatic checks',
            'These automatic formatting checks support DOCX files. Other formats need manual review.', 'Uploaded file')
        return rows
    try:
        manuscript.manuscript_file.open('rb')
        try: content = manuscript.manuscript_file.read()
        finally: manuscript.manuscript_file.close()
        validate_upload(content, manuscript.manuscript_filename)
        doc = Document(BytesIO(content))
    except Exception:
        add('document', 'Document could not be inspected', None, None, 'Readable DOCX',
            'Check that the Word document opens correctly.', 'Uploaded file')
        return rows
    # Paragraphs in tables and text boxes are included in counts. Header/footer text is separate.
    paragraphs = doc.element.body.xpath('.//w:p')
    texts = [''.join(p.xpath('.//w:t/text()')).strip() for p in paragraphs]
    all_text = '\n'.join(texts)
    words = len(re.findall(r"\b[\w]+(?:[-’'][\w]+)*\b", all_text))
    # Images can contain unextractable words: never claim full coverage for those.
    images = bool(doc.element.body.xpath('.//w:drawing | .//w:pict'))
    abstract = None
    keywords = None
    for index, line in enumerate(texts):
        if re.match(r'^abstract\b\s*[:.\-–—]?\s*', line, re.I):
            inline = re.sub(r'^abstract\b\s*[:.\-–—]?\s*', '', line, flags=re.I).strip()
            values = [inline] if inline else []
            for following in texts[index + 1:]:
                if re.match(r'^(key\s*words?\b|(?:1[.)]?\s+)?introduction\b)', following, re.I):
                    break
                if following: values.append(following)
            else:
                continue  # no reliable boundary: do not count the rest of the paper as abstract
            abstract = values
            break
    for index, line in enumerate(texts):
        if re.match(r'^key\s*words?\s*[:：]', line, re.I):
            raw = re.sub(r'^key\s*words?\s*[:：]\s*', '', line, flags=re.I)
            if raw.strip() and (re.search(r'[;,]',raw) or len(raw.split()) == 1):
                keywords = [x.strip(' .') for x in re.split(r'[;,]', raw) if x.strip(' .')]
            break
    n = sum(len(re.findall(r"\b[\w]+(?:[-’'][\w]+)*\b", p)) for p in abstract) if abstract else None
    for code, value, label, location in [
        ('abstract-length', n, 'abstract', 'Abstract in uploaded file'),
        ('keywords', len(keywords) if keywords else None, 'keyword list', 'Keyword line')]:
        if code not in spec: continue
        minimum, maximum = spec[code]
        failed = None if value is None else ((minimum is not None and value < minimum) or (maximum is not None and value > maximum))
        expected = (f'{minimum}–{maximum}' if minimum is not None and maximum is not None else f'at most {maximum}' if maximum is not None else f'at least {minimum}')
        unit = 'words' if code == 'abstract-length' else 'keywords'
        add(code, f'Adjust your {label}', failed, value, expected + ' ' + unit,
            f'Your {label} contains {value} {unit}. Use {expected} {unit}.', location)
    add('abstract-paragraphs', 'Use one paragraph for the abstract', len(abstract) > 1 if abstract else None,
        len(abstract) if abstract else None, 'One paragraph', 'Combine the abstract into one paragraph.', 'Abstract')
    bad_margins, bad_columns = [], []
    for i, section in enumerate(doc.sections, 1):
        margins = [getattr(section, edge + '_margin') for edge in ('top','bottom','left','right')]
        if any(m is not None and abs(m.inches - spec.get('margins', 1)) > .015 for m in margins): bad_margins.append(i)
        cols = section._sectPr.find(qn('w:cols'))
        if cols is not None and (int(cols.get(qn('w:num'), '1')) > 1 or len(cols.findall(qn('w:col'))) > 1): bad_columns.append(i)
    add('margins', 'Adjust page margins', bool(bad_margins), bad_margins, f"{spec.get('margins', 1) * 2.54:.2f} cm on every side",
        f"Set all margins to {spec.get('margins', 1) * 2.54:.2f} cm in document sections {", ".join(map(str,bad_margins))}.", 'Word page setup')
    add('columns', 'Use a single-column layout', bool(bad_columns), bad_columns, 'Single column for initial submission',
        f'Change document sections {", ".join(map(str,bad_columns))} to one column.', 'Word page setup')
    metadata = [name for name in ('author','last_modified_by') if str(getattr(doc.core_properties, name, '') or '').strip()]
    add('metadata', 'Remove author details from file properties', bool(metadata), metadata, 'Anonymous main manuscript',
        'In Word, use Inspect Document to remove author and last-saved-by information, then save the anonymized file.', 'Word file properties')
    # Known author identity only, outside References; no inference from arbitrary proper names.
    front = re.split(r'(?im)^\s*(?:\d+[.)]?\s*)?(?:references|bibliography)\s*$', all_text)[0]
    header_footer = '\n'.join(p.text for section in doc.sections for part in
                            (section.header, section.footer, section.first_page_header, section.first_page_footer,
                             section.even_page_header, section.even_page_footer) for p in part.paragraphs)
    identities = [getattr(manuscript,'author_name',''), getattr(manuscript,'author_email','')]
    found = any(len(value.strip()) >= 5 and re.search(r'(?<!\w)' + re.escape(value.strip()) + r'(?!\w)', front + '\n' + header_footer, re.I)
                for value in identities if value)
    add('identity', 'Remove identifying author information', True if found else None,
        'Known author information detected' if found else None, 'Anonymous main manuscript',
        'Your author name or email appears in the main text, header or footer. Move identifying details to a separate title page.', 'Main text and headers/footers')
    # Resolve direct formatting, inherited styles and explicit document defaults.
    defaults = doc.styles.element.find(qn('w:docDefaults'))
    def style_chain(style):
        seen = set()
        while style is not None and style.style_id not in seen:
            seen.add(style.style_id); yield style.element; style = style.base_style
    def property_value(nodes, prop, attr):
        for node in nodes:
            if node is None: continue
            el = node.find('.//' + qn('w:' + prop))
            if el is not None and el.get(qn('w:' + attr)) is not None: return el.get(qn('w:' + attr))
        return None
    bad_font, bad_size, bad_spacing = [], [], []
    unknown_font = unknown_size = unknown_spacing = False
    body_started = False
    for i, p in enumerate(doc.paragraphs, 1):
        if re.match(r"^(?:1[.)]?\s+)?introduction\s*$", p.text.strip(), re.I):
            body_started = True
            continue
        if not body_started:
            continue
        if re.match(r'^(references|bibliography)$', p.text.strip(), re.I): break
        if not p.text.strip(): continue
        # Avoid title/heading typography and equations; inspect ordinary prose only.
        if p.style and re.search(r'title|heading|subtitle|caption', p.style.name, re.I): continue
        if re.match(r'^(figure|table|fig\.)\s*\d', p.text.strip(), re.I): continue
        nodes = [p._p.pPr, *style_chain(p.style), defaults]
        spacing = property_value(nodes, 'spacing', 'line')
        line_rule = property_value(nodes, 'spacing', 'lineRule')
        if spacing is None: unknown_spacing = True
        elif line_rule not in (None, 'auto') or int(spacing) != round(spec.get('spacing', 1.5) * 240): bad_spacing.append(i)
        for run in p.runs:
            if not run.text.strip(): continue
            if run._r.xpath('.//m:oMath') or run.font.superscript or run.font.subscript: continue
            props = [run._r.rPr, *style_chain(run.style), *style_chain(p.style), defaults]
            font = property_value(props, 'rFonts', 'ascii')
            size = property_value(props, 'sz', 'val')
            # Theme font overrides can make inherited concrete values misleading.
            theme = None
            for prop in props:
                if prop is None: continue
                fonts = prop.find('.//' + qn('w:rFonts'))
                if fonts is not None and (fonts.get(qn('w:ascii')) or fonts.get(qn('w:asciiTheme'))):
                    font = fonts.get(qn('w:ascii'))
                    theme = fonts.get(qn('w:asciiTheme'))
                    break
            if theme or font is None: unknown_font = True
            elif font.lower() != spec.get('font', 'Times New Roman').lower(): bad_font.append(i)
            if size is None: unknown_size = True
            elif int(size) != round(spec.get('font-size', 12) * 2): bad_size.append(i)
    for code,title,positions,unknown,expected in [
        ('font','Adjust the body font',bad_font,unknown_font,spec.get('font','Times New Roman')),
        ('font-size','Adjust the text size',bad_size,unknown_size,str(spec.get('font-size',12)) + '-point font'),
        ('spacing','Adjust line spacing',bad_spacing,unknown_spacing,str(spec.get('spacing',1.5)) + ' line spacing')]:
        places = sorted(set(positions))
        add(code,title,True if places else None if unknown or not body_started else False,places[:12],expected,
            f'Apply {expected} to the manuscript body. Different formatting was detected in paragraph(s) {", ".join(map(str,places[:12]))}.',
            'Word body paragraphs; headings and table formatting require separate review')
    return rows
