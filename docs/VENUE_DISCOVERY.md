# Daily Venue Discovery

Admin > **Venue Discovery** lists journals, book publishers and conferences found on the
public web, verified against their official author/submission pages. Nothing is visible to
authors until a platform superuser clicks **Add to Venue Agent**, which creates the live
`Venue` and an active `VenueAgentConfig` (v1) in one transaction.

## 1. Enable it

Discovery runs as a **Claude agent** by default: Claude decides what to search for, which
results look official, and which pages to open, using Anthropic's server-side
`web_search` and `web_fetch` tools. Anthropic performs the browsing, so this server never
fetches arbitrary URLs in this mode. Only one key is needed.

In the backend `.env`:

```
VENUE_DISCOVERY_ENABLED=true
VENUE_DISCOVERY_MODE=claude_agent
VENUE_DISCOVERY_MODEL=claude-sonnet-5-5
ANTHROPIC_API_KEY=<your Anthropic API key>
```

Web search must be enabled for your organization in the Claude Console (it is on unless an
administrator turned it off). Costs: Anthropic bills about $10 per 1,000 web searches plus
normal tokens. Each agent request reserves AI budget first (operation
`venue_discovery_agent` / `venue_discovery_recheck`), so the existing daily and monthly AI
cost limits stop discovery before they are exceeded. Searches and fetches per request are
capped by `VENUE_DISCOVERY_MAX_SEARCHES_PER_REQUEST` and `VENUE_DISCOVERY_MAX_FETCHES_PER_REQUEST`;
what to look for is set by `VENUE_DISCOVERY_CATEGORIES` and `VENUE_DISCOVERY_FOCUS`.

Claude's answer is never trusted on its own: every venue is re-checked against the page
text that `web_fetch` actually returned. Quotes that are not on the fetched official page
are dropped, "accepting"/"closed" needs that evidence, and only objective limits become
automatic rules. Venues reported without fetched official pages are stored as "unclear"
with low confidence.

Alternative (`VENUE_DISCOVERY_MODE=search_api`): fixed queries through a search API
(`VENUE_SEARCH_PROVIDER=tavily`, `VENUE_SEARCH_API_KEY`) and this server's own safe fetcher,
with the AI used only to extract rules.

## 1b. Free setup: OpenAlex + DOAJ (+ SearXNG) + local Ollama (no API keys)

Recommended sources without Docker: `VENUE_SEARCH_PROVIDER=openalex,doaj`. OpenAlex is a free
global catalogue of journals (most-cited first, every major publisher); DOAJ adds open-access
journals. Each run takes candidates from the sources in turn, with at most
`VENUE_DISCOVERY_MAX_PER_COUNTRY` venues per country and `VENUE_DISCOVERY_MAX_PER_SITE` per
website, and directory sources rotate their results page daily.

Status and accepted types are also read directly from the official pages using exact,
standard phrases ("Make a Submission", "Submit your manuscript", "Call for papers",
"submissions are closed", "original research article", "book proposal", ...). The matching
sentence is stored as evidence, so venues can be confirmed even when a small local model
returns little.

### SearXNG + DOAJ + local Ollama

