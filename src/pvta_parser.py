"""
PVTA (Pioneer Valley Transit Authority) schedule parser.

Scrapes https://www.pvta.com/schedules.php, extracts PDF/image links per route,
and emits /output/pvta_schedules.json conforming to schema v1.0.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "output" / "pvta_schedules.json"

PVTA_SCHEDULES_URL = "https://www.pvta.com/schedules.php"
PVTA_BASE = "https://www.pvta.com/"
REQUEST_TIMEOUT = 30

SESSION_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BusTrack-Schedules/1.0; "
        "+https://github.com/bustrack/schedules)"
    )
}

# ---------------------------------------------------------------------------
# Static region grouping
# ---------------------------------------------------------------------------

REGION_MAP: dict[str, str] = {
    # Springfield Area
    "Loop":   "Springfield Area",
    "G1":     "Springfield Area",
    "G2":     "Springfield Area",
    "G2E":    "Springfield Area",
    "G3":     "Springfield Area",
    "B4":     "Springfield Area",
    "G5":     "Springfield Area",
    "B6":     "Springfield Area",
    "B7":     "Springfield Area",
    "B7S":    "Springfield Area",
    "OWL":    "Springfield Area",
    "R10S":   "Springfield Area",
    "R10":    "Springfield Area",
    "P11":    "Springfield Area",
    "B12":    "Springfield Area",
    "R14":    "Springfield Area",
    "B17":    "Springfield Area",
    "P20":    "Springfield Area",
    "P20E":   "Springfield Area",
    "P21":    "Springfield Area",
    "P21E":   "Springfield Area",
    "B23":    "Springfield Area",
    "R24":    "Springfield Area",
    "R29":    "Springfield Area",
    "X90":    "Springfield Area",
    "X92":    "Springfield Area",
    # Northampton Area
    "Rt39E":       "Northampton Area",
    "R41":         "Northampton Area",
    "R42":         "Northampton Area",
    "B43":         "Northampton Area",
    "B43no_school": "Northampton Area",
    "R44":         "Northampton Area",
    "B48":         "Northampton Area",
    "Rt39":        "Northampton Area",
    "NE":          "Northampton Area",
    "NSC":         "Northampton Area",
    # Amherst & Surrounding
    "Rt30": "Amherst & Surrounding",
    "Rt31": "Amherst & Surrounding",
    "Rt33": "Amherst & Surrounding",
    "Rt34": "Amherst & Surrounding",
    "Rt35": "Amherst & Surrounding",
    "Rt36": "Amherst & Surrounding",
    "Rt38": "Amherst & Surrounding",
    "Rt45": "Amherst & Surrounding",
    "Rt46": "Amherst & Surrounding",
    "WP":   "Amherst & Surrounding",
    "B79":  "Amherst & Surrounding",
}

REGION_ORDER = ["Springfield Area", "Northampton Area", "Amherst & Surrounding"]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch(url: str) -> str | None:
    session = requests.Session()
    session.headers.update(SESSION_HEADERS)
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        log.error("Failed to fetch %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# DOM parsing
# ---------------------------------------------------------------------------

_PDF_OR_IMG_RE = re.compile(r"\.(pdf|jpg|jpeg|png)$", re.IGNORECASE)


def _absolute(href: str) -> str:
    """Ensure href is absolute against the PVTA base URL."""
    if href.startswith("http"):
        return href
    return urljoin(PVTA_BASE, href)


def _classify_link(href: str, text: str) -> tuple[bool, bool]:
    """Return (is_schedule, is_map) based on href and anchor text heuristics."""
    combined = (href + " " + text).lower()
    is_schedule = "schedule" in combined or "timetable" in combined
    is_map = "map" in combined or "system" in combined
    return is_schedule, is_map


def parse_schedules_page(html: str) -> dict[str, dict]:
    """
    Parse the PVTA schedules page and return a dict keyed by route_id:
      {
        "route_short_name": str,
        "route_long_name": str,
        "schedule_pdf_url": str | None,
        "map_pdf_url": str | None,
      }

    PVTA's schedules.php groups routes in a table or definition-list structure.
    We apply a broad sweep: find all anchors that look like route containers,
    then extract PDF/image hrefs from their neighbourhood.
    """
    soup = BeautifulSoup(html, "lxml")
    routes: dict[str, dict] = {}

    # PVTA renders schedule rows as <tr> elements where the first cell contains
    # the route number/name and subsequent cells contain PDF links.
    # We look for any <tr> that contains a cell with a route-like identifier.
    route_id_re = re.compile(
        r"^(Loop|OWL|NSC|NE|WP|[A-Z]\d+\w*|Rt\d+\w*|[A-Z]{1,3}\d*\w*)$",
        re.IGNORECASE,
    )

    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        first_cell_text = cells[0].get_text(" ", strip=True)
        # Strip "Route" prefix if present (e.g. "Route B43" → "B43")
        first_cell_text = re.sub(r"(?i)^route\s*", "", first_cell_text).strip()

        if not route_id_re.match(first_cell_text):
            continue

        route_id = first_cell_text
        # Route long name often lives in cell[1] before any links
        long_name_cell = cells[1] if len(cells) > 1 else cells[0]
        long_name = long_name_cell.get_text(" ", strip=True)

        schedule_url: str | None = None
        map_url: str | None = None

        for cell in cells:
            for a_tag in cell.find_all("a", href=True):
                href: str = a_tag["href"]
                if not _PDF_OR_IMG_RE.search(href):
                    continue
                abs_url = _absolute(href)
                text = a_tag.get_text(" ", strip=True)
                is_sched, is_map = _classify_link(href, text)

                if schedule_url is None and (is_sched or (not is_map)):
                    schedule_url = abs_url
                elif map_url is None and is_map:
                    map_url = abs_url

        if schedule_url is None and map_url is None:
            # No PDF found in this row — skip; likely a header row
            continue

        routes[route_id] = {
            "route_short_name": route_id,
            "route_long_name": long_name,
            "schedule_pdf_url": schedule_url,
            "map_pdf_url": map_url,
        }
        log.info(
            "route=%-12s schedule=%s  map=%s",
            route_id,
            "✓" if schedule_url else "✗",
            "✓" if map_url else "✗",
        )

    if not routes:
        log.warning(
            "Table-based parse yielded 0 routes — falling back to link sweep."
        )
        routes = _link_sweep_fallback(soup)

    return routes


def _link_sweep_fallback(soup: BeautifulSoup) -> dict[str, dict]:
    """
    Fallback parser: finds PDF links and attempts to infer route IDs from
    the href path segments (e.g. /g_pdfs/B43_schedule.pdf → B43).
    """
    routes: dict[str, dict] = {}
    route_from_href = re.compile(
        r"/([A-Za-z]{0,3}\d+\w*)_?(schedule|map|timetable)?",
        re.IGNORECASE,
    )

    for a_tag in soup.find_all("a", href=True):
        href: str = a_tag["href"]
        if not _PDF_OR_IMG_RE.search(href):
            continue
        match = route_from_href.search(href)
        if not match:
            continue
        route_id = match.group(1)
        abs_url = _absolute(href)
        text = a_tag.get_text(" ", strip=True)
        is_sched, is_map = _classify_link(href, text)

        entry = routes.setdefault(
            route_id,
            {
                "route_short_name": route_id,
                "route_long_name": "",
                "schedule_pdf_url": None,
                "map_pdf_url": None,
            },
        )
        if is_map and entry["map_pdf_url"] is None:
            entry["map_pdf_url"] = abs_url
        elif entry["schedule_pdf_url"] is None:
            entry["schedule_pdf_url"] = abs_url

    log.info("Fallback sweep found %d routes", len(routes))
    return routes


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def build_groupings(route_ids: list[str]) -> dict[str, list[dict]]:
    bucket: dict[str, list[str]] = defaultdict(list)
    for rid in route_ids:
        region = REGION_MAP.get(rid, "Other")
        bucket[region].append(rid)

    all_regions = REGION_ORDER + sorted(
        r for r in bucket if r not in REGION_ORDER and r != "Other"
    )
    if "Other" in bucket:
        all_regions.append("Other")

    groups: list[dict] = []
    for region in all_regions:
        if region not in bucket:
            continue
        groups.append(
            {
                "group_id": region.lower()
                .replace(" ", "_")
                .replace("&", "and")
                .replace("(", "")
                .replace(")", ""),
                "name": region,
                "route_ids": sorted(bucket[region]),
            }
        )

    return {"region": sorted(groups, key=lambda g: g["name"])}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    log.info("Fetching %s", PVTA_SCHEDULES_URL)
    html = _fetch(PVTA_SCHEDULES_URL)
    if html is None:
        log.error("Could not retrieve PVTA schedules page — aborting.")
        sys.exit(1)

    routes = parse_schedules_page(html)

    if not routes:
        log.error("No routes parsed from PVTA page — aborting to avoid empty output.")
        sys.exit(1)

    output = {
        "agency_id": "PVTA",
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "routes": routes,
        "groupings": build_groupings(list(routes.keys())),
    }

    with OUTPUT_FILE.open("w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)

    log.info("Wrote %s (%d routes)", OUTPUT_FILE, len(routes))


if __name__ == "__main__":
    main()
