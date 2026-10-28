"""
PVTA (Pioneer Valley Transit Authority) schedule parser.

Primary data source : GTFS routes.txt  →  data/pvta/routes.txt
Secondary sources   : bus_service/schedules.php → schedule_pdf_url per route
                      bus_service/maps.php      → map_pdf_url per route

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
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

try:
    from .security_utils import fetch_html, make_session, resolve_document_url
except ImportError:
    from security_utils import fetch_html, make_session, resolve_document_url

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

REGION_MAP: dict[str, str] = {
    # Springfield Area
    "Loop":  "Springfield Area",
    "G1":    "Springfield Area",
    "G2":    "Springfield Area",
    "G2E":   "Springfield Area",
    "G3":    "Springfield Area",
    "B4":    "Springfield Area",
    "G5":    "Springfield Area",
    "B6":    "Springfield Area",
    "B7":    "Springfield Area",
    "B7S":   "Springfield Area",
    "OWL":   "Springfield Area",
    "R10S":  "Springfield Area",
    "R10":   "Springfield Area",
    "P11":   "Springfield Area",
    "B12":   "Springfield Area",
    "R14":   "Springfield Area",
    "B17":   "Springfield Area",
    "P20":   "Springfield Area",
    "P20E":  "Springfield Area",
    "P21":   "Springfield Area",
    "P21E":  "Springfield Area",
    "B23":   "Springfield Area",
    "R24":   "Springfield Area",
    "R29":   "Springfield Area",
    "X90":   "Springfield Area",
    "X92":   "Springfield Area",
    # Northampton Area  (Rt39 / Rt39E normalized → 39 / 39E)
    "39E":          "Northampton Area",
    "R41":          "Northampton Area",
    "R42":          "Northampton Area",
    "B43":          "Northampton Area",
    "B43no_school": "Northampton Area",
    "R44":          "Northampton Area",
    "B48":          "Northampton Area",
    "39":           "Northampton Area",
    "NE":           "Northampton Area",
    "NSC":          "Northampton Area",
    # Amherst & Surrounding  (Rt30–Rt46 normalized → 30–46)
    "30":  "Amherst & Surrounding",
    "31":  "Amherst & Surrounding",
    "33":  "Amherst & Surrounding",
    "34":  "Amherst & Surrounding",
    "35":  "Amherst & Surrounding",
    "36":  "Amherst & Surrounding",
    "38":  "Amherst & Surrounding",
    "45":  "Amherst & Surrounding",
    "46":  "Amherst & Surrounding",
    "WP":  "Amherst & Surrounding",
    "B79": "Amherst & Surrounding",
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
    return make_session(SESSION_HEADERS)


def _fetch(session: requests.Session, url: str) -> str | None:
    try:
        return fetch_html(session, url)
    except (requests.RequestException, ValueError, OSError) as exc:
        log.error("Failed to fetch %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# DOM helpers shared by both page parsers
# ---------------------------------------------------------------------------

# Matches PDF, JPEG, PNG asset hrefs
_ASSET_RE = re.compile(r"\.(pdf|jpg|jpeg|png)(?:$|[?#])", re.IGNORECASE)

# Matches raw (pre-normalization) route identifiers as they appear in table cells
_ROUTE_ID_RE = re.compile(
    r"^(Loop|OWL|NSC|NE|WP|[A-Z]\d+\w*|Rt\d+\w*|[A-Z]{1,3}\d*\w*|\d+\w*)$",
    re.IGNORECASE,
)


def _absolute(href: str) -> str | None:
    return resolve_document_url(PVTA_BASE, href)


def _first_asset_in_cells(cells) -> str | None:
    """Return the first PDF/image URL found across the given table cells."""
    for cell in cells:
        for a_tag in cell.find_all("a", href=True):
            href: str = a_tag["href"]
            if _ASSET_RE.search(href):
                absolute = _absolute(href)
                if absolute:
                    return absolute
    return None


# ---------------------------------------------------------------------------
# Generic table-based asset-page parser
# ---------------------------------------------------------------------------

def _parse_asset_table(html: str, page_label: str) -> dict[str, str]:
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

        asset_url = _first_asset_in_cells(cells)
        if asset_url is None:
            continue

        norm_id = normalize_route_id(raw_id)
        if norm_id not in result:
            result[norm_id] = asset_url
            log.info("%-20s %-8s → %s", page_label, norm_id, asset_url)

    if not result:
        log.warning("%s: table parse yielded 0 routes — trying link sweep", page_label)
        result = _link_sweep_fallback(soup, page_label)

    return result


def _link_sweep_fallback(soup: BeautifulSoup, label: str) -> dict[str, str]:
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
        absolute = _absolute(href)
        if norm_id not in result and absolute:
            result[norm_id] = absolute

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
    base_tag = soup.find("base", href=True)
    asset_base_url = (resolve_document_url(source_url, base_tag["href"]) or source_url) if base_tag else source_url
    result: dict[str, str] = {}

    for anchor in soup.select("#document-center .file-group a[href]"):
        href: str = anchor["href"]
        if not _ASSET_RE.search(href):
            continue
        asset_url = resolve_document_url(asset_base_url, href)
        if not asset_url:
            continue
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
    """Extract schedule URLs from the document center or legacy tables."""
    document_links = _parse_document_center_assets(html, "schedules", PVTA_SCHEDULES_URL)
    return document_links if document_links else _parse_asset_table(html, "schedules.php")


def parse_maps_page(html: str) -> dict[str, str]:
    """Extract document-center map URLs or legacy embedded images.

    Legacy pages embed each route map as:
      <img src="maps/{RouteID}.jpg">
    e.g. maps/Rt30.jpg → route 30, maps/B43.jpg → route B43.

    We scrape <img src> attributes under the maps/ path and normalize
    the filename stem via normalize_route_id (strips Rt/Route prefix).
    """
    document_links = _parse_document_center_assets(html, "maps", PVTA_MAPS_URL)
    if document_links:
        return document_links
    soup = BeautifulSoup(html, "lxml")
    result: dict[str, str] = {}
    _img_re = re.compile(r"^maps/(.+)\.(jpg|jpeg|png)$", re.IGNORECASE)

    for img_tag in soup.find_all("img", src=True):
        src: str = img_tag["src"]
        m = _img_re.match(src)
        if not m:
            continue
        norm_id = normalize_route_id(m.group(1))
        absolute = _absolute(src)
        if norm_id not in result and absolute:
            result[norm_id] = absolute
            log.info("routeMaps.php   %-8s → %s", norm_id, absolute)

    log.info("routeMaps.php: found %d map URLs", len(result))
    return result


# ---------------------------------------------------------------------------
# Groupings
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
