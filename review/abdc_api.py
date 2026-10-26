"""Private, read-only catalogue of imported ABDC rating data."""
from django.db.models import Count, Q
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .auth import require_platform_superuser
from .models import ABDCJournal


@require_GET
@require_platform_superuser
def abdc_list(request):
    try:
        edition = int(request.GET.get('edition', '2025'))
        page = max(1, int(request.GET.get('page', '1')))
    except (ValueError, TypeError):
        return JsonResponse({'detail': 'Invalid edition or page'}, status=400)
    qs = ABDCJournal.objects.filter(edition=edition)
    base = qs
    rating = request.GET.get('rating', '')
    status = request.GET.get('status', '')
    q = request.GET.get('q', '').strip()[:100]
    if rating:
        if rating not in ('A*', 'A', 'B', 'C'):
            return JsonResponse({'detail': 'Unknown ABDC rating'}, status=400)
        qs = qs.filter(rating=rating)
    if status:
        if status not in ('matched', 'pending_review', 'unmatched'):
            return JsonResponse({'detail': 'Unknown match status'}, status=400)
        qs = qs.filter(match_status=status)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(publisher__icontains=q) |
                       Q(issn__icontains=q) | Q(issn_online__icontains=q))
    total = qs.count()
    page_size = 40
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    ratings = {x['rating']: x['n'] for x in base.values('rating').annotate(n=Count('id'))}
    statuses = {x['match_status']: x['n'] for x in base.values('match_status').annotate(n=Count('id'))}
    rows = qs.select_related('indexed_venue').order_by('title', 'source_row')[(page - 1) * page_size:page * page_size]
    return JsonResponse({
        'edition': edition,
        'counts': {'total': base.count(), 'ratings': ratings, 'statuses': statuses},
        'pagination': {'page': page, 'pages': pages, 'page_size': page_size, 'total': total},
        'items': [{
            'id': item.id, 'title': item.title, 'publisher': item.publisher,
            'issn': item.issn, 'issn_online': item.issn_online,
            'year_inception': item.year_inception, 'for_code': item.for_code,
            'rating': item.rating, 'edition': item.edition,
            'status': item.match_status, 'method': item.match_method,
            'candidates': item.candidates, 'source_row': item.source_row,
            'source_url': item.source_url,
            'indexed_venue': ({'id': str(item.indexed_venue_id), 'title': item.indexed_venue.title}
                              if item.indexed_venue_id else None),
        } for item in rows],
    })
