"""Merge every rules_json/*.json into one import file, de-duplicate, and check it.

Run it from anywhere. It finds the rules_json folder itself:

    python merge_rules.py

It writes abdc_rules_merged.json next to the rules_json folder.

Three things it does that --dry-run cannot:

1. De-duplicates. ChatGPT, Gemini and Claude worked overlapping lists, so the same
   journal appears in more than one file. The loader would take whichever came last;
   this keeps the richest entry instead.
2. Catches the multi-rule defect. The platform checks every word_count rule against
   the same manuscript word count, with no idea which article type the author chose.
   Two rules on one journal means a valid paper gets rejected for a limit that does
   not apply to it. --dry-run does not catch this, because each rule is valid on its own.
3. Checks the field constraints that make the loader reject a whole file.
"""
import json
import os
import sys

# These mirror the loader. Keep them in step with review/author_api.py.
RULE_FIELDS = {
    'word_count': {'>', '>=', '<', '<=', '==', '!='},
    'reference_count': {'>', '>=', '<', '<=', '==', '!='},
    'required_sections': {'missing_any', 'missing_all'},
    'manuscript_type': {'==', '!=', 'in', 'not_in'},
    'disclosure': {'contains', 'not_contains', 'empty', 'not_empty'},
}
ITEM_TYPES = {'text', 'textarea', 'url', 'checkbox', 'file'}
CONTENT_KEYS = ('article_types', 'required_submission_items',
                'structured_desk_rejection_rules', 'aims_scope')


