"""Bounded HTTPS page retrieval and validation of published document URLs."""
import time
import re
import ipaddress
from urllib.parse import urljoin, urlsplit


MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 5
TOTAL_SECONDS = 30
READ_SECONDS = 10
CONNECT_SECONDS = 5


def valid_document_url(url):
    if not isinstance(url, str) or not url or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", url, re.I):
        return False
    try:
        if re.search(r"%(?![0-9a-fA-F]{2})", url):
            return False
        parts = urlsplit(url)
        host = parts.hostname
        if parts.scheme.lower() not in {"http", "https"} or not host or parts.username is not None or parts.password is not None or parts.port == 0:
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            host = host.encode("idna").decode("ascii").rstrip(".")
            if len(host) > 253 or not all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label) for label in host.split(".")):
                return False
        return True
    except (ValueError, UnicodeError):
        return False


def resolve_document_url(base, href):
    if not isinstance(href, str) or any(ord(c) <= 32 or ord(c) == 127 for c in href) or "\\" in href:
        return None
    try:
        result = urljoin(base, href)
    except ValueError:
        return None
    return result if valid_document_url(result) else None


def make_session(headers):
    import requests
    session = requests.Session()
    session.trust_env = False  # Do not load proxies, .netrc or ambient CA overrides.
    session.headers.update(headers)
    return session


def fetch_html(session, url):
    from urllib3.exceptions import HTTPError as Urllib3Error
    deadline = time.monotonic() + TOTAL_SECONDS
    for _ in range(MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("HTML download deadline exceeded")
        if not valid_document_url(url) or urlsplit(url).scheme.lower() != "https":
            raise ValueError("HTML URL and redirects must be credential-free HTTPS")
        with session.get(url, stream=True, allow_redirects=False,
                         timeout=(min(CONNECT_SECONDS, remaining), min(READ_SECONDS, remaining))) as response:
            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("Redirect missing Location")
                url = resolve_document_url(url, location)
                if not url:
                    raise ValueError("Invalid redirect location")
                continue
            response.raise_for_status()
            if not 200 <= response.status_code < 300:
                raise ValueError("Unexpected HTML response status")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_HTML_BYTES):
                raise ValueError("Invalid or oversized HTML Content-Length")
            data = bytearray()
            # read1 yields available data without waiting to fill an entire buffer;
            # decode_content caps decompressed as well as wire-size responses.
            while True:
                if time.monotonic() >= deadline:
                    raise ValueError("HTML download deadline exceeded")
                try:
                    chunk = response.raw.read1(16384, decode_content=True)
                except Urllib3Error as exc:
                    raise ValueError(f"HTML response read failed: {exc}") from exc
                if time.monotonic() >= deadline:
                    raise ValueError("HTML download deadline exceeded")
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_HTML_BYTES:
                    raise ValueError("HTML response exceeds size limit")
            return bytes(data).decode(response.encoding or "utf-8", errors="replace")
    raise ValueError("Too many HTML redirects")
