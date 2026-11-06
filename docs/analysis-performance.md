# Analysis performance changes

Venue matching caches successful model responses on the version-specific VenueMatch row. The cache key includes the complete prompt, manuscript hash/version, journal configuration identity/version and provider/model settings. Policy gates and evidence grounding still run on each match request. Failures are not cached. Removing a version clears its cache; full manuscript retention deletes match rows.

Short Ollama requests use context buckets of 4096/8192/16384 tokens up to the configured OLLAMA_NUM_CTX ceiling, with twice the estimated input tokens plus output and framing headroom. No prompt text is removed. Set OLLAMA_ADAPTIVE_CONTEXT=false to restore fixed context allocation. Long inputs retain the configured ceiling.

Apply migrations and restart the Django API and main qcluster after installing these changes. The separate detector queue does not perform readiness/matching. Existing results populate the matching cache on the first new analysis, not retroactively. Readiness already resumes saved sections; its coverage and per-attempt limits remain unchanged. No complete-review latency improvement is claimed until measured with the same document and checking coverage.


## Automatic readiness recovery

Semantic readiness now keeps the same ReviewJob across up to three automatic retries.
Incomplete coverage and transient exceptions schedule another attempt after 30, 60,
and 120 seconds (plus scheduler/queue latency). Completed checkpoint sections are reused.
After three retries, manual Resume remains available; a long document may still need it.
Invalid input, changed versions and purged manuscripts are not automatically retried.
The active job is returned with readiness results so reopening the page reconnects to it.
Only readiness polling uses the extended three-hour client limit; server work continues
when the browser closes. Automatic recovery requires the main qcluster and scheduler.

The stuck-job sweep runs every five minutes. Hard worker crashes are recovered only
after the existing processing timeout (30 minutes by default), not immediately.
Apply migration 0049 and restart Django and the main qcluster after deployment.
