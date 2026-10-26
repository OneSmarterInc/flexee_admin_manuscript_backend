# ABDC 2025 Journal Quality List — Local import

The prior ISSN worklist import is separate. Do **not** re-run it expecting ratings: it discards every column except ISSNs.

This implementation adds an **admin-only** ABDC catalogue. Each of the 2,648 official source rows is stored, including the four without a usable ISSN. It does **not** create or publish unverified IndexedVenue or Venue rows. It links to existing IndexedVenue rows when matching is unambiguous; other rows remain independently searchable with a review status.

## Steps in Windows PowerShell

1. Apply the backend patch, and then the frontend patch, in their respective repository folders. Do not push unless instructed.
2. Copy `abdc_2025_flexee_full_metadata.csv` into `D:\flexee\flexee_admin_manuscript_backend`.
3. Confirm the `.env` points at the intended local database, not production.

```powershell
cd D:\flexee\flexee_admin_manuscript_backend
.\.venv\Scripts\Activate.ps1
python manage.py check
python manage.py migrate
python manage.py import_abdc_jql --file ".\abdc_2025_flexee_full_metadata.csv" --edition 2025 --dry-run
python manage.py import_abdc_jql --file ".\abdc_2025_flexee_full_metadata.csv" --edition 2025
python manage.py shell -c "from review.models import ABDCJournal; from django.db.models import Count; print('ABDC rows:', ABDCJournal.objects.filter(edition=2025).count()); print(list(ABDCJournal.objects.filter(edition=2025).values('rating').annotate(n=Count('id')))); print(list(ABDCJournal.objects.filter(edition=2025).values('match_status').annotate(n=Count('id'))))"
```

Expected source count: **2648**; by rating: **A* 219, A 630, B 859, C 940**. The number matched to IndexedVenue depends on your local database. Review at `http://localhost:5173/admin/abdc-journals` (Vite dev frontend), signed in as a platform superuser.

## Matching rules

* Exact valid print/online ISSNs across all IndexedVenue rows: auto-link if exactly one candidate.
* When either the source or index lacks a valid ISSN, exact title plus publisher can auto-link if unique.
* Conflicting valid ISSNs, duplicate identifiers or a title-only candidate: pending human review, **not** auto-linked.
* No evidence: unmatched (source row still retained).
* Existing manual matches (`match_method=manual`) are preserved on re-import.
* Re-importing the same edition and Excel row is idempotent. A DB transaction ensures all-or-nothing writes.

ABDC rating is **not** FLEXEE's trust tier and does not establish journal legitimacy or current editorial rules. This view is admin-only. Check ABDC reuse permissions before any public reproduction of the list/ratings.
