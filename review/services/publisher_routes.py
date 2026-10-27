"""Guidelines addresses on publisher hosts that answer us, built from the homepage we already hold.

Two large publishers refuse our crawler on the host OpenAlex gives us but serve the same author
guidelines from a different host that answers normally. Measured on 27 October with our own client
and user agent:

    journals.sagepub.com          403
    www.sagepub.com/docs/...      200   <- same guidelines
    pubsonline.informs.org/page/  200   <- INFORMS guidelines

That covers 93 of the 849 ABDC A* and A journals: 82 on SAGE and 11 on INFORMS.

Neither address can be guessed from a journal's title, because both use a short internal code
(HUM for Human Relations, OSS for Organization Studies, opre for Operations Research). The code is
already in the homepage URL we hold, so it is read out of there rather than invented. A record whose
homepage does not carry a recognisable code gets no extra candidate, so this never guesses.

These are additional candidates tried before the homepage, not a replacement for it, and a site
that refuses us is still recorded as refusing us. Nothing here works around a refusal: both hosts
answer our ordinary crawler, and robots.txt is honoured by the fetcher as before.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# journals.sagepub.com/home/hum, /toc/hum/40/3, /journal/hum
_SAGE_HOSTS = ('journals.sagepub.com', 'sagepub.com', 'us.sagepub.com', 'uk.sagepub.com')
_SAGE_CODE = re.compile(r'^/(?:home|toc|journal|loi)/([a-z0-9]{2,8})(?:/|$)', re.IGNORECASE)

# pubsonline.informs.org/journal/opre, /toc/opre/73/1
_INFORMS_HOSTS = ('pubsonline.informs.org', 'informs.org')
_INFORMS_CODE = re.compile(r'^/(?:journal|toc|doi/abs|loi)/([a-z0-9]{2,8})(?:/|$)', re.IGNORECASE)


def _host(url):
    host = (urlsplit(url or '').hostname or '').lower()
    return host[4:] if host.startswith('www.') else host


def _code(url, pattern):
    match = pattern.match(urlsplit(url or '').path or '')
    return match.group(1) if match else ''


def candidates_for(record):
    """Extra guidelines URLs worth trying for this record, in order. [] when none apply.

    Takes anything with a homepage_url attribute (an IndexedVenue, or a stand-in in tests).
    """
    homepage = str(getattr(record, 'homepage_url', '') or '').strip()
    if not homepage:
        return []
    host = _host(homepage)
    urls = []
    if any(host == h or host.endswith('.' + h) for h in _SAGE_HOSTS):
        code = _code(homepage, _SAGE_CODE)
        if code:
            # Both cases are served; the upper-case form is the one SAGE's own links use.
            urls.append(f'https://www.sagepub.com/docs/default-source/msg/{code.upper()}')
            if code.lower() != code.upper():
                urls.append(f'https://www.sagepub.com/docs/default-source/msg/{code.lower()}')
    elif any(host == h or host.endswith('.' + h) for h in _INFORMS_HOSTS):
        code = _code(homepage, _INFORMS_CODE)
        if code:
            urls.append(f'https://pubsonline.informs.org/page/{code.lower()}/submission-guidelines')
    return urls
