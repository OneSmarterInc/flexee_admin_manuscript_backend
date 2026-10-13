"""How much the manuscript must change for a venue (8 October instructions, 2.5).

Every gap gets a class:
  edit     change existing text, no new content (a word cut, a declaration, reference trimming)
  section  add or restructure a section, or add content (required sections, falling short of a minimum)
  study    new data, method or study design
  out      cannot be fixed by revising this manuscript for this venue (article type not accepted,
           the venue's rules are not configured): the venue is left out of the plan, with the reason
Unknown gaps are 'section' and marked unclassified, so they are never treated as cheap.
"""
import re


CLASS_RANK = {'ready': 0, 'edit': 1, 'section': 2, 'study': 3}
CLASS_LABEL = {'ready': 'Ready', 'edit': 'Edits', 'section': 'New section', 'study': 'New study'}

# Compliance distance (submission plan spec, section 5): a count of required changes weighted by
# kind. A length or formatting change is light, a missing section or declaration is medium, a
# methodological or scope change is heavy. Tune these from evidence, here and nowhere else.
CLASS_KIND = {'edit': 'light', 'section': 'medium', 'study': 'heavy'}
COMPLIANCE_WEIGHTS = {'light': 1, 'medium': 3, 'heavy': 9}


# Spec 2.4: the plan never speaks of odds, ratings or rankings. Venue rule text is copied from the
# venue's own pages and can say "likely desk rejected (90% odds)": such text is replaced at runtime.
BANNED_WORDS = re.compile(r'probab|likel|chance|odds|%|top tier|\bA\*|ranked|rating', re.I)
NEUTRAL_MESSAGE = {
    'edit': 'Edit the manuscript to meet a venue rule.',
    'section': 'Add or restructure a section to meet a venue rule.',
    'study': 'New data or a new method is needed for this venue.',
    'out': 'This venue does not take this manuscript as it is.',
    'prepare': 'Prepare an item the venue asks for.',
}


def plain_words(text, cls='section', quantity=None, unit=''):
    """The text as it is, or a neutral sentence when it uses odds or rating language."""
    text = str(text or '')
    if not BANNED_WORDS.search(text):
        return text
    if quantity and unit:
        verb = 'Cut' if cls == 'edit' else 'Add'
        return f'{verb} about {int(quantity):,} {unit} to meet a venue rule.'
    return NEUTRAL_MESSAGE.get(cls, NEUTRAL_MESSAGE['section'])


def plain_item(change):
    """A gap item with its message in plain words (no odds, no ratings)."""
    out = dict(change)
    out['message'] = plain_words(change.get('message'), change.get('class') or 'section',
                                 change.get('quantity'), change.get('unit') or '')
    return out


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


def compliance_distance(items):
    """Sum of the weights of the manuscript changes (preparation items such as a cover letter do not count)."""
    return sum(COMPLIANCE_WEIGHTS[CLASS_KIND.get(i.get('class'), 'medium')] for i in items
               if i.get('class') in CLASS_KIND or i.get('class') not in {'prepare', 'out'})


def preparation_items(config):
    """The venue's required submission items (cover letter, data statement, ...), as things to prepare.
    They are listed with the step's required changes but are not changes to the manuscript, so they
    never move a venue out of the 'no changes' band."""
    out = []
    for entry in (getattr(config, 'required_submission_items', None) or []):
        if not isinstance(entry, dict) or entry.get('required') is False:
            continue
        label = str(entry.get('label') or entry.get('name') or '').strip()
        if label:
            out.append(item('venue_item', f'Prepare for the submission form: {label}.', 'prepare'))
    return out
