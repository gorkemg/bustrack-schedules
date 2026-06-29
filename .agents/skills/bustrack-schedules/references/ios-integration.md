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

    /// Fetches a manifest using a conditional GET when a cached ETag is available.
    /// Returns nil on HTTP 304 Not Modified (cached data is still current).
    /// Throws ScheduleError on any failure.
    func fetchManifest(from url: URL, cachedEtag: String? = nil) async throws -> (manifest: ScheduleManifest, raw: Data, etag: String?)? {
        var request = URLRequest(url: url)
        if let cachedEtag {
            request.setValue(cachedEtag, forHTTPHeaderField: "If-None-Match")
        }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw ScheduleError.networkFailure(URLError(.badServerResponse))
        }
        if http.statusCode == 304 { return nil }
        guard http.statusCode == 200 else {
            throw ScheduleError.networkFailure(URLError(.badServerResponse))
        }
        let manifest: ScheduleManifest
        do {
            manifest = try JSONDecoder.scheduleDecoder.decode(ScheduleManifest.self, from: data)
        } catch {
            throw ScheduleError.decodingFailure(error)
        }
        guard manifest.schemaVersion.prefix(1) == "1" else {
            throw ScheduleError.unsupportedSchemaVersion(manifest.schemaVersion)
        }
        return (manifest, data, http.value(forHTTPHeaderField: "ETag"))
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

    func save(_ data: Data, etag: String?, for agency: String) throws {
        try data.write(to: dataURL(for: agency), options: .atomic)
        if let etag {
            try? etag.write(to: etagURL(for: agency), atomically: true, encoding: .utf8)
        }
    }

    func load(for agency: String) -> Data? {
        try? Data(contentsOf: dataURL(for: agency))
    }

    func etag(for agency: String) -> String? {
        try? String(contentsOf: etagURL(for: agency), encoding: .utf8)
    }

    func isStale(for agency: String, maxAge: TimeInterval = 7 * 24 * 3600) -> Bool {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: dataURL(for: agency).path),
              let modified = attrs[.modificationDate] as? Date else { return true }
        return Date().timeIntervalSince(modified) > maxAge
    }

    private func dataURL(for agency: String) -> URL {
        directory.appendingPathComponent("\(agency.lowercased())_schedules.json")
    }

    private func etagURL(for agency: String) -> URL {
        directory.appendingPathComponent("\(agency.lowercased())_schedules.etag")
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
            if let result = try await service.fetchManifest(from: endpoint, cachedEtag: cache.etag(for: agency)) {
                // 200 — new content; update cache and manifest.
                try cache.save(result.raw, etag: result.etag, for: agency)
                manifest = result.manifest
            }
            // nil — 304 Not Modified; cached manifest already shown above.
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

## Asset caching (on-demand)

Assets (schedule PDFs and PVTA route map JPEGs) are downloaded on demand when the user taps "View". The URL from the manifest is the cache key: if an agency publishes a new file at a new URL, the old cached file becomes an orphan and the new one is downloaded automatically.

```swift
// Services/AssetCache.swift
import Foundation

final class AssetCache {
    static let shared = AssetCache()
    private let directory: URL

    private init() {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        directory = docs.appendingPathComponent("BusTrackAssets", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        excludeFromBackup(directory)  // reconstructable data must not inflate iCloud backups
    }

    /// Returns a local file URL, downloading from `remoteURL` only if missing or stale.
    func localURL(for remoteURL: URL) async throws -> URL {
        let local = cacheFile(for: remoteURL)
        if FileManager.default.fileExists(atPath: local.path),
           let localMod = modificationDate(local),
           let serverMod = try? await lastModified(remoteURL),
           localMod >= serverMod {
            return local
        }
        return try await fetch(remoteURL, to: local)
    }

    private func fetch(_ url: URL, to local: URL) async throws -> URL {
        let (tmp, response) = try await URLSession.shared.download(from: url)
        guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        try? FileManager.default.removeItem(at: local)
        try FileManager.default.moveItem(at: tmp, to: local)
        excludeFromBackup(local)
        return local
    }

    private func lastModified(_ url: URL) async throws -> Date? {
        var req = URLRequest(url: url)
        req.httpMethod = "HEAD"
        let (_, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse,
              let header = http.value(forHTTPHeaderField: "Last-Modified") else { return nil }
        return DateFormatter.httpDate.date(from: header)
    }

    private func modificationDate(_ url: URL) -> Date? {
        (try? FileManager.default.attributesOfItem(atPath: url.path))?[.modificationDate] as? Date
    }

    private func cacheFile(for url: URL) -> URL {
        let hash = String(abs(url.absoluteString.hashValue), radix: 16)
        return directory.appendingPathComponent("\(hash)_\(url.lastPathComponent)")
    }

    private func excludeFromBackup(_ url: URL) {
        var mutable = url
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? mutable.setResourceValues(values)
    }
}

private extension DateFormatter {
    static let httpDate: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "EEE, dd MMM yyyy HH:mm:ss zzz"
        f.locale = Locale(identifier: "en_US_POSIX")
        return f
    }()
}
```

## Route detail view

`.quickLookPreview` handles both PDFs and JPEG route maps uniformly, eliminating the need to distinguish asset types in the view layer.

```swift
// Views/RouteDetailView.swift
import SwiftUI
import QuickLook

struct RouteDetailView: View {
    let entry: RouteEntry
    @State private var scheduleState: AssetState = .idle
    @State private var mapState: AssetState = .idle
    @State private var previewURL: URL?

    var body: some View {
        List {
            assetRow("View Schedule", icon: "calendar", url: entry.schedulePdfUrl, state: $scheduleState)
            assetRow("View Map",      icon: "map",      url: entry.mapPdfUrl,      state: $mapState)
        }
        .navigationTitle(entry.displayName)
        .quickLookPreview($previewURL)
    }

    @ViewBuilder
    private func assetRow(_ label: String, icon: String, url: URL?, state: Binding<AssetState>) -> some View {
        if url != nil {
            Button {
                Task { await openAsset(url: url!, state: state) }
            } label: {
                HStack {
                    Label(label, systemImage: icon)
                    Spacer()
                    switch state.wrappedValue {
                    case .loading: ProgressView()
                    case .failed:  Image(systemName: "exclamationmark.triangle").foregroundStyle(.red)
                    case .idle:    EmptyView()
                    }
                }
            }
            .disabled(state.wrappedValue == .loading)
            if case .failed(let msg) = state.wrappedValue {
                Text(msg).font(.caption).foregroundStyle(.red)
            }
        }
    }

    private func openAsset(url: URL, state: Binding<AssetState>) async {
        state.wrappedValue = .loading
        do {
            previewURL = try await AssetCache.shared.localURL(for: url)
            state.wrappedValue = .idle
        } catch {
            state.wrappedValue = .failed(error.localizedDescription)
        }
    }
}

enum AssetState: Equatable {
    case idle, loading
    case failed(String)
}
```

## Caching strategy

### Manifest (JSON)

| Scenario | Behavior |
|----------|----------|
| No cached file | Fetch immediately; show `ProgressView` |
| Cache < 7 days old | Serve cache; skip network |
| Cache ≥ 7 days old | Serve cache immediately; conditional GET in background via `If-None-Match` |
| Server returns 304 | ETag matches — keep cache, no download |
| Server returns 200 | New ETag + bytes; update cache and manifest |
| Pull-to-refresh | Force conditional GET regardless of age |
| Fetch fails, cache exists | Keep showing cached data; surface error as banner |
| Fetch fails, no cache | Show `ContentUnavailableView` with error |

Manifest bytes live in `Caches/BusTrackSchedules/`; ETag in a companion `.etag` sidecar. Re-decoding on each launch means schema updates are picked up without clearing the cache.

### Assets (PDFs & JPEG maps)

| Scenario | Behavior |
|----------|----------|
| Asset not cached | Download on user tap; show `ProgressView` |
| Asset cached, `Last-Modified` unchanged | Serve local file immediately |
| Asset cached, `Last-Modified` newer | Re-download; replace cached file |
| Asset URL changed in manifest | Different cache key → fresh download; old file becomes orphan |
| Download fails | Show error label inline; button re-enables for retry |

Assets live in `Documents/BusTrackAssets/` with `isExcludedFromBackup = true`. The cache key is the asset URL, so agency-side file renames (common for PDF version updates) automatically trigger a fresh download.
