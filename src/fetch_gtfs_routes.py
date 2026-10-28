"""Download a bounded HTTPS GTFS archive and extract only its root routes.txt."""
import argparse
import io
import stat
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from urllib3.exceptions import HTTPError as Urllib3Error

try:
    from .security_utils import make_session, resolve_document_url, valid_document_url
except ImportError:
    from security_utils import make_session, resolve_document_url, valid_document_url

MAX_ARCHIVE = 50 * 1024 * 1024
MAX_ROUTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
TOTAL_SECONDS = 120
CONNECT_SECONDS = 10
READ_SECONDS = 10


def download(url, session):
    deadline = time.monotonic() + TOTAL_SECONDS
    for _ in range(MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("GTFS download deadline exceeded")
        if not valid_document_url(url) or urlsplit(url).scheme.lower() != "https":
            raise ValueError("GTFS URLs and redirects must be credential-free HTTPS")
        with session.get(url, stream=True, allow_redirects=False,
                         timeout=(min(CONNECT_SECONDS, remaining), min(READ_SECONDS, remaining))) as response:
            if response.is_redirect:
                url = resolve_document_url(url, response.headers.get("Location"))
                if not url:
                    raise ValueError("Invalid GTFS redirect location")
                continue
            response.raise_for_status()
            if not 200 <= response.status_code < 300:
                raise ValueError("Unexpected GTFS response status")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_ARCHIVE):
                raise ValueError("Invalid or oversized GTFS Content-Length")
            data = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise ValueError("GTFS download deadline exceeded")
                try:
                    chunk = response.raw.read1(65536, decode_content=True)
                except Urllib3Error as exc:
                    raise ValueError(f"GTFS response read failed: {exc}") from exc
                if time.monotonic() >= deadline:
                    raise ValueError("GTFS download deadline exceeded")
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_ARCHIVE:
                    raise ValueError("GTFS archive exceeds size limit")
            return bytes(data)
    raise ValueError("Too many GTFS redirects")


def extract_routes(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        matches = [entry for entry in archive.infolist() if entry.filename == "routes.txt"]
        if len(matches) != 1:
            raise ValueError("Archive must contain exactly one root routes.txt")
        entry = matches[0]
        if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ValueError("Unsupported routes.txt compression method")
        if stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG) or entry.is_dir():
            raise ValueError("routes.txt must be a regular file")
        if entry.file_size > MAX_ROUTES:
            raise ValueError("routes.txt exceeds size limit")
        with archive.open(entry) as source:
            routes = source.read(MAX_ROUTES + 1)
        if len(routes) > MAX_ROUTES:
            raise ValueError("routes.txt exceeds size limit")
        return routes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with make_session({}) as session:
        routes = extract_routes(download(args.url, session))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(routes)


if __name__ == "__main__":
    main()
