# Manuscript versions and the submission plan

Implements instructions 2.4 and 2.5 of 8 October, following Vikram's submission plan spec of 8 October.

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
- Editing in place is refused once the current version is submitted or a live plan was built on it.
- Retention removes an earlier version's file once none of its submissions is retained; a newer,
  unsent version is never purged. Earlier versions count toward storage.

## The plan (2.5), as in Vikram's spec of 8 October

- Models: `SubmissionPlan` (`manuscript_version`, `status` draft / active / paused / completed /
  abandoned, `position`, `generated_by`, `previous_plan`) and `SubmissionPlanStep` (`order`, `venue`,
  `venue_match`, `venue_submission`, `state` queued / current / submitted / accepted / declined /
  skipped / withdrawn, `rationale`, `required_changes`, `author_moved`, `outcome_note`). Every change
  is logged in `PlanEvent`. `ManuscriptVersion.supersedes` points at the version a revision replaced.
- Order, three bands never mixed: (1) eligible with no changes, (2) eligible with changes, (3) needs
  changes. Inside a band: compliance distance ascending, then fit (the match's topic similarity).
- Compliance distance is a count of changes weighted by kind, from one constant
  (`COMPLIANCE_WEIGHTS` in `review/gap_classes.py`): light 1 (length, formatting, a declaration),
  medium 3 (a missing section, added content), heavy 9 (method or study). The venue's required
  submission items (a cover letter, a data statement) are listed with the step as things to prepare,
  but are not changes to the manuscript and do not move a venue out of band 1.
- A breached desk rule marks the match `ineligible` for the text as it stands. For the plan, a breach
  the author can fix by revising is `needs changes`; only a gap revising cannot fix (article type not
  accepted, venue rules not configured) or a scope below `PLAN_MIN_FIT` leaves the venue out. Venues
  left out are listed under the plan with the reason, as are venues past `PLAN_MAX_STEPS`.
- Only venues an author may see and whose rules were read (`Venue.objects.matchable()`, the
  `RULE_TIERS`) are planned: never an excluded or `listed`-tier venue.
- Rationales are generated from the ordering key, never by an AI model, and never mention odds,
  ratings or tiers.
- Generation runs in the job queue: `POST .../plan/generate/` returns 202 and a job id; the result is
  a draft plan. Activating makes step 1 current. One venue at a time.
- Outcome accepted completes the plan. Declined or withdrawn pauses it: nothing advances until the
  author answers. `unchanged` makes the next step current. `revised` needs a newer version with its
  readiness check and matching done; the plan is then regenerated in the job queue against it.
- A regenerated plan never proposes a venue that declined this manuscript, nor one the author skipped
  or removed in the previous plan. If the author reordered the previous plan, their order is kept
  for the venues still in it (`author_moved`), and new venues follow in our order.
- Flexee venues update the plan themselves (author submit, editor decision); outside venues are
  reported by the author (`steps/<id>/submitted/`, `steps/<id>/outcome/`).
- API: see the docstring in `review/plan_api.py`. Admin support view: `GET /api/admin/plans/?manuscript=`.
- Migration 0040 maps earlier plans: preparing becomes current, under review and revise-and-resubmit
  become submitted, a decline waiting for an answer becomes declined with the plan paused, stopped
  plans become abandoned.

| Setting | Default | |
|---|---|---|
| `MANUSCRIPT_MAX_VERSIONS` | 20 | versions per manuscript |
| `PLAN_MAX_STEPS` | 8 | venues in a plan (1 to 20); `PLAN_MAX_POSITIONS` is read if unset |
| `PLAN_MIN_FIT` | 0.40 | embedding similarity below this leaves a venue out |
| `PLAN_STRONG_FIT` | 0.60 | similarity at or above this is a strong scope match |
