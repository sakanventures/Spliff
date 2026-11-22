import SwiftUI
import Combine

enum LibraryPage: String, CaseIterable, Identifiable {
    case albums, downloads, tracks, reviews
    var id: Self { self }
    var title: String {
        switch self {
        case .albums: "Albums"
        case .downloads: "Downloads"
        case .tracks: "Track Index"
        case .reviews: "Staged Reviews"
        }
    }
    var symbol: String {
        switch self {
        case .albums: "square.stack"
        case .downloads: "arrow.down.circle"
        case .tracks: "music.note.list"
        case .reviews: "waveform"
        }
    }
}

struct LibraryAlbum: Codable, Identifiable {
    let id, total, mp3, protected: Int
    let album, root, folder: String
    let artist: String?
    var path: String { URL(fileURLWithPath: root).appendingPathComponent(folder).path }
}

struct AlbumPage: Decodable {
    let items: [LibraryAlbum]
    let more, has_library: Bool
}

@MainActor
final class LibraryWorkspace: ObservableObject {
    @Published var section = LibraryPage.albums
    @Published var query = ""
    @Published var selectedID: Int?
    @Published var result: AlbumPage?
    @Published var error = ""
    @Published var webRoute: URL?
    var albumToOpen: LibraryAlbum?

    func navigate(_ page: LibraryPage, base: URL, album: LibraryAlbum? = nil, preferences: Bool = false) {
        section = page
        albumToOpen = album
        webRoute = page == .albums && album == nil && !preferences ? nil :
            base.appending(queryItems: [URLQueryItem(name: "page", value: page.rawValue),
                                        URLQueryItem(name: "preferences", value: preferences ? "1" : "0")])
    }

    func load(base: URL) async {
        let filter = query
        let url = URL(string: "/api/albums", relativeTo: base)!.absoluteURL
            .appending(queryItems: [URLQueryItem(name: "q", value: filter)])
        do {
            try await Task.sleep(for: .milliseconds(140))
            let (data, response) = try await URLSession.shared.data(from: url)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
            let page = try JSONDecoder().decode(AlbumPage.self, from: data)
            try Task.checkCancellation()
            guard query == filter else { return }
            result = page
            error = ""
            if !page.items.contains(where: { $0.id == selectedID }) { selectedID = nil }
        } catch {
            if !Task.isCancelled && query == filter { self.error = error.localizedDescription }
        }
    }
}

struct AlbumTable: View {
    @ObservedObject var workspace: LibraryWorkspace
    @EnvironmentObject var model: CompanionModel
    var selected: LibraryAlbum? { workspace.result?.items.first { $0.id == workspace.selectedID } }

    var body: some View {
        Table(workspace.result?.items ?? [], selection: $workspace.selectedID) {
            TableColumn("Album") { item in Text(item.album).help(item.path) }
            TableColumn("Artist") { item in Text(item.artist ?? "Unknown") }
            TableColumn("Tracks") { item in Text(String(item.total)).monospacedDigit() }.width(52)
            TableColumn("Format") { item in Text(item.mp3 == item.total ? "MP3" : "Mixed") }.width(60)
        }
        .contextMenu(forSelectionType: Int.self) { ids in
            if let album = workspace.result?.items.first(where: { ids.contains($0.id) }) {
                Button("Find upgrades") { open(album) }
                Button("Show in Finder") { NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: album.path) }
            }
        } primaryAction: { ids in
            if let album = workspace.result?.items.first(where: { ids.contains($0.id) }) { open(album) }
        }
        .overlay {
            if !workspace.error.isEmpty {
                Text(workspace.error).foregroundStyle(.secondary)
            } else if let page = workspace.result, page.items.isEmpty {
                if page.has_library { Text("No matching albums").foregroundStyle(.secondary) }
                else { Button("Choose music folder…", action: model.chooseMusic) }
            }
        }
        .searchable(text: $workspace.query, placement: .toolbar, prompt: "Search albums")
        .toolbar {
            Button("Find upgrades", systemImage: "magnifyingglass.circle") { if let selected { open(selected) } }
                .disabled(selected == nil).help("Find upgrades for selected album")
        }
        .safeAreaInset(edge: .bottom) {
            if workspace.result?.more == true { Text("First 25 albums · Search to narrow").font(.caption).foregroundStyle(.secondary).padding(5) }
        }
        .task(id: workspace.query) { await workspace.load(base: model.dashboardURL) }
    }
    func open(_ album: LibraryAlbum) { workspace.navigate(.albums, base: model.dashboardURL, album: album) }
}