def find_folder():
    candidates = [
        os.path.join(os.getcwd(), 'rules_json'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rules_json'),
        os.path.join(os.path.expanduser('~'), 'Downloads', 'rules_json'),
    ]
    for path in candidates:
        if os.path.isdir(path):
            return path
    print('Could not find a rules_json folder. Looked in:')
    for path in candidates:
        print('   ', path)
    print('\nOpen a terminal in the folder that CONTAINS rules_json, then run this again.')
    raise SystemExit(1)


def richness(entry):
    """How much an entry carries, used to pick a winner between duplicates."""
    score = 0
    for key in CONTENT_KEYS:
        value = entry.get(key)
        if isinstance(value, list):
            score += len(value) * 10
        elif isinstance(value, str) and value.strip():
            score += 5
    for key in ('config_notes', 'policies', 'issn', 'issn_online', 'publisher'):
        if str(entry.get(key) or '').strip():
            score += 1
    return score


def key_for(entry):
    title = str(entry.get('title') or '').strip().lower()
    return ' '.join(title.split())


def main():
    folder = find_folder()
    out_path = os.path.join(os.path.dirname(folder), 'abdc_rules_merged.json')
    names = sorted(n for n in os.listdir(folder) if n.lower().endswith('.json'))
    if not names:
        print(f'No .json files in {folder}')
        raise SystemExit(1)

    print(f'Reading {len(names)} files from {folder}\n')

    best = {}
    issn_seen = {}
    from_file = {}
    unreadable = []
    total_read = 0
    duplicates = []

    for name in names:
        path = os.path.join(folder, name)
        try:
            with open(path, encoding='utf-8-sig') as handle:
                data = json.load(handle)
        except Exception as exc:
            unreadable.append((name, str(exc)))
            continue
        if isinstance(data, dict):
            for wrapper in ('journals', 'entries', 'data', 'results'):
                if isinstance(data.get(wrapper), list):
                    data = data[wrapper]
                    break
        if not isinstance(data, list):
            unreadable.append((name, 'not a JSON array'))
            continue
        print(f'  {name:16} {len(data):4} entries')
        total_read += len(data)
        for entry in data:
            if not isinstance(entry, dict):
                continue
            key = key_for(entry)
            if not key:
                continue
            if key in best:
                duplicates.append((entry.get('title'), from_file[key], name))
                if richness(entry) > richness(best[key]):
                    best[key] = entry
                    from_file[key] = name
            else:
                best[key] = entry
                from_file[key] = name

    merged = list(best.values())

    print(f'\n{total_read} entries read, {len(duplicates)} duplicates collapsed, '
          f'{len(merged)} unique journals.')

    if unreadable:
        print('\nFILES THAT WOULD NOT PARSE:')
        for name, why in unreadable:
            print(f'  {name}: {why}')

    if duplicates:
        print('\nDuplicates (kept the richer entry):')
        for title, first, second in duplicates[:30]:
            print(f'  {str(title)[:52]:52} {first} + {second}')
        if len(duplicates) > 30:
            print(f'  ... and {len(duplicates) - 30} more')

    # --- checks ---------------------------------------------------------
    multi_rule = []
    bad_field = []
    bad_operator = []
    bad_item_type = []
    missing_required = []
    no_content = []
    issn_clash = []

    for entry in merged:
        title = str(entry.get('title') or '(no title)')[:58]

        if not str(entry.get('title') or '').strip() or not str(entry.get('guidelines_url') or '').strip():
            missing_required.append(title)
        if not any(entry.get(k) for k in CONTENT_KEYS):
            no_content.append(title)

        for which in ('issn', 'issn_online'):
            value = str(entry.get(which) or '').strip()
            if not value:
                continue
            if value in issn_seen and issn_seen[value] != key_for(entry):
                issn_clash.append((value, issn_seen[value], title))
            else:
                issn_seen[value] = key_for(entry)

        rules = entry.get('structured_desk_rejection_rules') or []
        if not isinstance(rules, list):
            bad_field.append((title, 'rules is not a list'))
            continue
        word_rules = 0
        for rule in rules:
            if not isinstance(rule, dict):
                bad_field.append((title, 'rule is not an object'))
                continue
            field = rule.get('field')
            operator = rule.get('operator')
            if field not in RULE_FIELDS:
                bad_field.append((title, f'field {field!r}'))
                continue
            if operator not in RULE_FIELDS[field]:
                bad_operator.append((title, f'{field} {operator!r}'))
            if field == 'word_count':
                word_rules += 1
        if word_rules > 1:
            multi_rule.append((title, word_rules,
                               [r.get('value') for r in rules
                                if isinstance(r, dict) and r.get('field') == 'word_count']))

        items = entry.get('required_submission_items') or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get('type') not in ITEM_TYPES:
                    bad_item_type.append((title, repr(item.get('type'))))

    print('\n' + '=' * 70)
    problems = 0

    if multi_rule:
        problems += len(multi_rule)
        print(f'\nSTOP — {len(multi_rule)} journals have more than one word_count rule.')
        print('Every rule fires against the same word count, so a valid paper gets')
        print('rejected for a limit that does not apply to its article type.')
        print('Fix these before importing: keep the main article type\'s limit, move')
        print('the rest into config_notes as prose.\n')
        for title, count, values in multi_rule:
            print(f'  {title:58} {count} rules: {values}')

    for label, rows in (('entries missing title or guidelines_url', missing_required),
                        ('entries with no usable content', no_content)):
        if rows:
            problems += len(rows)
            print(f'\n{len(rows)} {label}:')
            for row in rows[:20]:
                print(f'  {row}')

    for label, rows in (('invalid rule field', bad_field),
                        ('invalid operator for its field', bad_operator),
                        ('invalid required_submission_items type', bad_item_type)):
        if rows:
            problems += len(rows)
            print(f'\n{len(rows)} {label}:')
            for title, detail in rows[:20]:
                print(f'  {title:58} {detail}')

    if issn_clash:
        print(f'\n{len(issn_clash)} ISSNs used by two different titles — check these by hand:')
        for value, first, title in issn_clash[:20]:
            print(f'  {value}  {first[:30]} / {title[:30]}')

    with open(out_path, 'w', encoding='utf-8') as handle:
        json.dump(merged, handle, indent=2, ensure_ascii=False)

    print('\n' + '=' * 70)
    print(f'Written: {out_path}')
    print(f'{len(merged)} journals.')
    if problems:
        print(f'\n{problems} problems above. Fix them, then run this again.')
        print('Do NOT import until this prints clean.')
        raise SystemExit(1)
    print('\nClean. Safe to dry-run.')


if __name__ == '__main__':
    main()
