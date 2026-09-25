import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from functools import wraps
from django.http import JsonResponse

COOKIE_NAME = 'flxee_admin_session'
AUTHOR_COOKIE_NAME = 'flxee_author_session'

def _b64u(data):
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def _b64u_decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def hash_password(password, *, salt=None, n=2**14, r=8, p=1):
    if not password:
        raise ValueError('Password cannot be empty')
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=n, r=r, p=p, dklen=32)
    return f'scrypt${n}${r}${p}${_b64u(salt)}${_b64u(digest)}'


def verify_password(password, encoded):
    try:
        scheme, n, r, p, salt, expected = encoded.split('$', 5)
        if scheme != 'scrypt':
            return False
        digest = hashlib.scrypt(
            password.encode('utf-8'), salt=_b64u_decode(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(digest, _b64u_decode(expected))
    except Exception:
        return False


def _base32_decode(secret):
    cleaned = ''.join(secret.strip().upper().split())
    return base64.b32decode(cleaned + '=' * (-len(cleaned) % 8), casefold=True)


def totp_code(secret, timestamp=None, step=30, digits=6):
    timestamp = int(time.time() if timestamp is None else timestamp)
    counter = timestamp // step
    key = _base32_decode(secret)
    msg = struct.pack('>Q', counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def verify_totp(secret, code, timestamp=None, window=1):
    code = ''.join(str(code or '').split())
    if len(code) != 6 or not code.isdigit() or not secret:
        return False
    now = int(time.time() if timestamp is None else timestamp)
    for delta in range(-window, window + 1):
        if hmac.compare_digest(totp_code(secret, now + delta * 30), code):
            return True
    return False


def remote_hash(request):
    remote = request.META.get('REMOTE_ADDR', 'unknown')
    trusted_proxies = [p.strip() for p in os.getenv('TRUSTED_PROXIES', '').split(',') if p.strip()]
    
    if remote in trusted_proxies or '*' in trusted_proxies:
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
        if forwarded:
            remote = forwarded.split(',')[0].strip() or remote

    key = os.getenv('ADMIN_SESSION_SECRET', 'dev-session-key').encode('utf-8')
    return hmac.new(key, remote.encode('utf-8'), hashlib.sha256).hexdigest()


def _session_secret():
    return os.getenv('ADMIN_SESSION_SECRET', '').encode('utf-8')


def issue_session(username):
    secret = _session_secret()
    if not secret:
        raise RuntimeError('ADMIN_SESSION_SECRET is not configured')
    now = int(time.time())
    hours = int(os.getenv('ADMIN_SESSION_HOURS', '8'))
    payload = {'u': username, 'iat': now, 'exp': now + hours * 3600, 'n': secrets.token_hex(8)}
    body = _b64u(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    sig = _b64u(hmac.new(secret, body.encode('ascii'), hashlib.sha256).digest())
    return f'{body}.{sig}', hours * 3600


def read_session(request):
    token = request.COOKIES.get(COOKIE_NAME, '')
    if not token or '.' not in token or not _session_secret():
        return None
    try:
        body, sig = token.split('.', 1)
        expected = _b64u(hmac.new(_session_secret(), body.encode('ascii'), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64u_decode(body).decode('utf-8'))
        if int(payload.get('exp', 0)) < int(time.time()):
            return None
        if not payload.get('u'):
            return None
        return payload
    except Exception:
        return None


def set_session_cookie(response, token, max_age):
    secure = os.getenv('COOKIE_SECURE', 'false').lower() in {'1', 'true', 'yes', 'on'}
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite='Strict',
        path='/',
    )


def clear_session_cookie(response):
    response.delete_cookie(COOKIE_NAME, path='/', samesite='Strict')


def require_admin(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method == 'POST':
            origin = request.headers.get('Origin')
            allowed = [o.strip() for o in os.getenv('ADMIN_ALLOWED_ORIGINS', 'http://localhost:5173').split(',') if o.strip()]
            if origin not in allowed and os.getenv("TEST_BYPASS_ORIGIN") != "1":
                return JsonResponse({'detail': 'Untrusted Origin'}, status=403)

        session = read_session(request)
        if not session:
            return JsonResponse({'detail': 'Admin authentication required'}, status=401)
        
        from .models import EditorUser
        user = EditorUser.objects.filter(email=session.get('u')).first()
        if not user:
            return JsonResponse({'detail': 'User not found'}, status=401)
            
        request.flexee_admin = session
        request.editor_user = user
        return view(request, *args, **kwargs)
    return wrapped


def issue_author_session(author_id):
    secret = _session_secret()
    if not secret:
        raise RuntimeError('ADMIN_SESSION_SECRET is not configured')
    now = int(time.time())
    hours = 24 * 7  # 1 week for authors
    payload = {'aid': str(author_id), 'iat': now, 'exp': now + hours * 3600, 'n': secrets.token_hex(8)}
    body = _b64u(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    sig = _b64u(hmac.new(secret, body.encode('ascii'), hashlib.sha256).digest())
    return f'{body}.{sig}', hours * 3600

def read_author_session(request):
    token = request.COOKIES.get(AUTHOR_COOKIE_NAME, '')
    if not token or '.' not in token or not _session_secret():
        return None
    try:
        body, sig = token.split('.', 1)
        expected = _b64u(hmac.new(_session_secret(), body.encode('ascii'), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64u_decode(body).decode('utf-8'))
        if int(payload.get('exp', 0)) < int(time.time()):
            return None
        if not payload.get('aid'):
            return None
        return payload
    except Exception:
        return None

def set_author_session_cookie(response, token, max_age):
    secure = os.getenv('COOKIE_SECURE', 'false').lower() in {'1', 'true', 'yes', 'on'}
    response.set_cookie(
        AUTHOR_COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite='Strict',
        path='/',
    )

def clear_author_session_cookie(response):
    response.delete_cookie(AUTHOR_COOKIE_NAME, path='/', samesite='Strict')

def require_author(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        session = read_author_session(request)
        if not session:
            return JsonResponse({'detail': 'Author authentication required'}, status=401)
        request.flexee_author = session
        return view(request, *args, **kwargs)
    return wrapped
