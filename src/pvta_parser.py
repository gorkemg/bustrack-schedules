"""
PVTA (Pioneer Valley Transit Authority) schedule parser.

Primary data source : GTFS routes.txt  →  data/pvta/routes.txt
Secondary sources   : bus_service/schedules.php  →  schedule_pdf_url per route
                      bus_service/maps.php       →  map_pdf_url per route

The two web pages are scraped independently and merged with GTFS metadata.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES_FILE = REPO_ROOT / "data" / "pvta" / "routes.txt"
OUTPUT_FILE = REPO_ROOT / "output" / "pvta_schedules.json"

PVTA_SCHEDULES_URL = "https://www.pvta.com/bus_service/schedules.php"
PVTA_MAPS_URL = "https://www.pvta.com/bus_service/maps.php"
PVTA_BASE = "https://www.pvta.com/"
REQUEST_TIMEOUT = 30
MIN_ROUTES_SANITY = 30

SESSION_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BusTrack-Schedules/1.0; "
        "+https://github.com/bustrack/schedules)"
    )
}

# ---------------------------------------------------------------------------
# Static region grouping
# Keys use *normalized* route_short_name (no Rt/Route prefix).
# Rt30 → 30, Rt39E → 39E, B43 → B43 (unchanged), Loop → Loop (unchanged).
# ---------------------------------------------------------------------------

REGION_SPRINGFIELD = "Springfield Area"
REGION_NORTHAMPTON = "Northampton Area"
REGION_AMHERST = "Amherst & Surrounding"

# Values may be a single region (str) or, for connector routes PVTA itself
# cross-lists under two region headers on schedules.php, a list of regions.
# A route in 2 regions means it appears in both groups' route_ids arrays —
# valid per schema v1.0 (route_ids has no uniqueness/exclusivity constraint).
REGION_MAP: dict[str, str | list[str]] = {
    # Springfield Area
    "Loop":  REGION_SPRINGFIELD,
    "G1":    REGION_SPRINGFIELD,
    "G2":    REGION_SPRINGFIELD,
    "G2E":   REGION_SPRINGFIELD,
    "G3":    REGION_SPRINGFIELD,
    "G19":   REGION_SPRINGFIELD,
    "G73E":  REGION_SPRINGFIELD,
    "B4":    REGION_SPRINGFIELD,
    "G5":    REGION_SPRINGFIELD,
    "B6":    REGION_SPRINGFIELD,
    "B7":    REGION_SPRINGFIELD,
    "B7S":   REGION_SPRINGFIELD,
    "OWL":   REGION_SPRINGFIELD,
    "R10S":  REGION_SPRINGFIELD,
    "R10":   REGION_SPRINGFIELD,
    "P11":   REGION_SPRINGFIELD,
    "B12":   REGION_SPRINGFIELD,
    "R14":   REGION_SPRINGFIELD,
    "R22":   REGION_SPRINGFIELD,
    "B17":   REGION_SPRINGFIELD,
    "P20":   REGION_SPRINGFIELD,
    "P20E":  REGION_SPRINGFIELD,
    "P21":   REGION_SPRINGFIELD,
    "P21E":  REGION_SPRINGFIELD,
    "B23":   REGION_SPRINGFIELD,
    "R24":   REGION_SPRINGFIELD,
    "X90":   REGION_SPRINGFIELD,
    "X92":   REGION_SPRINGFIELD,
    "X94":   REGION_SPRINGFIELD,
    # Northampton Area  (Rt39 / Rt39E normalized → 39 / 39E)
    "39E":          REGION_NORTHAMPTON,
    "R42":          REGION_NORTHAMPTON,
    "B43":          REGION_NORTHAMPTON,
    "B43no_school": REGION_NORTHAMPTON,
    "R44":          REGION_NORTHAMPTON,
    "39":           REGION_NORTHAMPTON,
    "NE":           REGION_NORTHAMPTON,
    "NSC":          REGION_NORTHAMPTON,
    "904":          REGION_NORTHAMPTON,
    # Amherst & Surrounding  (Rt30–Rt46 normalized → 30–46)
    "30":    REGION_AMHERST,
    "31":    REGION_AMHERST,
    "33":    REGION_AMHERST,
    "34":    REGION_AMHERST,
    "35":    REGION_AMHERST,
    "36":    REGION_AMHERST,
    "38":    REGION_AMHERST,
    "38NS":  REGION_AMHERST,
    "45":    REGION_AMHERST,
    "46":    REGION_AMHERST,
    "WP":    REGION_AMHERST,
    "B79":   REGION_AMHERST,
    "PG":    REGION_AMHERST,
    "PS":    REGION_AMHERST,
    "WS":    REGION_AMHERST,
    # Connector routes PVTA cross-lists under two region headers on schedules.php.
    "B48": [REGION_SPRINGFIELD, REGION_NORTHAMPTON],
    "B49": [REGION_SPRINGFIELD, REGION_NORTHAMPTON],
    "G47": [REGION_SPRINGFIELD, REGION_NORTHAMPTON],
    "R41": [REGION_SPRINGFIELD, REGION_NORTHAMPTON],
    "R29": [REGION_SPRINGFIELD, REGION_AMHERST],
}

REGION_ORDER = ["Springfield Area", "Northampton Area", "Amherst & Surrounding"]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_route_id(raw_id: str) -> str:
    """
    Strip leading 'Rt' or 'Route' prefix so web-scraped IDs align with GTFS
    route_short_name values.

      Rt31   → 31
      Rt39E  → 39E
      B43    → B43   (unchanged — no Rt/Route prefix)
      Loop   → Loop  (unchanged)
    """
    cleaned = re.sub(r"(?i)^(route|rt)\s*", "", raw_id.strip())
    return cleaned if cleaned else raw_id


# ---------------------------------------------------------------------------
# GTFS loader
# ---------------------------------------------------------------------------

def load_gtfs_routes(path: Path) -> dict[str, dict[str, str | None]]:
    """
    Parse GTFS routes.txt and return a dict keyed by route_short_name:
      { "B43": { "route_long_name": "...", "route_color": "004a97" }, ... }
    """
    gtfs: dict[str, dict[str, str | None]] = {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            short = row.get("route_short_name", "").strip()
            if not short:
                continue
            gtfs[short] = {
                "route_long_name": row.get("route_long_name", "").strip(),
                "route_color": row.get("route_color", "").strip() or None,
            }
    log.info("Loaded %d routes from GTFS %s", len(gtfs), path)
    return gtfs


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


def _fetch(session: requests.Session, url: str) -> str | None:
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        log.error("Failed to fetch %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# DOM helpers shared by both page parsers
# ---------------------------------------------------------------------------

# Matches PDF, JPEG, PNG asset hrefs, optionally followed by a cache-busting
# query string (as emitted by PVTA's Revize document center).
_ASSET_RE = re.compile(r"\.(pdf|jpg|jpeg|png)(?:$|[?#])", re.IGNORECASE)

# Matches raw (pre-normalization) route identifiers as they appear in table cells
_ROUTE_ID_RE = re.compile(
    r"^(Loop|OWL|NSC|NE|WP|[A-Z]\d+\w*|Rt\d+\w*|[A-Z]{1,3}\d*\w*|\d+\w*)$",
    re.IGNORECASE,
)


def _absolute(href: str, source_url: str = PVTA_BASE) -> str:
    if href.startswith("http"):
        return href
    return urljoin(source_url, href)


def _page_base_url(soup: BeautifulSoup, fallback_url: str) -> str:
    """Return the HTML base URL, falling back to the fetched page URL."""
    base_tag = soup.find("base", href=True)
    return urljoin(fallback_url, base_tag["href"]) if base_tag else fallback_url


def _first_asset_in_cells(cells, source_url: str = PVTA_BASE) -> str | None:
    """Return the first PDF/image URL found across the given table cells."""
    for cell in cells:
        for a_tag in cell.find_all("a", href=True):
            href: str = a_tag["href"]
            if _ASSET_RE.search(href):
                return _absolute(href, source_url)
    return None


# ---------------------------------------------------------------------------
# Generic table-based asset-page parser
# ---------------------------------------------------------------------------

def _parse_asset_table(
    html: str, page_label: str, source_url: str = PVTA_BASE
) -> dict[str, str]:
    """
    Parse a PVTA asset page (schedules or maps) whose rows follow the pattern:
      <tr>
        <td>RouteID or Rt##</td>
        <td>Route Name</td>
        <td><a href="file.pdf">...</a></td>
      </tr>

    Returns dict[normalized_route_id → asset_url].
    """
    soup = BeautifulSoup(html, "lxml")
    result: dict[str, str] = {}

    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        # Strip "Route" prefix that appears on some pages ("Route B43" → "B43")
        # Note: "Rt" prefix is intentionally left for normalize_route_id to handle.
        raw_id = re.sub(r"(?i)^route\s+", "", cells[0].get_text(" ", strip=True)).strip()
        if not _ROUTE_ID_RE.match(raw_id):
            continue

        asset_url = _first_asset_in_cells(cells, source_url)
        if asset_url is None:
            continue

        norm_id = normalize_route_id(raw_id)
        if norm_id not in result:
            result[norm_id] = asset_url
            log.info("%-20s %-8s → %s", page_label, norm_id, asset_url)

    if not result:
        log.warning("%s: table parse yielded 0 routes — trying link sweep", page_label)
        result = _link_sweep_fallback(soup, page_label, source_url)

    return result


def _link_sweep_fallback(
    soup: BeautifulSoup, label: str, source_url: str = PVTA_BASE
) -> dict[str, str]:
    """
    Fallback: infer route IDs from PDF href path segments.
    e.g. /g_pdfs/B43_schedule.pdf → B43
    """
    result: dict[str, str] = {}
    route_from_href = re.compile(r"/([A-Za-z]{0,3}\d+\w*)[_\-.]", re.IGNORECASE)

    for a_tag in soup.find_all("a", href=True):
        href: str = a_tag["href"]
        if not _ASSET_RE.search(href):
            continue
        m = route_from_href.search(href)
        if not m:
            continue
        norm_id = normalize_route_id(m.group(1))
        if norm_id not in result:
            result[norm_id] = _absolute(href, source_url)

    log.info("%s: fallback sweep found %d routes", label, len(result))
    return result


# ---------------------------------------------------------------------------
# Public page parsers
# ---------------------------------------------------------------------------

_DOCUMENT_CENTER_ALIASES = {
    "palmer shuttle": ["PS"],
    "ware shuttle": ["WS"],
}


def _document_center_route_ids(label: str, href: str) -> list[str]:
    """Infer one or more route IDs from a Revize Document Center entry."""
    normalized_label = " ".join(label.split())
    alias = _DOCUMENT_CENTER_ALIASES.get(normalized_label.casefold())
    if alias:
        return alias

    filename = Path(urlsplit(href).path).stem
    filename_id = normalize_route_id(filename)
    # A filename such as Rt34_35 is a shared map, not a route named 34_35.
    if _ROUTE_ID_RE.match(filename_id) and not (
        "/" in normalized_label.split(" ", 1)[0] and "_" in filename_id
    ):
        return [filename_id]

    # Route-map labels can describe a shared asset, e.g. "34/35".
    label_ids = [
        normalize_route_id(value)
        for value in re.split(r"\s*/\s*", normalized_label.split(" ", 1)[0])
    ]
    return [route_id for route_id in label_ids if _ROUTE_ID_RE.match(route_id)]


def _parse_document_center_assets(
    html: str, page_label: str, source_url: str
) -> dict[str, str]:
    """Parse route assets from PVTA's current Revize Document Center layout."""
    soup = BeautifulSoup(html, "lxml")
    asset_base_url = _page_base_url(soup, source_url)
    result: dict[str, str] = {}

    for anchor in soup.select("#document-center .file-group a[href]"):
        href: str = anchor["href"]
        if not _ASSET_RE.search(href):
            continue
        asset_url = _absolute(href, asset_base_url)
        for route_id in _document_center_route_ids(
            anchor.get_text(" ", strip=True), href
        ):
            if route_id not in result:
                result[route_id] = asset_url
                log.info("%-20s %-8s → %s", page_label, route_id, asset_url)

    if result:
        log.info("%s: found %d document-center assets", page_label, len(result))
    return result

