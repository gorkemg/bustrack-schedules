# BusTrack Schedules — JSON Schema v1.0

This document is the normative specification for the `*_schedules.json` artifacts produced by the `bustrack-schedules` ETL pipeline. All mobile clients and downstream consumers must implement against this spec.

---

## File inventory

| File | Release tag (prod) | Release tag (beta) |
|------|-------------------|--------------------|
| `uta_schedules.json` | `uta` | `uta-beta` |
| `pvta_schedules.json` | `pvta` | `pvta-beta` |

Base download URL pattern:
```
https://github.com/gorkemg/bustrack-schedules/releases/download/{tag}/{file}
```

---

## Top-level envelope

```json
{
  "agency_id":      "UTA",
  "schema_version": "1.0",
  "generated_at":   "2026-06-01T00:00:00+00:00",
  "routes":         { … },
  "groupings":      { … }
}
```

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `agency_id` | `string` | No | Short agency identifier. Stable values: `"UTA"`, `"PVTA"`. |
| `schema_version` | `string` | No | Semantic version of this schema. Currently `"1.0"`. Clients should reject files with an unknown major version. |
| `generated_at` | `string` (ISO 8601, UTC) | No | Timestamp of when the artifact was generated. Format: `YYYY-MM-DDTHH:MM:SS+00:00`. Use for cache freshness checks and display ("Last updated …"). |
| `routes` | `object` | No | Map of route entries keyed by `route_key` (see below). |
| `groupings` | `object` | No | Map of grouping dimension name → array of group entries. Dimension name is agency-specific (see Groupings section). |

---

## Route key

The key used in the `routes` map depends on the agency:

| Agency | Key type | Example | Rationale |
|--------|----------|---------|-----------|
| UTA | GTFS `route_id` (integer as string) | `"87708"` | Stable internal identifier; survives route renumbering |
| PVTA | GTFS `route_short_name` | `"B43"` | Human-readable; PVTA's route_ids are opaque integers with no consumer value |

Clients must treat the key as an **opaque string** — never parse it as a number.

---

## Route entry

```json
"87708": {
  "route_short_name": "1",
  "route_long_name":  "South Temple",
  "route_color":      "2eb566",
  "schedule_pdf_url": "https://www.rideuta.com/…/schedule.pdf",
  "map_pdf_url":      "https://www.rideuta.com/…/map.pdf"
}
```

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `route_short_name` | `string` | No | Short public-facing route identifier (e.g. `"1"`, `"750"`, `"B43"`). Suitable for display in list rows and map pins. For UTA, same as GTFS `route_short_name`. For PVTA, same as the route key. |
| `route_long_name` | `string` | No | Descriptive name (e.g. `"South Temple"`, `"FrontRunner"`). May be empty string `""` if the agency's GTFS omits it. Never null. |
| `route_color` | `string` | **Yes** | 6-character hex color without `#` prefix (e.g. `"2eb566"`). Sourced directly from GTFS `route_color` column. `null` if the agency does not specify a color. Prepend `#` for use in CSS or SwiftUI `Color(hex:)`. |
| `schedule_pdf_url` | `string` (URL) | **Yes** | Absolute HTTP(S) URL to the route's schedule/timetable PDF. HTTPS is preferred; HTTP is retained only when supplied by the agency and has no transport integrity. `null` if scraping failed for this route or no PDF was found on the agency's website. |
| `map_pdf_url` | `string` (URL) | **Yes** | Absolute HTTP(S) URL to the route map PDF or image, with the same HTTP caveat. `null` under the same conditions. |

### Null semantics

Both `schedule_pdf_url` and `map_pdf_url` being `null` for a route is a valid state — it means the ETL pipeline could not locate a PDF for that route at generation time. Clients must:
- Render a disabled/hidden button rather than crashing.
- Not assume a PDF exists just because it existed in a previous fetch.

`route_color` being `null` means the agency did not define a brand color for this route. Clients should fall back to a default color from their design system.

---

## Groupings

`groupings` is a dictionary where each key is a **dimension name** and each value is an **array of group entries**, sorted alphabetically by `name`.

```json
"groupings": {
  "vehicle_type": [
    {
      "group_id": "commuter_rail_frontrunner",
      "name":     "Commuter Rail (FrontRunner)",
      "route_ids": ["41065"]
    },
    {
      "group_id": "light_rail_trax",
      "name":     "Light Rail (TRAX)",
      "route_ids": ["5907", "8246", "39020", "45389"]
    },
    {
      "group_id": "bus_rapid_transit_brt",
      "name":     "Bus Rapid Transit (BRT)",
      "route_ids": ["3686", "87711", "92235"]
    },
    {
      "group_id": "flex_on-demand",
      "name":     "Flex (On-Demand)",
      "route_ids": ["20201", "21305", "…"]
    },
    {
      "group_id": "bus",
      "name":     "Bus",
      "route_ids": ["10810", "19902", "…"]
    }
  ]
}
```

### Dimension names by agency

| Agency | Dimension key | Semantics |
|--------|--------------|-----------|
| UTA | `vehicle_type` | Groups routes by mode of transport |
| PVTA | `region` | Groups routes by geographic service area |

