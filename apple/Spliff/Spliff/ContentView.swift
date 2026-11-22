import SwiftUI
import WebKit

struct ContentView: View {
    @EnvironmentObject var model: CompanionModel
    @StateObject private var workspace = LibraryWorkspace()
    var body: some View {
        ZStack {
            if model.dashboardReady {
                NavigationSplitView {
                    List(selection: Binding<LibraryPage?>(
                        get: { workspace.section },
                        set: { if let page = $0 { workspace.navigate(page, base: model.dashboardURL) } }
                    )) {
                        ForEach(LibraryPage.allCases) { page in
                            Label(page.title, systemImage: page.symbol).tag(page)
                        }
                    }
                    .navigationSplitViewColumnWidth(min: 140, ideal: 160, max: 215)
                } detail: {
                    if let route = workspace.webRoute {
                        DashboardView(url: route, album: workspace.albumToOpen, workspace: workspace)
                            .id(route).navigationTitle(workspace.section.title)
                    } else {
                        AlbumTable(workspace: workspace).navigationTitle("Albums")
                    }
                }
                .id(model.revision)
                .opacity(model.settingsOpen ? 0 : 1)
                .accessibilityHidden(model.settingsOpen)
                .allowsHitTesting(!model.settingsOpen)
            }
            if model.settingsOpen {
                VStack(alignment: .leading, spacing: 0) {
                    HStack {
                        Text("Settings").font(.headline)
                        Spacer()
                        Button { model.settingsOpen = false } label: { Image(systemName: "xmark") }
                            .buttonStyle(.bordered).help("Close settings").accessibilityLabel("Close settings")
                            .keyboardShortcut(.cancelAction)
                    }.padding(20)
                    ScrollView {
                        VStack(alignment: .leading, spacing: 20) {
                            ConnectionView()
                            if model.dashboardReady {
                                Divider()
                                Button("Replacement preferences…") {
                                    model.settingsOpen = false
                                    workspace.navigate(.albums, base: model.dashboardURL, preferences: true)
                                }
                            }
                        }.frame(maxWidth: 420).padding(30)
                            .frame(maxWidth: .infinity)
                    }
                }
            } else if !model.dashboardReady {
                ConnectionView().frame(maxWidth: 420).padding(40)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Label(model.connected ? "Connected" : "Offline", systemImage: model.connected ? "circle.fill" : "circle")
                    .foregroundStyle(model.connected ? .green : .secondary).font(.caption)
            }
            ToolbarItem(placement: .primaryAction) {
                Button(action: model.chooseMusic) { Label("Choose music", systemImage: "folder.badge.plus") }
                    .disabled(model.busy || model.connecting || !model.runtimeAvailable)
            }
            ToolbarItem(placement: .primaryAction) {
                Button { model.settingsOpen.toggle() } label: { Label("Settings", systemImage: "gearshape") }
            }
        }
        .safeAreaInset(edge: .bottom) {
            if !model.settingsOpen && model.dashboardReady && (model.busy || !model.message.isEmpty) {
                HStack {
                    if model.busy { ProgressView().controlSize(.small) }
                    Text(model.message).font(.caption).textSelection(.enabled)
                    Spacer()
                    if !model.busy { Button { model.message = "" } label: { Image(systemName: "xmark") }.buttonStyle(.plain).accessibilityLabel("Dismiss") }
                }.padding(10).background(.bar)
            }
        }
        .task {
            while !Task.isCancelled {
                await model.refresh()
                try? await Task.sleep(for: .seconds(8))
            }
        }
    }
}

struct ConnectionView: View {
    @EnvironmentObject var model: CompanionModel
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Connection").font(.headline)
            if model.runtimeAvailable {
                HStack {
                    Label(model.dashboardReady ? "Music ready" : "Music offline", systemImage: model.dashboardReady ? "checkmark.circle" : "circle")
                    Spacer()
                    Label(model.connected ? "Chat connected" : "Chat offline", systemImage: model.connected ? "checkmark.circle" : "circle")
                }.font(.caption).foregroundStyle(.secondary)
                SecureField("Tunnel runtime key", text: $model.runtimeKey)
                    .textFieldStyle(.roundedBorder).privacySensitive()
                Text("Saved only on this Mac. Leave blank to reuse a saved key.")
                    .font(.caption).foregroundStyle(.secondary)
                Button(model.connected ? "Update connection" : "Connect") {
                    let credential = model.runtimeKey
                    model.runtimeKey = ""
                    Task { await model.install(key: credential) }
                }.buttonStyle(.borderedProminent).disabled(model.busy || model.connecting)
                HStack {
                    Button("Check connection") { Task { await model.refresh() } }
                    Button("Restart") { Task { await model.restart() } }
                }.disabled(model.busy || model.connecting)
            } else {
                Text("Choose your Spliff project to connect this development build.")
                    .foregroundStyle(.secondary)
                Button("Choose Spliff folder…", action: model.chooseProject).buttonStyle(.borderedProminent)
            }
            if !model.message.isEmpty { Text(model.message).font(.caption).textSelection(.enabled) }
            if !model.dashboardReady { Text("Connect Nicotine+ to search peers.").font(.caption).foregroundStyle(.secondary) }
        }
    }
}

struct DashboardView: NSViewRepresentable {
    let url: URL
    let album: LibraryAlbum?
    let workspace: LibraryWorkspace
    func makeCoordinator() -> Coordinator { Coordinator(origin: url, album: album, workspace: workspace) }
    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(context.coordinator, name: "libraryPage")
        let web = WKWebView(frame: .zero, configuration: configuration)
        web.navigationDelegate = context.coordinator
        web.underPageBackgroundColor = .windowBackgroundColor
        web.load(URLRequest(url: url))
        return web
    }
    func updateNSView(_ nsView: WKWebView, context: Context) {}
    final class Coordinator: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
        let origin: URL
        let album: LibraryAlbum?
        let workspace: LibraryWorkspace
        init(origin: URL, album: LibraryAlbum?, workspace: LibraryWorkspace) {
            self.origin = origin
            self.album = album
            self.workspace = workspace
        }
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            guard let album, let data = try? JSONEncoder().encode(album),
                  let object = try? JSONSerialization.jsonObject(with: data) else { return }
            Task {
                do { _ = try await webView.callAsyncJavaScript("openAlbumSearch(album)", arguments: ["album": object], in: nil, contentWorld: .page) }
                catch { self.workspace.error = error.localizedDescription; self.workspace.webRoute = nil }
            }
        }
        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
            guard message.frameInfo.isMainFrame, message.frameInfo.securityOrigin.host == origin.host,
                  message.frameInfo.securityOrigin.port == origin.port,
                  let page = message.body as? String else { return }
            if page == "library" { workspace.navigate(.albums, base: origin) }
            else if let section = LibraryPage(rawValue: page) { workspace.section = section }
        }
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url else { decisionHandler(.cancel); return }
            if url.scheme == origin.scheme && url.host == origin.host && url.port == origin.port {
                decisionHandler(.allow)
            } else {
                if ["https", "http"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
                decisionHandler(.cancel)
            }
        }
    }
}