def parse_schedules_page(html: str) -> dict[str, str]:
    """Schedules page → dict[normalized_route_id → schedule_pdf_url]."""
    document_center_links = _parse_document_center_assets(
        html, "schedules", PVTA_SCHEDULES_URL
    )
    if document_center_links:
        return document_center_links
    soup = BeautifulSoup(html, "lxml")
    return _parse_asset_table(
        html, "schedules", _page_base_url(soup, PVTA_SCHEDULES_URL)
    )


def parse_maps_page(html: str) -> dict[str, str]:
    """Maps page → dict[normalized_route_id → map_image_url].

    The current page is a Revize Document Center. The legacy fallback expects
    embedded images in the form:
      <img src="maps/{RouteID}.jpg">
    e.g. maps/Rt30.jpg → route 30, maps/B43.jpg → route B43.

    We scrape <img src> attributes under the maps/ path and normalize
    the filename stem via normalize_route_id (strips Rt/Route prefix).
    """
    document_center_links = _parse_document_center_assets(html, "maps", PVTA_MAPS_URL)
    if document_center_links:
        return document_center_links

    soup = BeautifulSoup(html, "lxml")
    asset_base_url = _page_base_url(soup, PVTA_MAPS_URL)
    result: dict[str, str] = {}
    _img_re = re.compile(r"^maps/(.+)\.(jpg|jpeg|png)$", re.IGNORECASE)

    for img_tag in soup.find_all("img", src=True):
        src: str = img_tag["src"]
        m = _img_re.match(src)
        if not m:
            continue
        norm_id = normalize_route_id(m.group(1))
        if norm_id not in result:
            result[norm_id] = _absolute(src, asset_base_url)
            log.info("maps            %-8s → %s", norm_id, _absolute(src, asset_base_url))

    log.info("maps: found %d map URLs", len(result))
    return result


