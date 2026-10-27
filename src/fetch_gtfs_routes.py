"""Download a bounded HTTPS GTFS archive and extract only its root routes.txt."""
import argparse
import io
import stat
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

MAX_ARCHIVE = 50 * 1024 * 1024
MAX_ROUTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5


def download(url, session):
    for _ in range(MAX_REDIRECTS + 1):
        if urlsplit(url).scheme.lower() != "https":
            raise ValueError("GTFS URLs and redirects must use HTTPS")
        with session.get(url, stream=True, allow_redirects=False, timeout=(10, 60)) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers["Location"])
                continue
            response.raise_for_status()
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_ARCHIVE:
                raise ValueError("GTFS archive exceeds size limit")
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
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
        if stat.S_ISLNK(entry.external_attr >> 16) or entry.is_dir():
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
    with requests.Session() as session:
        routes = extract_routes(download(args.url, session))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(routes)


if __name__ == "__main__":
    main()
