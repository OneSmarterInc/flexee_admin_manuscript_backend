"""Export the ABDC 2025 journals that still have no rules, as paste-ready batch files.

Run from the backend root:

    python manage.py shell -c "exec(open('make_next_batches.py').read())"

Writes next_batches/batch_01.txt .. and next_remaining.csv.

Two things it does on purpose:

A* first, then A, then B, then C. Within a rating, journals are ordered by whether
their guidelines are reachable at all — measured, not guessed. Spending a batch on
20 Elsevier journals returns 20 skips, so the hosts that answer go first.

Journals with no index record are left OUT of the batches and counted separately.
The loader matches an entry to an index record by ISSN then exact title, and skips
anything it cannot match, so reading rules for those is wasted work until a worklist
import puts them in the index.
"""
import csv
import os

from review.models import ABDCJournal

OUT_DIR = 'next_batches'
CSV_PATH = 'next_remaining.csv'
PER_BATCH = 20
RATINGS = ('A*', 'A', 'B', 'C')          # change to ('A*', 'A', 'B', 'C') to go wider
RATING_ORDER = {'A*': 0, 'A': 1, 'B': 2, 'C': 3}

# Measured on 27 October against our own client. Tier 1 patterns are proved to work.
PATTERN_WORKS = ('sage', 'informs', 'american marketing', 'american economic',
                 'american psychological', 'american accounting', 'academy of management',
                 'econometric society', 'university of chicago', 'chicago press',
                 'cambridge', 'now publishers')
LIKELY_ANSWERS = ('association', 'society', 'institute', 'academy', 'university', 'press',
                  'college', 'school', 'foundation', 'council', 'federation', 'mit',
                  'annual reviews', 'de gruyter', 'brill', 'duke', 'scielo', 'ios press')
REFUSES = ('elsevier', 'wiley', 'taylor', 'routledge', 'informa', 'springer', 'palgrave',
           'emerald', 'oxford', 'sciencedirect', 'inderscience', 'igi global', 'ingenta')
LABELS = {1: 'pattern proved to work', 2: 'society or university press, usually answers',
          3: 'unknown publisher, worth a try', 4: 'refuses every address tried'}


def tier(publisher):
    p = (publisher or '').lower()
    if not p.strip():
        return 3
    if any(k in p for k in PATTERN_WORKS):
        return 1
    if any(k in p for k in REFUSES):
        return 4
    if any(k in p for k in LIKELY_ANSWERS):
        return 2
    return 3


rows = ABDCJournal.objects.filter(edition=2025, rating__in=RATINGS).values_list(
    'rating', 'title', 'publisher', 'issn', 'issn_online',
    'indexed_venue_id', 'indexed_venue__rules_status')

todo = []
no_index = []
done = 0

for rating, title, publisher, issn, issn_online, index_id, rules in rows:
    rating = (rating or '').strip().upper()
    title = (title or '').strip()
    publisher = (publisher or '').strip()
    issn = (issn or '').strip()
    issn_online = (issn_online or '').strip()
    if not title:
        continue
    entry = {'rating': rating, 'title': title, 'publisher': publisher,
             'issn': issn, 'issn_online': issn_online, 'tier': tier(publisher)}
    if index_id is None:
        no_index.append(entry)
        continue
    if rules == 'ready':
        done += 1
        continue
    todo.append(entry)

todo.sort(key=lambda r: (RATING_ORDER.get(r['rating'], 9), r['tier'], r['title'].lower()))

print()
print(f'{done} already have rules.')
print(f'{len(todo)} to do, in {", ".join(RATINGS)}.')
print(f'{len(no_index)} left out — no index record, so the loader cannot match them.\n')

for rating in RATINGS:
    block = [r for r in todo if r['rating'] == rating]
    if not block:
        continue
    print(f'  {rating}: {len(block)}')
    counts = {}
    for r in block:
        counts[r['tier']] = counts.get(r['tier'], 0) + 1
    for t in sorted(counts):
        print(f'      tier {t}  {counts[t]:4}  {LABELS[t]}')

os.makedirs(OUT_DIR, exist_ok=True)
for old in os.listdir(OUT_DIR):
    if old.startswith('batch_') and old.endswith('.txt'):
        os.remove(os.path.join(OUT_DIR, old))

count = 0
for n in range((len(todo) + PER_BATCH - 1) // PER_BATCH):
    block = todo[n * PER_BATCH:(n + 1) * PER_BATCH]
    path = os.path.join(OUT_DIR, f'batch_{n + 1:02d}.txt')
    with open(path, 'w', encoding='utf-8') as out:
        out.write('Journals to do in this batch:\n\n')
        for r in block:
            out.write(f"{r['title']} | {r['issn']} | {r['issn_online']} | {r['publisher']}\n")
    count += 1

with open(CSV_PATH, 'w', encoding='utf-8', newline='') as handle:
    writer = csv.DictWriter(handle, fieldnames=['rating', 'title', 'publisher', 'issn',
                                                'issn_online', 'tier'])
    writer.writeheader()
    writer.writerows(todo)

print(f'\n{count} batch files written to {OUT_DIR}/batch_01.txt .. batch_{count:02d}.txt')
print(f'Full list also in {CSV_PATH}')

if no_index:
    with open('next_no_index_record.csv', 'w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['rating', 'title', 'publisher', 'issn',
                                                    'issn_online', 'tier'])
        writer.writeheader()
        writer.writerows(sorted(no_index, key=lambda r: (RATING_ORDER.get(r['rating'], 9),
                                                         r['title'].lower())))
    top = [r for r in no_index if r['rating'] in ('A*', 'A')]
    print(f'\n{len(no_index)} journals have no index record — written to '
          f'next_no_index_record.csv.')
    print(f'{len(top)} of them are A* or A. Reading rules for these is wasted work until')
    print('a worklist import puts them in the index.')
