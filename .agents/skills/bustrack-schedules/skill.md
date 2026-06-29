# Skill: bustrack-schedules

## Purpose

Use this skill whenever a task involves integrating the BusTrack schedule artifacts into the iOS app (or a future Android app). This includes: fetching schedule JSON, decoding it, rendering route lists and section groupings, opening schedule/map PDFs, and handling stale/missing data gracefully.

The BusTrack schedules pipeline publishes one JSON file per agency to GitHub Releases on a rolling basis. The iOS app fetches these files at runtime, decodes them with `Codable`, and presents route schedules organized by group (vehicle type for UTA, region for PVTA).

---

## CDN endpoints

All artifacts are served from GitHub Releases. The URLs are **stable and permanent** — the same tag is clobbered on every update.

```
Production (main branch):
  UTA  → https://github.com/gorkemg/bustrack-schedules/releases/download/uta/uta_schedules.json
  PVTA → https://github.com/gorkemg/bustrack-schedules/releases/download/pvta/pvta_schedules.json

Beta (beta branch):
  UTA  → https://github.com/gorkemg/bustrack-schedules/releases/download/uta-beta/uta_schedules.json
  PVTA → https://github.com/gorkemg/bustrack-schedules/releases/download/pvta-beta/pvta_schedules.json
```

Artifacts are refreshed on the 1st of every month (UTC midnight) and on any push to `main` or `beta` that modifies the pipeline source. The `generated_at` ISO 8601 timestamp in the JSON indicates when the data was last produced.

---

## JSON schema — quick reference

Full specification: `docs/schema-v1.0.md` in this repository.

```
ScheduleManifest
├── agency_id       String               "UTA" | "PVTA"
├── schema_version  String               "1.0"
├── generated_at    String (ISO 8601)    UTC timestamp
├── routes          [String: RouteEntry] keyed by route_key (see below)
└── groupings       [String: [RouteGroup]]
    └── dimension   "vehicle_type" (UTA) | "region" (PVTA)
        └── RouteGroup[]
            ├── group_id   String   machine slug
            ├── name       String   display label
            └── route_ids  [String] keys into `routes`

RouteEntry
├── route_short_name  String        public route number / code
├── route_long_name   String        descriptive name (may be "")
├── route_color       String?       6-char hex without "#", e.g. "2eb566"
├── schedule_pdf_url  String? (URL) may be null if not found
└── map_pdf_url       String? (URL) may be null if not found
```

**Route key semantics:**
- UTA: GTFS `route_id` — an opaque integer string (e.g. `"87708"`). Never parse as Int.
- PVTA: GTFS `route_short_name` — human-readable (e.g. `"B43"`, `"31"`).

---

## Swift / iOS integration

### 1. Codable models

```swift
// Models/ScheduleManifest.swift

import Foundation

struct ScheduleManifest: Codable {
    let agencyId: String
    let schemaVersion: String
    let generatedAt: Date
    let routes: [String: RouteEntry]
    /// Keyed by dimension name ("vehicle_type" for UTA, "region" for PVTA).
    let groupings: [String: [RouteGroup]]

    enum CodingKeys: String, CodingKey {
        case agencyId       = "agency_id"
        case schemaVersion  = "schema_version"
        case generatedAt    = "generated_at"
        case routes
        case groupings
    }
}

struct RouteEntry: Codable {
    let routeShortName: String
    let routeLongName: String
    let routeColor: String?
    let schedulePdfUrl: URL?
    let mapPdfUrl: URL?

    enum CodingKeys: String, CodingKey {
        case routeShortName = "route_short_name"
        case routeLongName  = "route_long_name"
        case routeColor     = "route_color"
        case schedulePdfUrl = "schedule_pdf_url"
        case mapPdfUrl      = "map_pdf_url"
    }
}

struct RouteGroup: Codable, Identifiable {
    let groupId: String
    let name: String
    let routeIds: [String]

    var id: String { groupId }

    enum CodingKeys: String, CodingKey {
        case groupId  = "group_id"
        case name
        case routeIds = "route_ids"
    }
}
```

### 2. JSON decoder configuration

