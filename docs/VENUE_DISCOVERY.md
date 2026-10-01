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

## 1b. Free setup: SearXNG + DOAJ + local Ollama (no API keys)

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