A future agency could introduce additional dimension keys. Clients should be written to handle unknown dimension names gracefully (ignore or render generically).

### Group entry

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `group_id` | `string` | No | Machine-readable slug derived from `name`: lowercased, spaces replaced with `_`, `&` replaced with `and`, parentheses removed. Stable within a schema version. Use as a `SwiftUI` list section `id` or Android adapter key. |
| `name` | `string` | No | Human-readable label for display as a section header (e.g. `"Light Rail (TRAX)"`, `"Springfield Area"`). |
| `route_ids` | `array<string>` | No | Ordered list of route keys (matching the keys in the top-level `routes` map) that belong to this group. Sorted lexicographically. May be empty `[]` in edge cases; clients should handle this without crashing. |

**A route key may appear in more than one group within the same dimension.** PVTA cross-lists connector routes (e.g. a route bridging Springfield and Northampton) under both region headers on its own website; the pipeline mirrors that by listing the route in both groups' `route_ids`. Clients must not assume exclusivity — do not use group membership as a unique key (e.g. a "jump to this route's section" feature must handle 2 valid targets), and a route appearing in a "favorites by group" UI may legitimately render in two sections.

### UTA vehicle_type values (exhaustive)

| `name` | `group_id` | Covers |
|--------|-----------|--------|
| `Commuter Rail (FrontRunner)` | `commuter_rail_frontrunner` | Route 750 |
| `Light Rail (TRAX)` | `light_rail_trax` | Routes 701, 703, 704, 720 |
| `Bus Rapid Transit (BRT)` | `bus_rapid_transit_brt` | Routes ending in `X` (50X, 603X, 830X) |
| `Flex (On-Demand)` | `flex_on-demand` | Routes starting with `F` (F11, F94, …) |
| `Bus` | `bus` | All remaining routes |

### PVTA region values (exhaustive)

| `name` | `group_id` | Geographic area |
|--------|-----------|----------------|
| `Amherst & Surrounding` | `amherst_and_surrounding` | UMass Amherst area |
| `Northampton Area` | `northampton_area` | Northampton / Hampshire County |
| `Springfield Area` | `springfield_area` | Greater Springfield metro |
| `Other` | `other` | Routes present in GTFS but not mapped to a region |

---

## Complete example — UTA (abbreviated)

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
      "schedule_pdf_url": "https://www.rideuta.com/Portals/0/Schedules/2024/route1_schedule.pdf",
      "map_pdf_url": "https://www.rideuta.com/Portals/0/Maps/route1_map.pdf"
    },
    "41065": {
      "route_short_name": "750",
      "route_long_name": "FrontRunner",
      "route_color": "c227b9",
      "schedule_pdf_url": "https://www.rideuta.com/Portals/0/Schedules/frontrunner_schedule.pdf",
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
        "group_id": "bus",
        "name": "Bus",
        "route_ids": ["87708"]
      }
    ]
  }
}
```

## Complete example — PVTA (abbreviated)

```json
{
  "agency_id": "PVTA",
  "schema_version": "1.0",
  "generated_at": "2026-06-01T00:00:00+00:00",
  "routes": {
    "B43": {
      "route_short_name": "B43",
      "route_long_name": "Northampton - Amherst via Bay Road",
      "route_color": "004a97",
      "schedule_pdf_url": "https://www.pvta.com/g_pdfs/B43_schedule.pdf",
      "map_pdf_url": "https://www.pvta.com/g_pdfs/B43_map.pdf"
    },
    "31": {
      "route_short_name": "31",
      "route_long_name": "Amherst - UMass - Northampton",
      "route_color": null,
      "schedule_pdf_url": "https://www.pvta.com/g_pdfs/31_schedule.pdf",
      "map_pdf_url": null
    }
  },
  "groupings": {
    "region": [
      {
        "group_id": "amherst_and_surrounding",
        "name": "Amherst & Surrounding",
        "route_ids": ["30", "31", "33", "34", "35", "36", "38", "45", "46", "B79", "WP"]
      },
      {
        "group_id": "northampton_area",
        "name": "Northampton Area",
        "route_ids": ["39", "39E", "B43", "NE", "NSC", "R41", "R42", "R44", "B48"]
      }
    ]
  }
}
```

---

## Version history

| Version | Date | Changes |
|---------|------|---------|
| `1.0` | 2026-06 | Initial release. Fields: `agency_id`, `schema_version`, `generated_at`, `routes` (with `route_short_name`, `route_long_name`, `route_color`, `schedule_pdf_url`, `map_pdf_url`), `groupings`. |

### Versioning policy

- **Patch changes** (new routes added, PDF URLs updated): no schema version bump. Clients pick up automatically.
- **Backwards-compatible additions** (new nullable field added): minor version bump (e.g. `1.1`). Clients using `1.0` continue to work; fields they don't know are ignored.
- **Breaking changes** (field removed, key semantics changed, type changed): major version bump (e.g. `2.0`). Clients must gate on `schema_version` major component.
