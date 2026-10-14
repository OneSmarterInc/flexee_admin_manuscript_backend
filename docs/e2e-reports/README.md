# Production E2E reports

Four `production_e2e` runs on the AWS host (https://manuscriptbackend.flexee.org): both AI providers, one article and one book each. Every run covers the full path with real SMTP: author registration, upload, readiness, matching, venue assessment, packet submission, editor review and a human editor decision (revision requested). All four passed with the AI used at every AI stage (no deterministic fallback).

| Report | Provider / model | Manuscript | Run (UTC) | Total time | AI calls | AI cost |
|---|---|---|---|---|---|---|
| shared_qwen_article.json | shared_qwen / qwen2.5-shared | research article | 2026-10-09 17:42 | 403.7 s | 4 | $0 (local) |
| shared_qwen_book.json | shared_qwen / qwen2.5-shared | book | 2026-10-12 10:20 | 248.9 s | 4 | $0 (local) |
| anthropic_article.json | anthropic / claude-haiku-5-5 | research article | 2026-10-12 13:06 | 36.0 s | 4 | $0.003437 |
| anthropic_book.json | anthropic / claude-haiku-5-5 | book | 2026-10-12 13:08 | 26.8 s | 4 | $0.003045 |

## Time per stage (seconds)

The AI stages account for almost all of the time; every other stage takes under 5 seconds.

| Stage | Qwen article | Qwen book | Claude article | Claude book |
|---|---|---|---|---|
| Semantic readiness (worker) | 236.5 | 110.2 | 14.0 | 9.0 |
| Semantic matching (worker) | 68.1 | 45.1 | 6.0 | 5.0 |
| Venue assessment (worker) | 92.2 | 85.2 | 7.0 | 8.0 |
| Deterministic matching | 2.6 | 4.4 | 4.6 | 1.0 |
| Packet submission | 0.6 | 0.6 | 0.6 | 0.6 |
| Human editor decision | 0.6 | 0.6 | 0.6 | 0.5 |
| All other stages together | 2.9 | 2.6 | 2.9 | 2.5 |
| Total wall-clock | 403.7 | 248.9 | 36.0 | 26.8 |

The total is wall-clock time for the whole run, so it is 0.2 to 0.3 s more than the sum of the stages. Worker times are measured by polling, so they are rounded up to the poll interval (about 1 second).

## AI cost per manuscript

| Operation | Claude article tokens | Claude article cost | Claude book tokens | Claude book cost |
|---|---|---|---|---|
| Semantic readiness (2 calls) | 7,049 | $0.001713 | 5,132 | $0.001124 |
| Semantic matching (1 call) | 3,015 | $0.000676 | 2,850 | $0.000681 |
| Venue assessment (1 call) | 4,812 | $0.001048 | 5,060 | $0.001240 |
| Total | 14,876 | $0.003437 | 13,042 | $0.003045 |

The Qwen runs used 6,690 tokens (article) and 5,061 tokens (book) on the shared Qwen worker, which has no per-call price. The `unpriced_cloud_calls: 4` in those two files is a counting error fixed later (review 2, X14: local providers are no longer counted as unpriced cloud calls).

## Observations

1. Claude Haiku is about 11 times faster per manuscript (36 s against 404 s for the article) for about a third of a cent.
2. The Qwen answers are much thinner: its fit summaries restate the venue type ("The Manuscript Type is Acceptable by the Venue.") with few gaps, where Claude names specific gaps (for example the article's "illustrative firms" limitation and missing disclosures).
3. The runs used the code of 9 and 12 October. Fixes merged since then (reviews 2 and 3) change how long manuscripts are split for the shared Qwen worker and how the costs are counted; they do not change the stages. Re-run on the current code if exact current timings are needed.
4. Both books were checked against Field Notes Journal, which lists "Book manuscript" among its types.

Earlier `ollama` runs (9 October) are not included: the local model was not reachable then, so every AI stage used the deterministic fallback and the reports show no AI use.
