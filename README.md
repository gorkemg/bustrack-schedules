# bustrack-schedules

Automated ETL pipeline that generates standardized `schedules.json` artifacts for transit agencies, published to GitHub Releases as a lightweight CDN for iOS (and Android) clients.

Currently supported agencies: **UTA** (Utah Transit Authority) · **PVTA** (Pioneer Valley Transit Authority)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  GitHub Actions  (cron: 1st of month · push to main/beta · manual)      │
│                                                                          │
│  ① Download UTA GTFS zip  →  data/uta/routes.txt                        │
│  ② uta_parser.py                                                         │
│       • Reads GTFS routes.txt (route_id, short_name, color, URL)        │
│       • ThreadPoolExecutor: fetches each rideuta.com route page          │
│       • Extracts schedule PDF and map PDF via BeautifulSoup              │
│       • Derives vehicle_type groupings (FrontRunner / TRAX / BRT / …)   │
│       → output/uta_schedules.json                                        │
│                                                                          │
│  ③ Download PVTA GTFS zip  →  data/pvta/routes.txt                      │
│  ④ pvta_parser.py                                                        │
│       • Reads GTFS routes.txt (route_short_name, long_name, color)      │
│       • Scrapes pvta.com/schedules.php  → schedule_pdf_url per route    │
│       • Scrapes pvta.com/routeMaps.php  → map_pdf_url per route         │
│       • Normalizes route IDs (Rt31 → 31) to match GTFS short names      │
│       • Sanity check: aborts if fewer than 30 routes resolved            │
│       → output/pvta_schedules.json                                       │
│                                                                          │
│  ⑤ Enforce status (fail fast if any step errored)                       │
│  ⑥ gh release upload → rolling tags uta / pvta (or uta-beta / pvta-beta)│
└─────────────────────────────────────────────────────────────────────────┘
                              │
              GitHub Releases CDN (stable, versioned URLs)
                              │
          ┌───────────────────┴───────────────────┐
          ▼                                       ▼
   iOS BusTrack App                     Android BusTrack App
   (URLSession + Codable)               (OkHttp + Moshi / Gson)
```

---

## Repository structure

```
bustrack-schedules/
├── .github/
│   └── workflows/
│       └── schedule_update.yml   # CI/CD pipeline definition
├── .agents/
│   └── skills/
│       └── bustrack-schedules/
│           └── skill.md          # AI agent skill for mobile integration
├── data/
│   ├── uta/
│   │   └── routes.txt            # GTFS feed (downloaded by CI, committed as fallback)
│   └── pvta/
│       └── routes.txt            # GTFS feed (downloaded by CI)
├── docs/
│   └── schema-v1.0.md            # Full JSON schema specification
├── output/                       # Generated JSON (not committed; published via Releases)
│   ├── uta_schedules.json
│   └── pvta_schedules.json
├── src/
│   ├── uta_parser.py
│   └── pvta_parser.py
├── requirements.txt
└── README.md
```

---

## Data sources

### UTA — Utah Transit Authority

| Source | URL | Used for |
|--------|-----|----------|
| GTFS Static feed | `https://apps.rideuta.com/tms/gtfs/Static` | `route_id`, `route_short_name`, `route_long_name`, `route_color`, `route_url` |
| Per-route web pages | `https://www.rideuta.com/Rider-Tools/Schedules-and-Maps/…` | Scraping schedule PDF and map PDF links |

UTA's GTFS `route_url` field points directly to the per-route schedule page, which is used as the scrape target. UTA uses `route_id` (an internal integer) as the primary key in the output JSON.

### PVTA — Pioneer Valley Transit Authority

| Source | URL | Used for |
|--------|-----|----------|
| GTFS Static feed | `http://www.pvta.com/g_trans/google_transit.zip` | `route_short_name` (canonical key), `route_long_name`, `route_color` |
| Schedule page | `https://www.pvta.com/schedules.php` | `schedule_pdf_url` per route |
| Maps page | `https://www.pvta.com/routeMaps.php` | `map_pdf_url` per route |

PVTA's web pages sometimes prefix route IDs with `Rt` (e.g. `Rt31`). The parser normalizes these to match GTFS `route_short_name` (e.g. `31`) via `normalize_route_id()`. PVTA uses `route_short_name` as the primary key in the output JSON.

---

## Output JSON — schema overview

Both `uta_schedules.json` and `pvta_schedules.json` conform to **schema v1.0**. Full field-level documentation lives in [`docs/schema-v1.0.md`](docs/schema-v1.0.md).

