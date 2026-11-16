from __future__ import annotations

import hashlib
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit, unquote_plus

TRACKING = {"fbclid", "gclid", "msclkid"}


def canonicalize(url: str) -> str:
    p = urlsplit(url.strip())
    if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Only absolute HTTP(S) URLs without embedded credentials are accepted")
    host = p.hostname.encode("idna").decode().lower().rstrip(".")
    port = p.port
    host = f"[{host}]" if ":" in host else host
    if port and (p.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    # Preserve parameter order, repeated values and meaningful parameters.
    parts = [part for part in p.query.split("&")
             if not unquote_plus(part.split("=", 1)[0]).lower().startswith("utm_")
             and unquote_plus(part.split("=", 1)[0]).lower() not in TRACKING]
    return urlunsplit((p.scheme.lower(), host, p.path or "/", "&".join(parts), ""))


def fingerprint(url: str) -> str:
    return hashlib.sha256(canonicalize(url).encode()).hexdigest()


def host_of(url: str) -> str:
    return urlsplit(url).hostname.lower()


def validate_target(url: str, allowed_domains: list[str], allow_private=False) -> str:
    url = canonicalize(url)
    host = host_of(url)
    if host not in allowed_domains:
        raise ValueError(f"Domain is outside the plan: {host}")
    if not allow_private:
        addresses = socket.getaddrinfo(host, urlsplit(url).port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError("Private, loopback and non-public addresses are not allowed")
    return url
