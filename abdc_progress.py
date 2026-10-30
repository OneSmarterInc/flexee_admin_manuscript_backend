"""How many ABDC 2025 journals still have no submission rules.

Run from the backend root:

    python manage.py shell -c "exec(open('abdc_progress.py').read())"

Counts against ABDCJournal(edition=2025), which is the authoritative list, rather
than against the index, so a journal missing from the index still shows as remaining.
"""
from collections import Counter

from review.models import ABDCJournal, IndexedVenue, Venue

ORDER = ['A*', 'A', 'B', 'C']

rows = ABDCJournal.objects.filter(edition=2025).values_list(
    'rating', 'indexed_venue_id', 'indexed_venue__rules_status',
    'indexed_venue__screening_status', 'indexed_venue__venue_id')

total = Counter()
ready = Counter()
no_index = Counter()
flagged = Counter()
excluded = Counter()

for rating, index_id, rules, screening, venue_id in rows:
    rating = (rating or '').strip().upper()
    total[rating] += 1
    if index_id is None:
        no_index[rating] += 1
        continue
    if rules == 'ready':
        ready[rating] += 1
    if screening == 'flagged':
        flagged[rating] += 1
    if screening == 'excluded':
        excluded[rating] += 1

print()
print('ABDC 2025 — journals with submission rules')
print('=' * 72)
print(f'{"":4} {"have rules":>11} {"remaining":>11} {"of total":>9}   {"not in index":>13} {"flagged":>8}')
for rating in ORDER:
    have = ready[rating]
    tot = total[rating]
    print(f'{rating:4} {have:11} {tot - have:11} {tot:9}   {no_index[rating]:13} {flagged[rating]:8}')

have_all = sum(ready.values())
tot_all = sum(total.values())
print('-' * 72)
print(f'{"ALL":4} {have_all:11} {tot_all - have_all:11} {tot_all:9}   '
      f'{sum(no_index.values()):13} {sum(flagged.values()):8}')

pct = (have_all / tot_all * 100) if tot_all else 0
print(f'\n{pct:.1f}% of the 2025 list now carries rules.')

# The top band is what matters most to authors.
top_have = ready['A*'] + ready['A']
top_tot = total['A*'] + total['A']
top_pct = (top_have / top_tot * 100) if top_tot else 0
print(f'{top_pct:.1f}% of the A* and A band ({top_have} of {top_tot}).')

print('\nIndex-wide, counting everything the platform knows, not only ABDC 2025:')
index_total = IndexedVenue.objects.count()
for status, label in (('ready', 'rules ready'), ('not_read', 'not read'),
                      ('incomplete', 'rules not found on the pages'),
                      ('failed', 'pages could not be read'),
                      ('blocked', 'site blocks automated reading')):
    print(f'  {label:34} {IndexedVenue.objects.filter(rules_status=status).count():6}')
print(f'  {"index records in total":34} {index_total:6}')
print(f'  {"live venues authors can see":34} {Venue.objects.filter(active=True).count():6}')

print('\nScreening, which gates visibility separately from rules:')
for status, label in (('not_screened', 'not screened'), ('clear', 'clear'),
                      ('flagged', 'flagged — invisible to authors'),
                      ('kept', 'reviewed and kept'), ('excluded', 'excluded')):
    print(f'  {label:34} {IndexedVenue.objects.filter(screening_status=status).count():6}')
