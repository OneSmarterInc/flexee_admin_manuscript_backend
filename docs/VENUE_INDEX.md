# Venue index, layer 1: the spine

Build plan step 2. A catalogue of journals in the target fields, built from free structured
sources with no AI. Records are labelled **Listed** until the agent reads their rules (step 4).

## Sources

| Source | What it gives | Calls |
|---|---|---|
| OpenAlex `/sources` | The journal list: title, ISSNs, publisher, country, open access, subject mix, output and citation counts, first and last publication year | ~1 per 100 journals |
| Crossref `/journals/{issn}` | Whether the journal registers DOIs, how many, and the first year with DOIs | 1 per journal |
| DOAJ `/api/search/journals/issn:{issn}` | Open-access titles only: DOAJ listing, review type and time, APC, and the official guideline, scope and board pages | 1 per open-access journal |
| ISSN | Checksum validation, and whether Crossref lists the same ISSNs | none |

The ISSN Portal itself is a paid service and disallows automated access, so it is not called.

## Which journals are kept

OpenAlex lists every topic a journal ever published in, so one stray paper must not pull a
journal in. The subject mix of each journal's output decides:

| Main subject | Kept when |
|---|---|
| A core field: business & management, MIS, information systems, decision sciences / operations research, strategy, OB/HRM, technology & innovation | Always |
| A bridging field: Education, Artificial Intelligence, Industrial & Manufacturing Engineering | At least 10% of its output is in the other target fields (`VENUE_INDEX_MIN_CORE_SHARE`). Keeps business/IS education, ed-tech and AI-in-organizations journals; drops general school teaching and pure AI |
| Anything else (accounting, linguistics, engineering, ...) | At least 50% of its output is in the target fields (`VENUE_INDEX_MIN_SCOPE_SHARE`) and 10% in core fields |

Journals must also have at least `VENUE_INDEX_MIN_WORKS` works and have published within
`VENUE_INDEX_MAX_INACTIVE_YEARS`. A journal already in the index that no longer qualifies (for
example after the rules were narrowed) is removed at the next refresh, unless it is linked to a
live venue.

Even with these rules the fields hold more than 3,000 genuine journals, so the index is capped
at the `VENUE_INDEX_MAX_RECORDS` (2,500) most-published ones. OpenAlex returns journals largest
first, so reaching the cap is a size cutoff: the run records it (for example "down to 180
works"), journals below it leave the index (unless linked to a live venue), and the pass still
counts as complete, so journals above the cutoff that vanish from OpenAlex are flagged.

The first real import (October 2026) with a looser 25% rule kept ~3,000 journals: 1,121 mainly
education, 225 mainly AI, and ~500 whose main subject was outside the fields. These rules
target the plan's 1,500 to 2,500.

If OpenAlex rejects the subfield filter, the importer falls back to keyword searches with the
same scope check, and records which method it used on the run.

## Running it

```
python manage.py import_venue_index               # full: catalogue refresh, then Crossref/DOAJ checks
python manage.py import_venue_index --enrich-only # only the checks that are due
python manage.py ensure_venue_index_schedule      # monthly refresh on the 1st + daily catch-up
```

The command prints progress as it goes (each OpenAlex page, then "Checked N of M"). Ctrl+C stops
it cleanly: everything saved is kept, and running it again skips checks already done.
`--minutes 10` stops it after ten minutes.

Speed: OpenAlex pages are requested largest journals first with only the needed fields, and
paging stops once journals fall below `VENUE_INDEX_MIN_WORKS`. Crossref allows 3 parallel
requests (10 per second) to callers who send a contact email and 1 request (5 per second)
otherwise, so set `VENUE_INDEX_CONTACT_EMAIL`. DOAJ is called one at a time (about 2 per
second) and only for open-access titles.

The admin page **Venue Index** shows coverage, the last run, and every record, and can start a
run or turn the schedule on.

Each run stops after `VENUE_INDEX_TIME_LIMIT_MINUTES` and saves as it goes; unfinished checks
continue in the next run. A journal that drops out of the catalogue is kept and flagged
("missing"), never deleted, and only a complete catalogue pass can flag it.

Cost: a full pass of ~2,500 journals is about 25 OpenAlex list calls plus one Crossref call per
journal, well within OpenAlex's free daily allowance even without a key.
