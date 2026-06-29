# iOS Integration — BusTrack Schedules

Complete Swift implementation for fetching, caching, and displaying BusTrack schedule manifests.

## Codable models

```swift
// Models/ScheduleManifest.swift
import Foundation

struct ScheduleManifest: Codable {
    let agencyId: String
    let schemaVersion: String
    let generatedAt: Date
    let routes: [String: RouteEntry]
    /// Key: "vehicle_type" (UTA) or "region" (PVTA)
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

## route_color → SwiftUI Color

```swift
// Extensions/RouteEntry+Color.swift
import SwiftUI

extension RouteEntry {
    /// Converts the 6-char hex route_color to a SwiftUI Color. Returns nil if absent or malformed.
    var color: Color? {
        guard let hex = routeColor,
              hex.count == 6,
              let value = UInt64(hex, radix: 16) else { return nil }
        let r = Double((value >> 16) & 0xFF) / 255
        let g = Double((value >> 8)  & 0xFF) / 255
        let b = Double(value         & 0xFF) / 255
        return Color(red: r, green: g, blue: b)
    }

    /// Display name: long name if non-empty, otherwise short name.
    var displayName: String {
        routeLongName.isEmpty ? routeShortName : routeLongName
    }
}
```

## JSON decoder

```swift
// Services/ScheduleDecoder.swift
import Foundation

extension JSONDecoder {
    static var scheduleDecoder: JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }
}
```

## Fetch service

```swift
// Services/ScheduleService.swift
import Foundation

enum ScheduleError: LocalizedError {
    case networkFailure(Error)
    case decodingFailure(Error)
    case unsupportedSchemaVersion(String)

    var errorDescription: String? {
        switch self {
        case .networkFailure(let e):
            return "Network error: \(e.localizedDescription)"
        case .decodingFailure(let e):
            return "Decoding error: \(e.localizedDescription)"
        case .unsupportedSchemaVersion(let v):
            return "Unsupported schema version: \(v)"
        }
    }
}

enum ScheduleEndpoints {
    static let utaProduction  = URL(string: "https://github.com/gorkemg/bustrack-schedules/releases/download/uta/uta_schedules.json")!
    static let pvtaProduction = URL(string: "https://github.com/gorkemg/bustrack-schedules/releases/download/pvta/pvta_schedules.json")!
    static let utaBeta        = URL(string: "https://github.com/gorkemg/bustrack-schedules/releases/download/uta-beta/uta_schedules.json")!
    static let pvtaBeta       = URL(string: "https://github.com/gorkemg/bustrack-schedules/releases/download/pvta-beta/pvta_schedules.json")!
}

final class ScheduleService {
    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    /// Fetches and decodes a manifest. Throws ScheduleError on any failure.
    func fetchManifest(from url: URL) async throws -> (manifest: ScheduleManifest, raw: Data) {
        let (data, response) = try await session.data(from: url)

        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw ScheduleError.networkFailure(URLError(.badServerResponse))
        }

        let manifest: ScheduleManifest
        do {
            manifest = try JSONDecoder.scheduleDecoder.decode(ScheduleManifest.self, from: data)
        } catch {
            throw ScheduleError.decodingFailure(error)
        }

        // Gate on major version only — "1.x" is backward compatible with "1.0".
        guard manifest.schemaVersion.prefix(1) == "1" else {
            throw ScheduleError.unsupportedSchemaVersion(manifest.schemaVersion)
        }

        return (manifest, data)
    }
}
```

## Disk cache (stale-while-revalidate)

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

## ViewModel

```swift
// ViewModels/ScheduleViewModel.swift
import Foundation
import Observation

@Observable
final class ScheduleViewModel {
    var manifest: ScheduleManifest?
    var isLoading = false
    var error: ScheduleError?

    /// Ordered groups for section-header rendering.
    /// Both "vehicle_type" and "region" map to the first (and only) groupings value.
    var groups: [RouteGroup] {
        manifest?.groupings.values.first ?? []
    }

