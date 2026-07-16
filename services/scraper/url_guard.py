"""
url_guard.py — SSRF gate for user-supplied scrape targets.

This service fetches arbitrary URLs from inside the docker network, where
Postgres, Redis, RabbitMQ, Mongo, and every other service answer without
auth — so a scrape target of http://usage:8000/ or http://169.254.169.254/
(cloud metadata, on the AWS deployment target) would turn the scraper into a
proxy into the private network. validate_url() rejects the obvious cases
STATICALLY (no DNS lookup — validation must never touch the network, and the
test suite runs offline):

  - only http/https (no file:, ftp:, javascript:, gopher:, ...)
  - no credentials in the URL (http://user:pass@host smuggling)
  - literal IPs must be globally routable (ipaddress.is_global rejects
    loopback, RFC1918 private, link-local/metadata 169.254.*, CGNAT,
    reserved, and their IPv6 equivalents), including legacy dotted forms
    like 0x7f.0.0.1 that inet_aton accepts
  - hostname must not be localhost / *.localhost / *.local / *.internal /
    *.lan / *.home.arpa, and must contain a dot — dotless names
    (http://intranet, http://mongo) only resolve on private networks, and
    the rule also catches decimal-IP forms like http://2130706433

KNOWN LIMIT (documented, deliberate): a public DNS name that RESOLVES to a
private IP (DNS rebinding) passes this static check — pinning the resolved
address at fetch time is an engine-level hardening step for later.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2000

BLOCKED_HOSTS = ("localhost",)
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


class InvalidTargetURL(ValueError):
    """The URL is malformed, non-http(s), or points at a non-public target."""


def validate_url(url: str) -> str:
    """Return the URL unchanged if it is an acceptable scrape target; raise
    InvalidTargetURL with the reason otherwise."""
    if not url or not url.strip():
        raise InvalidTargetURL("url is required")
    if len(url) > MAX_URL_LENGTH:
        raise InvalidTargetURL(f"url longer than {MAX_URL_LENGTH} characters")

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise InvalidTargetURL(f"malformed url: {exc}") from exc

    if parts.scheme not in ("http", "https"):
        raise InvalidTargetURL("only http and https URLs can be scraped")
    if parts.username or parts.password:
        raise InvalidTargetURL("credentials in the URL are not allowed")

    try:
        host = parts.hostname
    except ValueError as exc:  # e.g. invalid IPv6 literal
        raise InvalidTargetURL(f"malformed host: {exc}") from exc
    if not host:
        raise InvalidTargetURL("url has no host")
    host = host.rstrip(".")  # "localhost." is still localhost

    if host in BLOCKED_HOSTS or any(host.endswith(s) for s in BLOCKED_HOST_SUFFIXES):
        raise InvalidTargetURL(f"host {host!r} is not a public target")

    ip = _literal_ip(host)
    if ip is not None:
        if not ip.is_global:
            raise InvalidTargetURL(
                f"IP {ip} is not globally routable (private/loopback/link-local/reserved)")
    elif "." not in host:
        raise InvalidTargetURL(f"host {host!r} is not a public DNS name")

    return url


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address `host` denotes, if it is a literal IP — including the
    legacy dotted-hex/octal IPv4 spellings inet_aton accepts (no DNS ever)."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(host))
    except OSError:
        return None
