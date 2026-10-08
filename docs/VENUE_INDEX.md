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

## Exclusion screening (build plan step 3)

Automated screening only **flags** journals for a person to review. Nothing is excluded without
a reviewer choosing the criteria it fails and at least one evidence link, and every decision is
stored (`IndexReviewDecision`) and reversible. Wording is always about criteria, never a label:
excluded journals are hidden from authors and never listed publicly.

Catalogue signals (no AI, takes seconds): no Crossref DOIs (2 points), no ISSN (2) or an ISSN
that fails its check digit (3), publisher not stated (1), under two years of publishing (1),
DOAJ submission-to-publication of 3 weeks or less (2), output spread across four or more
unrelated fields (2), charging authors while not in DOAJ (1), publisher on the internal
blocklist (3), and at least 500 items with fewer than 0.1 citations per item (3; typical of
magazines and news titles, pre-fills "Not a peer-reviewed journal"). Three points put a journal in the **Needs review** queue. Positive signals
(DOAJ, long Crossref history, CWTS core source, ISSN confirmed by Crossref) are shown to the
reviewer and never used to exclude.

Evidence from the journal's own pages: for journals with any concern, or that charge authors
outside DOAJ, the homepage and up to two author or fee pages are read (robots.txt respected)
and scanned for exact phrases: guaranteed acceptance, publication or acceptance promised
within days, and metrics from unrecognised ranking bodies (SJIF, Global Impact Factor, Index
Copernicus Value and similar). Any match puts the journal in the queue with the quote and the
page link. Pages are re-read after 30 days. `VENUE_INDEX_READ_PAGES=false` turns this off.
Up to `VENUE_INDEX_PAGE_WORKERS` (6) websites are read at the same time, but never two requests
at once to the same site, with `VENUE_INDEX_PAGE_DELAY_SECONDS` between journals on one site
(many journals share a publisher's site). Journals with the most concern points are read first.

Decisions (admin, Venue Index -> a journal):
- **Exclude**: choose criteria and evidence links; optionally block the publisher, which sends
  its other titles to the queue (never excludes them automatically). A linked live venue is
  excluded too, so authors never see it. Excluded journals are kept in the index (never deleted
  by scope or size changes), and Venue Discovery refuses to re-add them.
- **Keep**: the journal leaves the queue and only returns if a new kind of concern appears.
- **Restore**: reverses an exclusion. When the criteria behind an exclusion are no longer
  detected, the journal returns to the queue as "re-review suggested".

```
python manage.py import_venue_index --screen-only            # screen + read pages
python manage.py import_venue_index --screen-only --no-pages # catalogue signals only
```
Full and daily runs screen automatically after their Crossref/DOAJ checks.

## Reading the rules (build plan step 4, layer 2)

For the first field (`VENUE_INDEX_RULES_FIELD=information-systems`: Information Systems, MIS,
Information Systems and Management), journals that are not excluded, not waiting for an
exclusion decision and not yet live have their rules read from their own pages: the DOAJ
author-guidelines page when there is one, otherwise the homepage plus linked author and
submission pages. The Venue Discovery pipeline does the reading, so the same guarantees apply:
every rule must be backed by a quote found on the fetched page, invented limits are dropped,
and confidence is computed from the sources, never chosen by the AI.

The AI is local Ollama by default (`VENUE_INDEX_RULES_AI_PROVIDER=ollama`, model from
`VENUE_DISCOVERY_OLLAMA_MODEL`, a 7B to 8B model recommended). If Ollama cannot be reached the
run stops with that reason instead of failing every journal. `VENUE_INDEX_RULES_PER_RUN` (40)
is the per-run ceiling, together with the time limit.

Journals with a DOAJ author-guidelines page are read first (those pages are usually readable).
If one official URL cannot be read, the other (guidelines page or homepage) is tried.

Outcomes: **Rules ready** (quoted rules found), **Rules not found** (with the reason), pages
unreadable, or **Site blocks reading** (HTTP 401/403/429 or robots.txt). Failed reads are
retried after 30 days. Blocked sites are respected, never worked around: other journals on the
same site are skipped for the rest of the run, and blocked journals are retried after 90 days.
For those, enter the rules by hand in Venue Agents, wait for the editor to claim the venue
(step 9), or leave the journal as Listed only. Reading never publishes. In Venue Index ->
"Rules ready", an admin checks what was read (each rule with its quote and link) and clicks
**Publish**, which creates the live venue labelled "Checked from official pages" with those
rules. Excluding a published journal later hides the live venue again.

```
python manage.py import_venue_index --rules-only            # up to 40 journals
python manage.py import_venue_index --rules-only --limit 5  # a quick trial
```

## Local AI first, escalation on evidence (build plan step 6)

Rule extraction never uses a router model. The page validator already knows whether a result is
good enough, so it decides: the local model reads first; if the result has no rule quoted on the
page, or a required field (`VENUE_INDEX_REQUIRED_FIELDS`, default aims & scope and article types)
is missing, the local model gets one retry told exactly what failed (optionally a different model,
`VENUE_INDEX_RETRY_OLLAMA_MODEL`). Pages are fetched once for all attempts. By default that is the
end: rule reading uses local models only (`VENUE_INDEX_ESCALATE=off`), even when an Anthropic key is
configured. `auto` (Anthropic as a third attempt when `ANTHROPIC_API_KEY` is set) or `anthropic`
turns the cloud step on; it is capped per run (`VENUE_INDEX_ESCALATIONS_PER_RUN`, 10) and goes
through the AI budget; if the budget is reached, escalation stops for the run and reading continues
locally. Unchanged pages that were read well before are reused without any model call.

Every attempt is stored (`RulesAttempt`: stage, model, outcome, missing fields), and each stage has
its own AI usage operation (`index_rules_local`, `index_rules_retry`, `index_rules_cloud`), so the
cost of escalation is visible. Venue Index shows the escalation rate per field over 30 days: under
20 percent the local model pays for itself; above it, that field should move to the cloud.

## Freshness: cadence and stale-call suppression (build plan step 7)

| What | Re-checked | On failure |
|---|---|---|
| Catalogue (OpenAlex, Crossref, DOAJ) | Monthly | Keep, flag |
| Scope, article types, limits, required items | Every 90 days (`VENUE_INDEX_RULES_REFRESH_DAYS`), weekly schedule, local AI | Keep, show the age |
| Open calls for papers and their deadlines | Weekly per venue (daily schedule, `VENUE_CALLS_RECHECK_DAYS`=6), no AI | Hide |

Rule: an open call is shown to authors only while its deadline is ahead and, on journals Flexee
checked from official pages, only if it was re-confirmed on those pages within
`VENUE_CALLS_CONFIRM_DAYS` (10). Otherwise it is hidden, never shown with an old date. The same
filter applies to what the matching AI is told. Calls on editor-configured venues are the editor's
to maintain and are hidden once their deadline passes. Dated deadlines that have passed are not
shown either.

The re-read of a live journal never changes its live rules by itself: unchanged pages renew its
check date; changed pages put it under Venue Index -> "Pages changed", where an admin applies the
changes (a new rules version) or leaves the live rules, which keep their older check date.

```
python manage.py import_venue_index --calls-only      # re-confirm open calls now
python manage.py ensure_venue_index_schedule          # monthly, daily checks, daily calls, weekly rules
```

## The author-facing journal index (build plan step 8)

`/journals` (public, read-only) searches every venue an author may see plus the journals Flexee has
only listed so far: live venues first (editor-confirmed, then checked from official pages), then
listed journals by size. Search covers name, publisher, ISSN and subject; filters for tier and open
access. Every result and every journal page shows the tier badge and the check date. A live
journal's page (`/journals/v/<slug>`) shows its rules, its source pages and only live open calls; a
listed journal's page (`/journals/i/<id>`) shows catalogue facts and says plainly that its rules
were not read. Excluded journals and journals waiting for an exclusion decision never appear.
Match results link to each journal's page, and the match score's scope part now also counts the
embedding similarity (cosine 0.40 to 0.70 mapped to 0 to 100 percent; it never lowers the
word-based score). `import_venue_index --force` closes a run left "processing" by a restart.

The search and both journal pages share one rate limit per network: `JOURNAL_INDEX_REQUESTS_PER_WINDOW`
(600) per `JOURNAL_INDEX_WINDOW_SECONDS` (3600), counted with the same keyed network hash as claims
and sign-in, answering 429 with `Retry-After` when exceeded. Counters live in the database
(`PublicRequestWindow`, one row per network per window), so all gunicorn workers share them; rows
older than a day are removed by the hourly retention sweep. Behind nginx, `TRUSTED_PROXIES=127.0.0.1`
is needed or every visitor counts as one network; `verify_production_readiness` warns when it is empty.

## Topical shortlist for matching (build plan step 5)

Before any AI looks at a manuscript-venue pair, a local embedding model narrows the matchable
venues (claimed and verified_index) to the `VENUE_SHORTLIST_SIZE` (30) whose scope is closest to
the manuscript. Only those get the policy gate, a match record and, later, the AI fit explanation,
so matching cost no longer grows with the catalogue. No language model is involved and nothing
escalates.

The model is `VENUE_EMBED_MODEL=nomic-embed-text` through Ollama (`ollama pull nomic-embed-text`,
about 270 MB, runs on CPU). A venue's vector comes from its name, description, aims and scope,
article types, reviewer criteria and methods; a manuscript's from its title, keywords, abstract and
semantic profile. Vectors are stored and re-made only when that text or the model changes; a
manuscript's vector is deleted with its content. If Ollama or the model is unavailable, the
shortlist uses keyword overlap instead, so matching never stops.

Each match stores `topic_similarity` and `shortlist_rank`; the author can sort by closest topic.
Re-running matching replaces matches that left the shortlist. A venue published later is added to
an author's matches only if it makes that manuscript's shortlist.

```
python manage.py embed_venues --check   # does the embedding model answer?
python manage.py embed_venues           # pre-compute vectors for all matchable venues (optional)
```

## Claim this venue (build plan step 9)

Every journal page in the author index that is not editor-confirmed has "Claim this venue". The
editor gives their name, role, work email and (optionally) a page that lists them, then confirms
the email from a signed link (3 days). The claim then waits in Admin -> Venue Claims, which shows
whether the email is on the journal's own domain and whether others claim the same journal.

Approving it (platform admins only):
- a listed journal becomes a venue (still "Listed only") linked to its index record;
- the journal moves into its own organization ("<journal> editorial office") unless it already
  has one to itself, so the claim never reaches the publisher's other journals;
- the claimant gets an owner membership, with a new editor account if needed and an emailed link to
  set a password (7 days, works once); two-step sign-in is set up at the first sign-in.

The journal becomes "Editor-confirmed" only when its own editor (an owner, not a Flexee admin) saves
or activates its rules for the first time; until then it keeps its earlier tier. Rejections are
emailed to the claimant with the admin's note. Public claims are rate-limited
(`VENUE_CLAIMS_PER_HOUR`, `VENUE_CLAIMS_PER_EMAIL_DAY`).
