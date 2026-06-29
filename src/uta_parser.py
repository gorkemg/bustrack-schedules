"""
UTA (Utah Transit Authority) schedule parser.

Reads GTFS routes.txt, fetches each route page concurrently, scrapes PDF links,
and emits /output/uta_schedules.json conforming to schema v1.0.
"""

from __future__ import annotations

import csv
import json
import logging
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES_FILE = REPO_ROOT / "data" / "uta" / "routes.txt"
OUTPUT_FILE = REPO_ROOT / "output" / "uta_schedules.json"

BASE_URL = "https://www.rideuta.com"
REQUEST_TIMEOUT = 20
MAX_WORKERS = 10

SESSION_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BusTrack-Schedules/1.0; "
        "+https://github.com/bustrack/schedules)"
    )
}


# ---------------------------------------------------------------------------
# Grouping heuristic
# ---------------------------------------------------------------------------

def _vehicle_type_group(short_name: str) -> str:
    """Return a human-readable vehicle-type group label for a UTA route."""
    s = short_name.upper()

    if s == "750":
        return "Commuter Rail (FrontRunner)"

    # TRAX light rail: 701 (Blue), 703 (Red), 704 (Green), 720 (S-Line)
    if s in {"701", "703", "704", "720"}:
        return "Light Rail (TRAX)"

    # BRT / express identifiers used by UTA (colour-coded routes)
    if s.endswith("X") or s in {"50X", "603X", "830X"}:
        return "Bus Rapid Transit (BRT)"

    # Flex (on-demand) routes are prefixed with F
    if s.startswith("F"):
        return "Flex (On-Demand)"

    return "Bus"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(SESSION_HEADERS)
    adapter = requests.adapters.HTTPAdapter(max_retries=2)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _fetch_page(session: requests.Session, url: str) -> str | None:
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        log.warning("Failed to fetch %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# PDF link extraction
# ---------------------------------------------------------------------------

def _extract_pdf_links(html: str, page_url: str) -> tuple[str | None, str | None]:
    """
    Return (schedule_pdf_url, map_pdf_url) from a route page's HTML.

    Strategy:
      1. Find all <a href="*.pdf"> tags (case-insensitive).
      2. Classify by whether the href/text contains 'schedule' or 'map'.
      3. Fall back to first/second PDF if keywords are absent.
    """
    soup = BeautifulSoup(html, "lxml")
    schedule_url: str | None = None
    map_url: str | None = None

    pdf_links: list[tuple[str, str]] = []  # (absolute_url, anchor_text)
    for tag in soup.find_all("a", href=True):
        href: str = tag["href"]
        if not href.lower().endswith(".pdf"):
            continue
        absolute = href if href.startswith("http") else urljoin(page_url, href)
        pdf_links.append((absolute, tag.get_text(" ", strip=True).lower()))

    for url, text in pdf_links:
        url_lower = url.lower()
        if schedule_url is None and ("schedule" in url_lower or "schedule" in text):
            schedule_url = url
        elif map_url is None and ("map" in url_lower or "map" in text):
            map_url = url

    # Fallback: assign positionally when keyword matching yields nothing
    if schedule_url is None and pdf_links:
        schedule_url = pdf_links[0][0]
    if map_url is None and len(pdf_links) > 1:
        map_url = pdf_links[1][0]

    return schedule_url, map_url


# ---------------------------------------------------------------------------
# Per-route processing (runs in thread pool)
# ---------------------------------------------------------------------------

def _process_route(
    session: requests.Session,
    route: dict[str, str],
) -> tuple[str, dict]:
    route_id = route["route_id"]
    url = route.get("route_url", "").strip()

    entry: dict = {
        "route_short_name": route.get("route_short_name", "").strip(),
        "route_long_name": route.get("route_long_name", "").strip(),
        "route_color": route.get("route_color", "").strip() or None,
        "schedule_pdf_url": None,
        "map_pdf_url": None,
    }

    if not url:
        log.warning("route_id=%s has no route_url — skipping HTTP fetch", route_id)
        return route_id, entry

    html = _fetch_page(session, url)
    if html is None:
        return route_id, entry

    entry["schedule_pdf_url"], entry["map_pdf_url"] = _extract_pdf_links(html, url)
    log.info(
        "route_id=%-6s short=%-5s  schedule=%s  map=%s",
        route_id,
        entry["route_short_name"],
        "✓" if entry["schedule_pdf_url"] else "✗",
        "✓" if entry["map_pdf_url"] else "✗",
    )
    return route_id, entry


# ---------------------------------------------------------------------------
# Groupings
# ---------------------------------------------------------------------------

def build_groupings(
    routes_meta: dict[str, dict[str, str]],
) -> dict[str, list[dict]]:
    """Produce the groupings block keyed by 'vehicle_type'."""
    bucket: dict[str, list[str]] = defaultdict(list)
    for route_id, meta in routes_meta.items():
        group = _vehicle_type_group(meta["route_short_name"])
        bucket[group].append(route_id)

    vehicle_type_order = [
        "Commuter Rail (FrontRunner)",
        "Light Rail (TRAX)",
        "Bus Rapid Transit (BRT)",
        "Flex (On-Demand)",
        "Bus",
    ]

    groups: list[dict] = []
    for label in vehicle_type_order:
        if label not in bucket:
            continue
        groups.append(
            {
                "group_id": label.lower()
                .replace(" ", "_")
                .replace("(", "")
                .replace(")", "")
                .replace("/", "_"),
                "name": label,
                "route_ids": sorted(bucket[label]),
            }
        )

    return {"vehicle_type": groups}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not ROUTES_FILE.exists():
        log.error("routes.txt not found at %s", ROUTES_FILE)
        sys.exit(1)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    with ROUTES_FILE.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        all_routes = list(reader)

    log.info("Loaded %d routes from %s", len(all_routes), ROUTES_FILE)

    routes_result: dict[str, dict] = {}
    routes_meta: dict[str, dict[str, str]] = {r["route_id"]: r for r in all_routes}

    session = _make_session()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_process_route, session, r): r["route_id"]
            for r in all_routes
        }
        for future in as_completed(futures):
            route_id, entry = future.result()
            routes_result[route_id] = entry

    output = {
        "agency_id": "UTA",
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "routes": routes_result,
        "groupings": build_groupings(routes_meta),
    }

    with OUTPUT_FILE.open("w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)

    log.info("Wrote %s (%d routes)", OUTPUT_FILE, len(routes_result))


if __name__ == "__main__":
    main()
