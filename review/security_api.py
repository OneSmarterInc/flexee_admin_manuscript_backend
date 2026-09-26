from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.http import require_GET


@require_GET
def csrf_token(request):
    """Issue a CSRF cookie and return a matching token for browser clients."""
    return JsonResponse({'csrfToken': get_token(request)})