# ---------------------------------------------------------------------------
# Groupings
# ---------------------------------------------------------------------------

def build_groupings(route_ids: list[str]) -> dict[str, list[dict]]:
    bucket: dict[str, list[str]] = defaultdict(list)
    for rid in route_ids:
        regions = REGION_MAP.get(rid, "Other")
        if isinstance(regions, str):
            regions = [regions]
        for region in regions:
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

    if not ROUTES_FILE.exists():
        log.error("GTFS routes.txt not found at %s — run the GTFS download step first.", ROUTES_FILE)
        sys.exit(1)

    gtfs = load_gtfs_routes(ROUTES_FILE)

    session = _make_session()

    log.info("Fetching %s", PVTA_SCHEDULES_URL)
    schedules_html = _fetch(session, PVTA_SCHEDULES_URL)
    if schedules_html is None:
        log.error("Could not retrieve %s — aborting.", PVTA_SCHEDULES_URL)
        sys.exit(1)

    log.info("Fetching %s", PVTA_MAPS_URL)
    maps_html = _fetch(session, PVTA_MAPS_URL)
    if maps_html is None:
        log.warning("Could not retrieve %s — map URLs will be null.", PVTA_MAPS_URL)

    schedule_links = parse_schedules_page(schedules_html)
    map_links = parse_maps_page(maps_html) if maps_html else {}

    # GTFS is the authoritative list of valid routes. Web scraping enriches
    # each route with PDF links. Routes found only on the web (not in GTFS) are
    # included with a warning — they may be seasonal/special services.
    all_ids: set[str] = set(gtfs.keys()) | set(schedule_links.keys()) | set(map_links.keys())

    routes: dict[str, dict] = {}
    for route_id in sorted(all_ids):
        gtfs_meta = gtfs.get(route_id, {})
        routes[route_id] = {
            "route_short_name": route_id,
            "route_long_name": gtfs_meta.get("route_long_name", ""),
            "route_color": gtfs_meta.get("route_color"),
            "schedule_pdf_url": schedule_links.get(route_id),
            "map_pdf_url": map_links.get(route_id),
        }
        if route_id not in gtfs:
            log.warning("route_id=%s found on web pages but absent from GTFS", route_id)

    # Sanity check: a suspiciously small result set indicates a scraping failure.
    if len(routes) < MIN_ROUTES_SANITY:
        log.error(
            "Sanity check failed: only %d routes found (minimum %d). "
            "Aborting to prevent publishing corrupted output.",
            len(routes),
            MIN_ROUTES_SANITY,
        )
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
