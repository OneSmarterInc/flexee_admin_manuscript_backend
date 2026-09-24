import re

with open(r'd:\flexee\flexee_admin_manuscript_backend\review\editor_api.py', 'r', encoding='utf-8') as f:
    content = f.read()

helper = """
def _check_org_access(user, organization_id, required_roles=None):
    if user.platform_superuser:
        return True
    if not required_roles:
        required_roles = ['owner', 'editor', 'viewer']
    return user.memberships.filter(
        organization_id=organization_id,
        role__in=required_roles
    ).exists()
"""
content = content.replace("DECISION_STATUSES = {'revision_requested', 'accepted', 'rejected'}", "DECISION_STATUSES = {'revision_requested', 'accepted', 'rejected'}\n" + helper)

# admin_venue_detail
content = re.sub(
    r"(\s*except Venue.DoesNotExist:\n\s*return JsonResponse\(\{'detail': 'Venue not found'\}, status=404\))",
    r"\1\n\n    required_roles = ['owner', 'editor', 'viewer'] if request.method == 'GET' else ['owner']\n    if not _check_org_access(request.editor_user, venue.organization_id, required_roles):\n        return JsonResponse({'detail': 'Forbidden'}, status=403)",
    content
)

# admin_venue_configs
content = re.sub(
    r"(def admin_venue_configs.*?except Venue.DoesNotExist:\n\s*return JsonResponse\(\{'detail': 'Venue not found'\}, status=404\))",
    r"\1\n    if not _check_org_access(request.editor_user, venue.organization_id, ['owner', 'editor', 'viewer']):\n        return JsonResponse({'detail': 'Forbidden'}, status=403)",
    content,
    flags=re.DOTALL
)

# admin_activate_venue_config
content = re.sub(
    r"(def admin_activate_venue_config.*?except VenueAgentConfig.DoesNotExist:\n\s*return JsonResponse\(\{'detail': 'Venue configuration not found'\}, status=404\))",
    r"\1\n\n    if not _check_org_access(request.editor_user, venue.organization_id, ['owner']):\n        return JsonResponse({'detail': 'Forbidden'}, status=403)",
    content,
    flags=re.DOTALL
)

# admin_venue_submissions
content = re.sub(
    r"(def admin_venue_submissions\(request\):\n\s*queryset = VenueSubmission.objects.select_related\(\n\s*'manuscript', 'venue', 'venue__organization', 'venue_config'\n\s*\).all\(\))",
    r"\1\n\n    user = request.editor_user\n    if not user.platform_superuser:\n        org_ids = user.memberships.values_list('organization_id', flat=True)\n        queryset = queryset.filter(venue__organization_id__in=org_ids)",
    content
)
content = re.sub(
    r"(counts_base = VenueSubmission.objects.all\(\))",
    r"\1\n    if not user.platform_superuser:\n        counts_base = counts_base.filter(venue__organization_id__in=org_ids)",
    content
)

# admin_venue_submission_detail
content = re.sub(
    r"(def admin_venue_submission_detail.*?except VenueSubmission.DoesNotExist:\n\s*return JsonResponse\(\{'detail': 'Venue submission not found'\}, status=404\))",
    r"\1\n    if not _check_org_access(request.editor_user, item.venue.organization_id, ['owner', 'editor', 'viewer']):\n        return JsonResponse({'detail': 'Forbidden'}, status=403)",
    content,
    flags=re.DOTALL
)

# admin_start_venue_review
content = re.sub(
    r"(def admin_start_venue_review.*?item = VenueSubmission.objects.get\(id=submission_id\))",
    r"def admin_start_venue_review(request, submission_id):\n    try:\n        item = VenueSubmission.objects.select_related('venue').get(id=submission_id)",
    content,
    flags=re.DOTALL
)
content = re.sub(
    r"(def admin_start_venue_review.*?except VenueSubmission.DoesNotExist:\n\s*return JsonResponse\(\{'detail': 'Venue submission not found'\}, status=404\))",
    r"\1\n\n    if not _check_org_access(request.editor_user, item.venue.organization_id, ['owner', 'editor']):\n        return JsonResponse({'detail': 'Forbidden'}, status=403)",
    content,
    flags=re.DOTALL
)

# admin_venue_submission_decision
content = re.sub(
    r"(def admin_venue_submission_decision.*?except VenueSubmission.DoesNotExist:\n\s*return JsonResponse\(\{'detail': 'Venue submission not found'\}, status=404\))",
    r"\1\n\n    if not _check_org_access(request.editor_user, item.venue.organization_id, ['owner', 'editor']):\n        return JsonResponse({'detail': 'Forbidden'}, status=403)",
    content,
    flags=re.DOTALL
)

# admin_venue_submission_download
content = re.sub(
    r"(def admin_venue_submission_download.*?item = VenueSubmission.objects.select_related\('manuscript'\).get\(id=submission_id\))",
    r"def admin_venue_submission_download(request, submission_id):\n    try:\n        item = VenueSubmission.objects.select_related('manuscript', 'venue').get(id=submission_id)",
    content,
    flags=re.DOTALL
)
content = re.sub(
    r"(def admin_venue_submission_download.*?except VenueSubmission.DoesNotExist:\n\s*return JsonResponse\(\{'detail': 'Venue submission not found'\}, status=404\))",
    r"\1\n\n    if not _check_org_access(request.editor_user, item.venue.organization_id, ['owner', 'editor', 'viewer']):\n        return JsonResponse({'detail': 'Forbidden'}, status=403)",
    content,
    flags=re.DOTALL
)

with open(r'd:\flexee\flexee_admin_manuscript_backend\review\editor_api.py', 'w', encoding='utf-8') as f:
    f.write(content)
