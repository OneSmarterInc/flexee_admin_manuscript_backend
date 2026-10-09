"""How much the manuscript must change for a venue (8 October instructions, 2.5).

Every gap gets a class:
  edit     change existing text, no new content (a word cut, a declaration, reference trimming)
  section  add or restructure a section, or add content (required sections, falling short of a minimum)
  study    new data, method or study design
  out      cannot be fixed by revising this manuscript for this venue (article type not accepted,
           the venue's rules are not configured): the venue is left out of the plan, with the reason
Unknown gaps are 'section' and marked unclassified, so they are never treated as cheap.
"""

CLASS_RANK = {'ready': 0, 'edit': 1, 'section': 2, 'study': 3}
CLASS_LABEL = {'ready': 'Ready', 'edit': 'Edits', 'section': 'New section', 'study': 'New study'}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def item(code, message, cls, quantity=None, unit='', source=''):
    return {'code': code, 'message': message, 'class': cls, 'quantity': quantity, 'unit': unit,
            'source_locator': source}


def classify_violation(violation, source=''):
    """A structured desk-rule violation (field, operator, value, actual, message) as a gap item."""
    field = violation.get('field')
    op = str(violation.get('operator') or '')
    expected, actual = violation.get('value'), violation.get('actual')
    message = str(violation.get('message') or '').strip()
    if field in {'word_count', 'reference_count'}:
        unit = 'words' if field == 'word_count' else 'references'
        exp, act = _int(expected), _int(actual)
        if exp is None or act is None:
            return item(f'{field}_rule', message or f'{unit.title()} rule not met.', 'section', source=source)
        if op in {'>', '>='}:  # over a maximum: cut
            amount = act - exp + (1 if op == '>=' else 0)
            return item(f'{field}_over', message or f'Cut about {amount:,} {unit} (limit {exp:,}).', 'edit',
                        amount, unit, source)
        if op in {'<', '<='}:  # under a minimum: add content
            amount = exp - act + (1 if op == '<=' else 0)
            return item(f'{field}_under', message or f'Add about {amount:,} {unit} (minimum {exp:,}).', 'section',
                        amount, unit, source)
        return item(f'{field}_rule', message or f'{unit.title()} rule not met.', 'section', source=source)
    if field == 'required_sections':
        missing = (actual or {}).get('missing', []) if isinstance(actual, dict) else []
        text = message or ('Add the required section' + ('s: ' if len(missing) > 1 else ': ') + ', '.join(missing) + '.')
        return item('sections_missing', text, 'section', len(missing) or 1, 'sections', source)
    if field == 'manuscript_type':
        return item('type_not_accepted', message or 'This venue does not take this article type.', 'out', source=source)
    if field == 'disclosure':
        return item('declaration', message or 'Adjust the disclosure statement.', 'edit', 1, 'statements', source)
    return item('unclassified', message or 'A venue rule is not met.', 'section', source=source)


def items_for(match):
    """The match's gap items, plus any free-text gap (e.g. added later by the AI fit check) that has
    no structured item yet, as an unclassified section."""
    items = [dict(i) for i in (match.gap_items or []) if isinstance(i, dict)]
    known = {i.get('message') for i in items}
    for text in match.gaps or []:
        if isinstance(text, str) and text and text not in known:
            items.append(item('unclassified', text, 'section'))
    return items


def effort_of(items):
    """(class, hardest-class count, total quantity in that class) for ordering. 'out' wins outright."""
    if any(i['class'] == 'out' for i in items):
        return 'out', 0, 0
    if not items:
        return 'ready', 0, 0
    hardest = max(items, key=lambda i: CLASS_RANK.get(i['class'], 2))['class']
    same = [i for i in items if i['class'] == hardest]
    return hardest, len(same), sum(i.get('quantity') or 0 for i in same)
