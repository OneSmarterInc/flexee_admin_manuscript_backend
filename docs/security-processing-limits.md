# Analysis security limits

Normal author uploads, matching and automatic readiness recovery retain their existing flow.

- Author-triggered analysis requests share per-account limits: 30 per minute and 120 per hour. Configure AUTHOR_ANALYSIS_PER_MINUTE and AUTHOR_ANALYSIS_PER_HOUR if needed. Excess requests return HTTP 429 and Retry-After. Polling and background retries do not consume this allowance.
- PDF/DOCX extraction runs in an isolated Python subprocess with a 60-second deadline, 1536 MiB memory ceiling, 1000 PDF-page ceiling and 8 million extracted-character ceiling. Existing upload/ZIP validation still applies. Limit violations produce a readable extraction error; documents are never silently truncated.
- Configurable bounded settings: DOCUMENT_PARSE_TIMEOUT_SECONDS (1–120), DOCUMENT_MEMORY_MB (256–4096), DOCUMENT_MAX_PAGES (1–5000), DOCUMENT_MAX_TEXT_CHARS (1000–16000000).
- Windows uses a process-memory Job Object; Linux uses RLIMIT_AS. Deployments must permit subprocesses and memory-limit enforcement. This is resource isolation, not an OS sandbox for arbitrary code.
- Readiness checkpoint and completion writes lock the manuscript and refuse writes after retention removal or manuscript replacement.
- Author reset and editor invitation consumption run under row locks. Production must use a database supporting SELECT FOR UPDATE (PostgreSQL); SQLite does not provide equivalent row locking.

Restart Django and background workers after deployment. No schema migration is required for these changes. Keep the detector environment package installer updated separately from its pinned inference dependencies.
