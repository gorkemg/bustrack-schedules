---
name: bustrack-schedules
description: Use when integrating BusTrack transit schedule data into iOS (Swift/SwiftUI) or Android (Kotlin) apps. Covers fetching ScheduleManifest JSON from GitHub Releases CDN for UTA and PVTA agencies, decoding schema v1.0 with Codable, rendering grouped route lists with section headers, displaying route color badges, opening schedule and map PDFs, and implementing a stale-while-revalidate disk cache. Also use when adding new agencies, debugging null PDF URLs, or handling schema version upgrades.
compatibility: Designed for Claude Code. Requires internet access to fetch JSON from GitHub Releases. iOS integration targets Swift 5.9+ and SwiftUI. Android integration targets Kotlin with Retrofit/Moshi.
metadata:
  author: gorkemg
  version: "1.0"
  schema-version: "1.0"
  repo: https://github.com/gorkemg/bustrack-schedules
---

## Overview

The `bustrack-schedules` pipeline publishes one JSON file per agency to GitHub Releases on a rolling "clobber" basis (updated monthly and on every pipeline push). Mobile clients fetch these files at runtime; no backend proxy is needed.

Full schema specification: `docs/schema-v1.0.md` in the pipeline repository.  
Full iOS implementation: [references/ios-integration.md](references/ios-integration.md)  
Android integration guide: [references/android-integration.md](references/android-integration.md)

---

## CDN endpoints

All URLs are stable and permanent — the tag is clobbered on each update.

```
Production (main branch):
  UTA  → https://github.com/gorkemg/bustrack-schedules/releases/download/uta/uta_schedules.json
  PVTA → https://github.com/gorkemg/bustrack-schedules/releases/download/pvta/pvta_schedules.json

Beta (beta branch):
  UTA  → https://github.com/gorkemg/bustrack-schedules/releases/download/uta-beta/uta_schedules.json
  PVTA → https://github.com/gorkemg/bustrack-schedules/releases/download/pvta-beta/pvta_schedules.json
```

---

## Schema v1.0 — quick reference

```
ScheduleManifest
├── agency_id       String               "UTA" | "PVTA"
├── schema_version  String               "1.0"
├── generated_at    String (ISO 8601)    UTC, e.g. "2026-06-01T00:00:00+00:00"
├── routes          { String: RouteEntry }
│     └── key:     UTA → GTFS route_id (opaque integer string, e.g. "87708")
│                  PVTA → GTFS route_short_name (e.g. "B43", "31")
└── groupings       { String: [RouteGroup] }
      └── key:     "vehicle_type" (UTA) | "region" (PVTA)

RouteEntry
├── route_short_name  String       display label, e.g. "1", "750", "B43"
├── route_long_name   String       may be "" — fall back to route_short_name
├── route_color       String?      6-char hex without "#", e.g. "2eb566" — may be null
├── schedule_pdf_url  String? URL  may be null — disable button, never crash
└── map_pdf_url       String? URL  may be null — disable button, never crash

RouteGroup
├── group_id   String     machine slug, stable within v1.x, safe as persistent ID
├── name       String     display label for section headers
└── route_ids  [String]   keys into `routes`; may be empty []
```

### UTA vehicle_type groups (exhaustive)

| name | group_id |
|------|----------|
| Commuter Rail (FrontRunner) | `commuter_rail_frontrunner` |
| Light Rail (TRAX) | `light_rail_trax` |
| Bus Rapid Transit (BRT) | `bus_rapid_transit_brt` |
| Flex (On-Demand) | `flex_on-demand` |
| Bus | `bus` |

### PVTA region groups (exhaustive)

| name | group_id |
|------|----------|
| Amherst & Surrounding | `amherst_and_surrounding` |
| Northampton Area | `northampton_area` |
| Springfield Area | `springfield_area` |
| Other | `other` |

---

## Implementation steps — iOS

1. Add `Codable` models (`ScheduleManifest`, `RouteEntry`, `RouteGroup`) — see [references/ios-integration.md](references/ios-integration.md).
2. Configure `JSONDecoder` with `.iso8601` date decoding strategy.
3. Implement `ScheduleService.fetchManifest(from:)` with schema version gate (reject unknown major versions).
4. Implement `ScheduleCache` writing raw bytes to the Caches directory (keyed by agency name).
5. Create `ScheduleViewModel` using `@Observable`. On `load()`: serve cache immediately, skip network if fresh (<7 days), else refresh in background.
6. Build a SwiftUI `List` driven by `manifest.groupings.values.first ?? []`, using `ForEach` over `RouteGroup.routeIds` and resolving each via `manifest.routes[id]`.
7. Add `RouteEntry.color: Color?` computed property converting `route_color` hex to SwiftUI `Color`.
8. Open PDFs via `SFSafariViewController` or a `PDFView` wrapper. Guard on `schedulePdfUrl != nil` before enabling the button.

---

## Implementation steps — Android

1. Add Retrofit + Moshi (or Gson) dependencies.
2. Define `ScheduleManifest`, `RouteEntry`, `RouteGroup` data classes with `@Json` / `@SerializedName` — see [references/android-integration.md](references/android-integration.md).
3. Parse `route_color` with `Color.parseColor("#$routeColor")`. Null-check first.
4. Open PDFs via `Intent(Intent.ACTION_VIEW, Uri.parse(url))`. Guard on non-null URL.

---

## Error handling checklist

| Scenario | Required behavior |
|----------|------------------|
| Network unreachable | Serve cached JSON; show offline indicator if no cache |
| HTTP 4xx / 5xx from CDN | Treat as network failure; serve cache |
| `schema_version` major ≠ `"1"` | Throw, show update prompt — do not render with unknown schema |
| `schedule_pdf_url` is null | Hide/disable button — never show broken link |
| `map_pdf_url` is null | Hide/disable button |
| `route_long_name` is `""` | Fall back to `route_short_name` |
| `route_color` is null | Fall back to design-system default color |
| Route key in `route_ids` absent from `routes` | Skip silently |
| JSON decode failure | Log raw body; show last cached manifest if available |

---

## Critical invariants

1. **Never display UTA `route_id` keys in UI.** Always use `route_short_name` for badges and `route_long_name` for titles.
2. **Never hardcode route IDs or counts in the app.** Drive all UI from the `groupings` array; route lists change month to month (seasonal services).
3. **`group_id` is stable within schema v1.x.** Safe as a persistent key for user preferences (e.g. favourite groups).
4. **`generated_at` is a generation timestamp, not a data-effective date.** Use only for "last refreshed" display copy.
5. **Cache raw JSON bytes, not decoded structs.** Re-decode on each launch so schema updates are picked up without a cache clear.
6. **Check only the major version prefix** when gating on `schema_version`. A `1.1` manifest is valid for a `1.0` client.