The `generated_at` field uses ISO 8601 with timezone offset. Use `iso8601` date strategy:

```swift
// Services/ScheduleDecoder.swift

import Foundation

extension JSONDecoder {
    static var scheduleDecoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}
```

### 3. Fetch service

```swift
// Services/ScheduleService.swift

import Foundation

enum ScheduleError: LocalizedError {
    case networkFailure(Error)
    case decodingFailure(Error)
    case unsupportedSchemaVersion(String)

    var errorDescription: String? {
        switch self {
        case .networkFailure(let e):   return "Network error: \(e.localizedDescription)"
        case .decodingFailure(let e):  return "Decoding error: \(e.localizedDescription)"
        case .unsupportedSchemaVersion(let v): return "Unsupported schema version: \(v)"
        }
    }
}

struct ScheduleEndpoints {
    static let utaProduction  = URL(string: "https://github.com/gorkemg/bustrack-schedules/releases/download/uta/uta_schedules.json")!
    static let pvtaProduction = URL(string: "https://github.com/gorkemg/bustrack-schedules/releases/download/pvta/pvta_schedules.json")!
}

final class ScheduleService {
    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    func fetchManifest(from url: URL) async throws -> ScheduleManifest {
        let (data, response) = try await session.data(from: url)

        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw ScheduleError.networkFailure(
                URLError(.badServerResponse)
            )
        }

        let manifest: ScheduleManifest
        do {
            manifest = try JSONDecoder.scheduleDecoder.decode(ScheduleManifest.self, from: data)
        } catch {
            throw ScheduleError.decodingFailure(error)
        }

        // Gate on known major version to prevent silent breakage after schema changes.
        let major = manifest.schemaVersion.prefix(1)
        guard major == "1" else {
            throw ScheduleError.unsupportedSchemaVersion(manifest.schemaVersion)
        }

        return manifest
    }
}
```

### 4. Local disk cache

Cache the raw JSON on disk keyed by agency. Serve the cache immediately while refreshing in background ("stale-while-revalidate" pattern):

```swift
// Services/ScheduleCache.swift

import Foundation

final class ScheduleCache {
    private let directory: URL

    init() {
        let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        directory = caches.appendingPathComponent("BusTrackSchedules", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    func save(_ data: Data, for agency: String) throws {
        try data.write(to: fileURL(for: agency), options: .atomic)
    }

    func load(for agency: String) -> Data? {
        try? Data(contentsOf: fileURL(for: agency))
    }

    /// Returns true if the cached file is older than maxAge.
    func isStale(for agency: String, maxAge: TimeInterval = 7 * 24 * 3600) -> Bool {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: fileURL(for: agency).path),
              let modified = attrs[.modificationDate] as? Date else { return true }
        return Date().timeIntervalSince(modified) > maxAge
    }

    private func fileURL(for agency: String) -> URL {
        directory.appendingPathComponent("\(agency.lowercased())_schedules.json")
    }
}
```

### 5. ViewModel

```swift
// ViewModels/ScheduleViewModel.swift

import Foundation
import Observation

@Observable
final class ScheduleViewModel {
    var manifest: ScheduleManifest?
    var isLoading = false
    var error: ScheduleError?

    /// Ordered groups for List section rendering.
    var groups: [RouteGroup] {
        // Both "vehicle_type" and "region" are valid dimension keys.
        manifest?.groupings.values.first ?? []
    }

    /// Resolve a route key to its entry.
    func entry(for routeId: String) -> RouteEntry? {
        manifest?.routes[routeId]
    }

    private let service = ScheduleService()
    private let cache = ScheduleCache()
    private let agency: String
    private let endpoint: URL

    init(agency: String, endpoint: URL) {
        self.agency = agency
        self.endpoint = endpoint
    }

    func load() async {
        // 1. Serve cached data immediately if available.
        if let cached = cache.load(for: agency),
           let manifest = try? JSONDecoder.scheduleDecoder.decode(ScheduleManifest.self, from: cached) {
            self.manifest = manifest
        }

        // 2. Skip network refresh if cache is fresh.
        guard cache.isStale(for: agency) else { return }

        isLoading = true
        defer { isLoading = false }

        do {
            // Fetch raw data first so we can cache the bytes.
            let (data, response) = try await URLSession.shared.data(from: endpoint)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else { return }

            try cache.save(data, for: agency)
            manifest = try JSONDecoder.scheduleDecoder.decode(ScheduleManifest.self, from: data)
            error = nil
        } catch let e as ScheduleError {
            error = e
        } catch {
            self.error = .networkFailure(error)
        }
    }
}
```