```json
{
  "agency_id": "UTA",
  "schema_version": "1.0",
  "generated_at": "2026-06-01T00:00:00+00:00",
  "routes": {
    "87708": {
      "route_short_name": "1",
      "route_long_name": "South Temple",
      "route_color": "2eb566",
      "schedule_pdf_url": "https://www.rideuta.com/…/schedule.pdf",
      "map_pdf_url": "https://www.rideuta.com/…/map.pdf"
    },
    "41065": {
      "route_short_name": "750",
      "route_long_name": "FrontRunner",
      "route_color": "c227b9",
      "schedule_pdf_url": "https://…",
      "map_pdf_url": null
    }
  },
  "groupings": {
    "vehicle_type": [
      {
        "group_id": "commuter_rail_frontrunner",
        "name": "Commuter Rail (FrontRunner)",
        "route_ids": ["41065"]
      },
      {
        "group_id": "light_rail_trax",
        "name": "Light Rail (TRAX)",
        "route_ids": ["5907", "8246", "39020", "45389"]
      }
    ]
  }
}
```

Key design decisions:
- **Primary key**: UTA uses GTFS `route_id` (stable integer); PVTA uses GTFS `route_short_name` (human-readable string).
- **`route_color`**: Raw 6-digit hex without `#` prefix, per GTFS spec. Null if the agency omits it.
- **`schedule_pdf_url` / `map_pdf_url`**: Null if scraping failed for a specific route — clients must handle gracefully.
- **`groupings`**: Dimension name differs by agency (`vehicle_type` for UTA, `region` for PVTA), allowing the iOS app to render appropriate section headers.

---

## GitHub Releases — CDN URLs

The pipeline never commits generated files back to Git. Instead it publishes them as GitHub Release assets under stable, predictable tags:

| Environment | Agency | Asset URL |
|-------------|--------|-----------|
| Production (`main`) | UTA | `https://github.com/gorkemg/bustrack-schedules/releases/download/uta/uta_schedules.json` |
| Production (`main`) | PVTA | `https://github.com/gorkemg/bustrack-schedules/releases/download/pvta/pvta_schedules.json` |
| Beta | UTA | `https://github.com/gorkemg/bustrack-schedules/releases/download/uta-beta/uta_schedules.json` |
| Beta | PVTA | `https://github.com/gorkemg/bustrack-schedules/releases/download/pvta-beta/pvta_schedules.json` |

Each upload uses `--clobber`, so the tag always holds the most recent artifact. The URL is permanent and can be hardcoded in the mobile apps.

### Rolling release strategy

```
main branch  →  tags: uta, pvta          (stable production releases)
beta branch  →  tags: uta-beta, pvta-beta  (pre-release, marked in GitHub UI)
```

---

## GitHub Actions workflow

The workflow (`.github/workflows/schedule_update.yml`) runs on three triggers:

| Trigger | Condition |
|---------|-----------|
| `schedule` | Cron `0 0 1 * *` — midnight UTC on the 1st of every month |
| `push` | Any push to `main` or `beta` that touches `src/**`, `.github/workflows/**`, or `requirements.txt` |
| `workflow_dispatch` | Manual run from the GitHub Actions UI |

Each GTFS download step uses `continue-on-error: true`. If the download fails, the corresponding parser step is skipped. The "Enforce Workflow Status" gate collects all step outcomes and fails the workflow (triggering a GitHub notification) if any are in `failure` state — preventing silent partial publishes.

---

## Local development

### Prerequisites

- Python 3.11+
- `pip install -r requirements.txt`

### Run parsers locally

```bash
# UTA (requires data/uta/routes.txt — committed to repo as fallback)
python src/uta_parser.py

# PVTA (requires data/pvta/routes.txt — download manually or via CI)
curl -fsSL "http://www.pvta.com/g_trans/google_transit.zip" -o pvta_gtfs.zip
unzip -o pvta_gtfs.zip routes.txt -d data/pvta/ && rm pvta_gtfs.zip
python src/pvta_parser.py
```

Output is written to `output/uta_schedules.json` and `output/pvta_schedules.json`.

### Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `requests` | 2.32.3 | HTTP fetching with retry adapter |
| `beautifulsoup4` | 4.12.3 | HTML parsing |
| `lxml` | 5.2.2 | Fast BS4 parser backend |

---

## Adding a new agency

1. Create `data/<agency>/` and add a GTFS download step to the workflow.
2. Add `src/<agency>_parser.py` following the same contract:
   - Reads from `data/<agency>/routes.txt`.
   - Writes to `output/<agency>_schedules.json`.
   - Exits non-zero if data quality is insufficient.
   - Conforms to schema v1.0.
3. Add the parser run step and its GTFS step to the workflow.
4. Add a new release tag to the "Publish" step.

---

## License

See [LICENSE](LICENSE).
