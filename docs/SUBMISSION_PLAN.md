# Manuscript versions and the submission plan

Implements instructions 2.4 and 2.5 of 8 October, following the draft spec
(`flexee_submission_plan_spec_DRAFT.md`). Where Vikram's own spec differs, his wins.

## Versions (2.4)

- `ManuscriptVersion` holds one version's file, hash, title/abstract/keywords/type snapshot, parsed
  profile and change note. `Manuscript.current_version` points at the version the Manuscript row
  (the working copy every pipeline reads) mirrors.
- Readiness assessments, venue matches and venue submissions point at the version they were made for.
  Rows created without one take the current version. Author screens show the current version's rows;
  submission work (brief, editor download and payload) uses the submission's own version.
- Existing manuscripts became version 1 (migration 0038), sharing their file.
- `POST /api/author/manuscripts/<id>/versions/` (file, change_note, optional title/abstract/keywords/
  manuscript_type/revised_after) makes version N+1. Earlier versions never change. Draft packets move
  to the new version and reset. Readiness and matching then run again for it.
- Editing in place is refused once the current version is submitted or an active plan was built on it.
- Retention removes an earlier version's file once none of its submissions is retained; a newer,
  unsent version is never purged. Earlier versions count toward storage.

## The plan (2.5)

- Order: how much the manuscript must change for each venue (`ready` < `edit` < `section` <
  `study`), then scope fit (strong before moderate, then closer similarity), then fewer and smaller
  changes. Nothing else: no ratings, metrics, tiers or acceptance data.
- Left out (with the reason): article type not accepted, venue rules incomplete, scope below
  `PLAN_MIN_FIT`, or beyond `PLAN_MAX_POSITIONS`.
- Gaps are stored structured on `VenueMatch.gap_items` (`review/gap_classes.py`); free-text gaps
  without an item count as an unclassified new section.
- Reasons are generated from the ordering key, never by an AI model.
- States: queued → preparing → submitted → under_review → accepted / revise_resubmit / declined /
  withdrawn. A decline becomes `awaiting_author`: nothing advances until the author answers
  `revised` (a newer version; the remaining venues are re-checked and re-ordered against it) or
  `unchanged` (a written reason and an explicit confirmation). One live venue at a time, in order
  (skip with a reason to move past one).
- Flexee venues update the plan themselves (author submit, editor start review, editor decision);
  outside venues are reported by the author.
- API: see `review/plan_api.py`. Admin support view: `GET /api/admin/plans/?manuscript=`.

| Setting | Default | |
|---|---|---|
| `MANUSCRIPT_MAX_VERSIONS` | 20 | versions per manuscript |
| `PLAN_MAX_POSITIONS` | 5 | venues in a plan (1 to 10) |
| `PLAN_MIN_FIT` | 0.40 | embedding similarity below this leaves a venue out |
| `PLAN_STRONG_FIT` | 0.60 | similarity at or above this is a strong scope match |