### 6. SwiftUI view — grouped route list

```swift
// Views/ScheduleListView.swift

import SwiftUI

struct ScheduleListView: View {
    @State private var viewModel = ScheduleViewModel(
        agency: "UTA",
        endpoint: ScheduleEndpoints.utaProduction
    )

    var body: some View {
        NavigationStack {
            Group {
                if viewModel.manifest == nil && viewModel.isLoading {
                    ProgressView("Loading schedules…")
                } else if let error = viewModel.error, viewModel.manifest == nil {
                    ContentUnavailableView(error.localizedDescription,
                                          systemImage: "exclamationmark.triangle")
                } else {
                    List {
                        ForEach(viewModel.groups) { group in
                            Section(group.name) {
                                ForEach(group.routeIds, id: \.self) { routeId in
                                    if let entry = viewModel.entry(for: routeId) {
                                        RouteRowView(routeId: routeId, entry: entry)
                                    }
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("Schedules")
            .task { await viewModel.load() }
        }
    }
}

struct RouteRowView: View {
    let routeId: String
    let entry: RouteEntry

    var body: some View {
        HStack(spacing: 12) {
            // Route color badge
            Text(entry.routeShortName)
                .font(.caption.bold())
                .foregroundStyle(.white)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(entry.color ?? .accentColor, in: RoundedRectangle(cornerRadius: 6))

            VStack(alignment: .leading, spacing: 2) {
                Text(entry.routeLongName.isEmpty ? entry.routeShortName : entry.routeLongName)
                    .font(.body)
                HStack(spacing: 12) {
                    if entry.schedulePdfUrl != nil {
                        Label("Schedule", systemImage: "calendar")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    if entry.mapPdfUrl != nil {
                        Label("Map", systemImage: "map")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}

// MARK: - Color helper

extension RouteEntry {
    /// Converts the 6-char hex `route_color` to a SwiftUI Color. Returns nil if absent.
    var color: Color? {
        guard let hex = routeColor, hex.count == 6,
              let value = UInt64(hex, radix: 16) else { return nil }
        let r = Double((value >> 16) & 0xFF) / 255
        let g = Double((value >> 8)  & 0xFF) / 255
        let b = Double(value         & 0xFF) / 255
        return Color(red: r, green: g, blue: b)
    }
}
```

### 7. Open a PDF

Both `schedule_pdf_url` and `map_pdf_url` are standard HTTPS URLs to PDF files hosted on agency servers. Open them in-app with `SafariServices` or push a `PDFView`:

```swift
// Option A — in-app browser (simplest)
import SafariServices

if let url = entry.schedulePdfUrl {
    let safari = SFSafariViewController(url: url)
    present(safari, animated: true)
}

// Option B — native PDFView (no browser chrome)
import PDFKit

struct PDFViewerView: View {
    let url: URL

    var body: some View {
        PDFKitView(url: url)
            .navigationTitle("Schedule")
            .navigationBarTitleDisplayMode(.inline)
    }
}

struct PDFKitView: UIViewRepresentable {
    let url: URL

    func makeUIView(context: Context) -> PDFView {
        let view = PDFView()
        view.autoScales = true
        Task {
            if let doc = await PDFDocument(url: url) {
                await MainActor.run { view.document = doc }
            }
        }
        return view
    }

    func updateUIView(_ uiView: PDFView, context: Context) {}
}
```

---

## route_color usage guidelines

- The value is a **6-digit lowercase hex string without `#`** (GTFS convention), e.g. `"2eb566"`.
- Always null-check before use. Fall back to a neutral color from the design system.
- Use for route badges, list row accents, and map annotations.
- Do **not** use as a background on white text without contrast checking — some agencies use light colors (e.g. `"c227b9"` passes AA on white, `"2eb566"` passes on dark text).

