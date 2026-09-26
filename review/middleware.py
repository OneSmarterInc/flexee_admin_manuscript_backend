from django.http import HttpResponse

from .auth import allowed_frontend_origins


class CorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.allowed = allowed_frontend_origins()

    def __call__(self, request):
        origin = request.headers.get('Origin', '')
        if request.method == 'OPTIONS':
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)
        if origin and origin in self.allowed:
            response['Access-Control-Allow-Origin'] = origin
            response['Vary'] = 'Origin'
            response['Access-Control-Allow-Credentials'] = 'true'
            response['Access-Control-Allow-Headers'] = 'Content-Type, X-Requested-With, X-Manuscript-Token'
            response['Access-Control-Allow-Methods'] = 'GET, POST, PATCH, OPTIONS'
        return response