    func entry(for routeId: String) -> RouteEntry? {
        manifest?.routes[routeId]
    }

    private let service = ScheduleService()
    private let cache = ScheduleCache()
    let agency: String
    let endpoint: URL

    init(agency: String, endpoint: URL) {
        self.agency = agency
        self.endpoint = endpoint
    }

    func load(forceRefresh: Bool = false) async {
        // 1. Serve cache immediately if available.
        if let cached = cache.load(for: agency),
           let decoded = try? JSONDecoder.scheduleDecoder.decode(ScheduleManifest.self, from: cached) {
            manifest = decoded
        }

        // 2. Skip network if cache is fresh and no manual refresh requested.
        guard forceRefresh || cache.isStale(for: agency) else { return }

        isLoading = true
        defer { isLoading = false }

        do {
            let (newManifest, raw) = try await service.fetchManifest(from: endpoint)
            try cache.save(raw, for: agency)
            manifest = newManifest
            error = nil
        } catch let e as ScheduleError {
            error = e
        } catch {
            self.error = .networkFailure(error)
        }
    }
}
```

## SwiftUI grouped list

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
                    ContentUnavailableView(
                        error.localizedDescription,
                        systemImage: "exclamationmark.triangle"
                    )
                } else {
                    List {
                        ForEach(viewModel.groups) { group in
                            Section(group.name) {
                                ForEach(group.routeIds, id: \.self) { routeId in
                                    if let entry = viewModel.entry(for: routeId) {
                                        RouteRowView(entry: entry)
                                    }
                                }
                            }
                        }
                    }
                    .refreshable { await viewModel.load(forceRefresh: true) }
                }
            }
            .navigationTitle("Schedules")
            .task { await viewModel.load() }
        }
    }
}

struct RouteRowView: View {
    let entry: RouteEntry

    var body: some View {
        HStack(spacing: 12) {
            Text(entry.routeShortName)
                .font(.caption.bold())
                .foregroundStyle(.white)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(entry.color ?? .accentColor, in: RoundedRectangle(cornerRadius: 6))

            VStack(alignment: .leading, spacing: 2) {
                Text(entry.displayName)
                    .font(.body)

                HStack(spacing: 10) {
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
        .contentShape(Rectangle())
    }
}
```

## Route detail / PDF viewer

```swift
// Views/RouteDetailView.swift
import SwiftUI
import SafariServices
import PDFKit

struct RouteDetailView: View {
    let entry: RouteEntry
    @State private var showSchedule = false
    @State private var showMap = false

    var body: some View {
        List {
            if let url = entry.schedulePdfUrl {
                Button("View Schedule") { showSchedule = true }
                    .sheet(isPresented: $showSchedule) {
                        SafariView(url: url)
                    }
            }
            if let url = entry.mapPdfUrl {
                Button("View Map") { showMap = true }
                    .sheet(isPresented: $showMap) {
                        SafariView(url: url)
                    }
            }
        }
        .navigationTitle(entry.displayName)
    }
}

struct SafariView: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> SFSafariViewController {
        SFSafariViewController(url: url)
    }

    func updateUIViewController(_ uiViewController: SFSafariViewController, context: Context) {}
}
```

## Caching strategy

| Scenario | Behavior |
|----------|----------|
| No cached file | Fetch immediately; show `ProgressView` while loading |
| Cache < 7 days old | Serve cache; skip network entirely |
| Cache ≥ 7 days old | Serve cache immediately; refresh in background |
| Pull-to-refresh | Force network fetch regardless of cache age |
| Fetch fails, cache exists | Keep showing cached data; surface error as banner |
| Fetch fails, no cache | Show `ContentUnavailableView` with error |

Cache stores raw JSON bytes so re-decoding on every launch picks up schema changes without clearing the cache.