This mode costs nothing per run. Venues are found with **DOAJ** (Directory of Open Access
Journals, a free public API; journals only, and it links each journal's author instructions)
and **SearXNG** (a free search engine you run yourself). This server fetches the official
pages safely and a **local Ollama model** reads them. Every claim is checked against the
pages exactly as in the other modes.

1. Ollama: `ollama pull qwen2.5:0.5b-instruct` (testing) or `ollama pull qwen2.5:7b-instruct`
   (recommended for real results; needs about 8 GB RAM).
2. SearXNG with Docker, from any folder:

   ```
   docker run -d --name searxng -p 8888:8080 -v "${PWD}/searxng:/etc/searxng" searxng/searxng
   ```

   It creates `searxng/settings.yml` in that folder on first start. Edit it so that it has

   ```yaml
   search:
     formats:
       - html
       - json
   server:
     limiter: false
   ```

   then `docker restart searxng`. Test it: open
   `http://127.0.0.1:8888/search?q=journal+author+guidelines&format=json` in a browser; you
   should see JSON, not an error page.
3. Backend `.env`:

   ```
   VENUE_DISCOVERY_ENABLED=true
   VENUE_DISCOVERY_MODE=search_api
   VENUE_SEARCH_PROVIDER=searxng,doaj
   VENUE_SEARXNG_URL=http://127.0.0.1:8888
   VENUE_DISCOVERY_AI_PROVIDER=ollama
   VENUE_DISCOVERY_OLLAMA_MODEL=qwen2.5:0.5b-instruct
   VENUE_DISCOVERY_MAX_CANDIDATES_PER_RUN=5
   ```

   DOAJ searches use the subject areas in `VENUE_DISCOVERY_FOCUS` (comma-separated).

What to expect from `qwen2.5:0.5b-instruct`: search and page fetching work fully, but the
model reads rules poorly, so many venues come back "Unclear" with thin details. When the model
returns unusable output for a DOAJ journal, the journal is still staged as "Unclear" using
DOAJ's own data (name, publisher, subjects, peer review, APC). If Ollama itself is not
reachable, nothing is staged and the run lists the error. Switch to `qwen2.5:7b-instruct`
(one `.env` line) for useful results.

## 1c. More venues per run, open calls for papers

* Temporary failures (address lookup, HTTP 429/5xx, timeouts) are retried once
  (`VENUE_DISCOVERY_RETRY_DELAY_SECONDS`). 401/403 and robots.txt refusals are not retried.
* When a catalogue journal's own site refuses automated reading (403, robots.txt), it is skipped
  and listed under the run's skip reasons. Set `VENUE_DISCOVERY_KEEP_BLOCKED=true` to stage such
  journals from catalogue data as unverified "Unclear" venues instead.
* The admin page opens on "Verified only": venues whose status (accepting or closed) is proven by
  a quote from their own official page.
* Remove unverified venues already staged (dry run first, then `--yes`):

  ```
  python manage.py cleanup_unverified_discoveries
  python manage.py cleanup_unverified_discoveries --yes
  python manage.py cleanup_unverified_discoveries --yes --all-unclear   # also every New 'unclear' venue
  ```

  Only venues still in New and not added to Venue Agents are removed.
* Each venue can use up to `VENUE_DISCOVERY_MAX_PAGES_PER_CANDIDATE` (default 5) pages, including
  up to `VENUE_DISCOVERY_MAX_CFP_PAGES` (default 2) special-issue / call-for-papers pages.
* Open calls are read from official pages: a special-issue or call-for-papers heading with a
  deadline date. Only future deadlines are kept, in `current_demand.calls_for_papers` and
  `deadlines`, each with the exact text as evidence. An open call also confirms "Accepting".
  The admin page can show "Open calls only".

## 1d. Run length and stopping a run

* A run stops taking new venues after `VENUE_DISCOVERY_MAX_RUN_MINUTES` (default 25), which keeps
  it inside the worker's 30-minute job timeout. Venues not reached are tried by the next run.
* A run is never resumed after a worker restart or timeout: it is marked "Interrupted" and its
  results so far are kept. Stuck "processing" runs are closed automatically.
* "Stop run" on the admin page stops the current run before the next venue.
* While running, the status strip shows progress ("Checking venue 7 of 40").
* For thinking models such as `qwen3:1.7b`, discovery turns thinking off
  (`VENUE_DISCOVERY_OLLAMA_THINK=false`), which is much faster.

## 2. Schedule it (once per environment, idempotent)

```
python manage.py migrate
python manage.py ensure_venue_discovery_schedule --hour 2
```

This keeps exactly one Django-Q schedule (`flexee-venue-discovery-daily`). Re-running it
updates the same schedule; `--remove` deletes it.

## 3. Run the worker

Scheduled and manual runs execute in `qcluster` (`python manage.py qcluster`, or the
`flexee-qcluster` service in production). The web app never crawls during a request.

## 4. Run it manually

Click **Run discovery now** on the Venue Discovery page. It enqueues the same task the
schedule uses and returns immediately; the status strip refreshes while it runs.

## 5. Check the last run

The status strip shows the last run (status, new/updated/changed counts, pages checked,
skipped items). From a shell:

```
python manage.py shell -c "from review.models import VenueDiscoveryRun as R; r=R.objects.first(); print(r.status, r.summary, r.errors[:5])"
```

## 6. When something is missing

| Situation | Result |
|---|---|
| `VENUE_DISCOVERY_ENABLED` is not true | "Run discovery now" answers 409; scheduled runs are recorded as failed with "disabled". No network calls. |
| No `ANTHROPIC_API_KEY` (agent mode) or search API key (search_api mode) | The run is recorded as failed with a clear message. Nothing else is affected. |
| AI cost limit reached | Discovery stops for that run and records it; nothing else is affected. |
| A page, search query or AI call fails | That item is skipped and listed in the run's errors; the run continues. |

## Safety

* In search_api mode, only `http`/`https` URLs on public addresses are fetched (localhost, private, link-local,
  CGNAT and cloud-metadata addresses are blocked, including after redirects); responses are
  size- and time-limited; only HTML/text is read; robots.txt is respected; requests are
  throttled per domain and identify themselves with `VENUE_DISCOVERY_USER_AGENT`.
* Status "accepting"/"closed" requires a verified quote from an official page; otherwise
  the venue is "unclear". Quotes that do not appear on the page are dropped.
* Only objective, verified limits (word count, reference count, named sections) become
  automatic desk-rejection rules.
* Re-checks never change a live Venue Agent; differences mark the record "Changed".
* No manuscript or account data is ever sent to the search provider or AI.
