import os
from django.http import HttpResponse


class CorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.allowed = {x.strip() for x in os.getenv(
            'FRONTEND_ORIGINS',
            'http://localhost:5173,http://127.0.0.1:5173'
        ).split(',') if x.strip()}

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
            response['Access-Control-Allow-Headers'] = 'Content-Type, X-Requested-With'
            response['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        return response
