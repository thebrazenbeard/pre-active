from __future__ import annotations

import ipaddress
from typing import Any
from urllib import parse, request


def is_loopback_url(url: str) -> bool:
    """Identify local provider URLs without changing model-target admission."""
    try:
        parsed = parse.urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or host is None:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _RejectRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Returning None leaves the original HTTP status for the caller's
        # existing probe/provider error classification. No second request runs.
        return None


def open_local_request(req: request.Request, *, timeout: float) -> Any:
    """Keep an admitted local request on its exact endpoint.

    A dedicated opener ignores environment/system proxy configuration and the
    global urllib opener. All redirects are refused, including local aliases.
    """
    if not is_loopback_url(req.full_url):
        raise ValueError("local model request must use a loopback endpoint")
    opener = request.build_opener(request.ProxyHandler({}), _RejectRedirects())
    return opener.open(req, timeout=timeout)