---

## Error handling checklist

| Scenario | Expected behavior |
|----------|------------------|
| Network unreachable | Serve cached JSON if available; show "offline" indicator if no cache exists. |
| HTTP 4xx / 5xx from GitHub CDN | Same as above. Log the status code. |
| `schema_version` major ≠ `"1"` | Throw `ScheduleError.unsupportedSchemaVersion`. Do not render stale data. Show update prompt. |
| `schedule_pdf_url` is `null` | Hide/disable the "View Schedule" button. Never show a broken link. |
| `map_pdf_url` is `null` | Hide/disable the "View Map" button. |
| `route_long_name` is `""` | Fall back to `route_short_name` for display. |
| `routes` entry missing for a `route_id` in `groupings` | Skip silently — this should not occur in v1.0 but is a possible edge case during a transition publish. |
| JSON decode failure | Log the error with the raw response body for diagnostics. Show last cached manifest if available. |

---

## Caching strategy

| Scenario | Recommended cache TTL |
|----------|-----------------------|
| Normal operation | 7 days (pipeline runs monthly; no point fetching more often) |
| App first launch (no cache) | Fetch immediately, block UI with loading indicator |
| App launch with fresh cache (<7 days) | Serve cache instantly, skip network |
| App launch with stale cache (>7 days) | Serve cache instantly while refreshing in background |
| Foreground refresh (pull-to-refresh) | Bypass TTL, force network fetch |

Store raw JSON bytes (not decoded structs) so the decoder is re-run on each app launch — this means schema updates are picked up without clearing the cache.

---

## Android integration (future)

When implementing the Android client, apply the same architectural pattern using Kotlin coroutines and Retrofit:

```kotlin
// Retrofit interface
interface ScheduleApi {
    @GET
    suspend fun fetchManifest(@Url url: String): ScheduleManifest
}

// Data classes (use @SerializedName with Gson or @Json with Moshi)
data class ScheduleManifest(
    @SerializedName("agency_id")      val agencyId: String,
    @SerializedName("schema_version") val schemaVersion: String,
    @SerializedName("generated_at")   val generatedAt: String,
    @SerializedName("routes")         val routes: Map<String, RouteEntry>,
    @SerializedName("groupings")      val groupings: Map<String, List<RouteGroup>>
)

data class RouteEntry(
    @SerializedName("route_short_name") val routeShortName: String,
    @SerializedName("route_long_name")  val routeLongName: String,
    @SerializedName("route_color")      val routeColor: String?,
    @SerializedName("schedule_pdf_url") val schedulePdfUrl: String?,
    @SerializedName("map_pdf_url")      val mapPdfUrl: String?
)

data class RouteGroup(
    @SerializedName("group_id")  val groupId: String,
    @SerializedName("name")      val name: String,
    @SerializedName("route_ids") val routeIds: List<String>
)
```

Parse `route_color` to `android.graphics.Color` using `Color.parseColor("#$routeColor")`. Open PDFs via an Intent with `Intent.ACTION_VIEW` pointed at the URL, which the system browser or a PDF viewer app will handle.

---

## Known edge cases and design invariants

1. **UTA route keys are not human-readable.** Do not display `route_id` in the UI. Always use `route_short_name` for the badge label and `route_long_name` for the row title.

2. **PVTA may publish more routes than the static `REGION_MAP` covers.** Unknown routes appear in the `"Other"` group in the `region` groupings array. Do not hardcode PVTA route IDs in the app — always drive UI from the `groupings` array in the JSON.

3. **Route count varies month to month.** Seasonal routes (ski shuttles, school services) may appear or disappear. The app must never assume a fixed number of routes per agency.

4. **`generated_at` is a generation timestamp, not a data-effective timestamp.** It reflects when the ETL ran, not when the transit agency last updated its schedules. Use it only for "last refreshed" display copy, not for business logic.

5. **The `group_id` slug is stable within schema v1.0.** Safe to use as a persistent identifier (e.g. for user-selected favorite groups). It will be regenerated if the group `name` changes — such changes constitute a v1.x bump.
