import Combine
import SwiftUI
import WebKit

struct ContentView: View {
    @EnvironmentObject var model: CompanionModel

    var body: some View {
        Group {
            if model.dashboardReady {
                DashboardView(url: model.dashboardURL, model: model)
                    .id(model.revision)
            } else {
                VStack(alignment: .leading, spacing: 18) {
                    Text("Connect Spliff").font(.largeTitle.bold())
                    ConnectionView()
                }
                .frame(maxWidth: 360)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(Color(red: 241 / 255, green: 229 / 255, blue: 210 / 255).ignoresSafeArea())
        .inspector(isPresented: $model.settingsOpen) {
            SpliffSettings()
                .environmentObject(model)
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if model.dashboardReady && (model.busy || !model.message.isEmpty) {
                HStack {
                    if model.busy { ProgressView().controlSize(.small) }
                    Text(model.message).textSelection(.enabled)
                    Spacer()
                    if !model.busy {
                        Button("Dismiss", systemImage: "xmark") { model.message = "" }
                            .labelStyle(.iconOnly).buttonStyle(.plain)
                    }
                }.padding(12).background(.bar)
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

private struct SpliffSettings: View {
    @EnvironmentObject var model: CompanionModel

    @StateObject private var preference = LibraryPreference(
        path: "/api/replacement-preference", key: "destination", initial: "trash")
    @StateObject private var metadata = LibraryPreference(
        path: "/api/file-metadata-preference", key: "mode", initial: "preserve")

    var body: some View {
        ScrollView {
            settings
        }
        .inspectorColumnWidth(min: 300, ideal: 360, max: 480)
    }

    private var settings: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack {
                Text("Settings").font(.title2.bold())
                Spacer()
                Button("Done") { model.settingsOpen = false }.keyboardShortcut(.cancelAction)
            }
            if model.dashboardReady {
                TransferSettings(base: model.dashboardURL)
                Divider()
                VStack(alignment: .leading, spacing: 10) {
                    Text("After replacing an album").font(.headline)
                    Picker("Original files", selection: $preference.destination) {
                        Text("Move to Mac Trash").tag("trash")
                        Text("Move to hidden recovery folder").tag("hidden")
                    }.disabled(preference.saved == nil || preference.busy)
                    Text("Spliff shows this destination in the plan before you choose Replace.")
                        .foregroundStyle(.secondary)
                    HStack {
                        Button("Save preference") { Task { await preference.load(base: model.dashboardURL, save: true) } }
                            .buttonStyle(.borderedProminent)
                            .disabled(preference.saved == nil || preference.destination == preference.saved || preference.busy)
                        if preference.busy { ProgressView().controlSize(.small) }
                        Text(preference.status).foregroundStyle(.secondary)
                        if preference.saved == nil && !preference.busy {
                            Button("Retry") { Task { await preference.load(base: model.dashboardURL) } }
                        }
                    }
                }
                VStack(alignment: .leading, spacing: 10) {
                    Text("File metadata on import").font(.headline)
                    Picker("File metadata", selection: $metadata.destination) {
                        Text("Preserve downloaded files").tag("preserve")
                        Text("Write verified music tags (FLAC)").tag("write")
                    }.disabled(metadata.saved == nil || metadata.busy)
                    Text("Discogs can help identify releases with either setting.")
                        .foregroundStyle(.secondary)
                    HStack {
                        Button("Save metadata preference") { Task { await metadata.load(base: model.dashboardURL, save: true) } }
                            .buttonStyle(.borderedProminent)
                            .disabled(metadata.saved == nil || metadata.destination == metadata.saved || metadata.busy)
                        if metadata.busy { ProgressView().controlSize(.small) }
                        Text(metadata.status).foregroundStyle(.secondary)
                        if metadata.saved == nil && !metadata.busy {
                            Button("Retry") { Task { await metadata.load(base: model.dashboardURL) } }
                        }
                    }
                }
                Divider()
            }
            DisclosureGroup("ChatGPT connection") { ConnectionView().padding(.top, 12) }
        }
        .padding(24)
        .task(id: model.dashboardReady) {
            if model.dashboardReady {
                async let replacement: Void = preference.load(base: model.dashboardURL)
                async let tagging: Void = metadata.load(base: model.dashboardURL)
                _ = await (replacement, tagging)
            }
        }
    }
}

private struct TransferSettings: View {
    private struct Readiness: Decodable {
        let permission: String
        let code: String
        let message: String
        let bridgeConnected: Bool
        let soulseekConnected: Bool?

        var needsBridge: Bool { ["bridge_unavailable", "bridge_update_required"].contains(code) }
    }

    private struct Change: Decodable {
        let message: String?
        let error: String?
    }

    let base: URL
    @State private var readiness: Readiness?
    @State private var busy = false
    @State private var readFailed = false
    @State private var message = ""
    @State private var generation = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Downloads").font(.headline)
            Toggle("Allow Spliff downloads", isOn: Binding(
                get: { readiness?.permission == "on" },
                set: { enabled in
                    Task { await change("/api/transfer-permission?action=" + (enabled ? "enable" : "stop")) }
                }
            ))
            .disabled(busy || readFailed || readiness == nil ||
                      (readiness?.permission != "on" && readiness?.needsBridge == true))
            Text("Review the Nicotine+ queue before turning this on. You still choose which downloads to stage.")
                .foregroundStyle(.secondary)
            if let status = readiness, !readFailed {
                LabeledContent("Nicotine+ bridge", value: status.bridgeConnected ? "Connected" : "Unavailable")
                if let connected = status.soulseekConnected {
                    LabeledContent("Soulseek", value: connected ? "Connected" : "Offline")
                }
            }
            Text(readFailed ? "Could not check download readiness. Spliff will try again." :
                    readiness?.message ?? "Checking Nicotine+…")
                .foregroundStyle(.secondary)
            if readiness?.needsBridge == true {
                Button("Install or update Spliff Search") { Task { await change("/api/install-nicotine-bridge") } }
                    .disabled(busy)
            }
            if !message.isEmpty { Text(message) }
        }
        .task(id: base) {
            while !Task.isCancelled {
                await refresh()
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    private func refresh() async {
        guard !busy, let url = URL(string: "/api/transfer-readiness", relativeTo: base) else { return }
        generation += 1
        let check = generation
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            let result = try decoder.decode(Readiness.self, from: data)
            guard check == generation, !Task.isCancelled else { return }
            readiness = result
            readFailed = false
        } catch {
            if check == generation && !Task.isCancelled { readFailed = true }
        }
    }

    private func change(_ path: String) async {
        guard !busy, let url = URL(string: path, relativeTo: base) else { return }
        generation += 1
        busy = true
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.httpBody = Data()
        request.setValue("dashboard", forHTTPHeaderField: "X-Spliff-Action")
        request.setValue("http://127.0.0.1:\(base.port ?? 8765)", forHTTPHeaderField: "Origin")
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let result = try JSONDecoder().decode(Change.self, from: data)
            message = (response as? HTTPURLResponse)?.statusCode == 200 ?
                result.message ?? "Download permission saved." : result.error ?? "Could not save download permission."
        } catch {
            message = "Could not reach the Spliff dashboard. Try again."
        }
        busy = false
        await refresh()
    }
}

@MainActor
private final class LibraryPreference: ObservableObject {
    @Published var destination: String
    @Published var saved: String?
    @Published var status = ""
    @Published var busy = true
    private let path: String
    private let key: String

    init(path: String, key: String, initial: String) {
        self.path = path
        self.key = key
        destination = initial
    }

    func load(base: URL, save: Bool = false) async {
        busy = true
        defer { busy = false }
        status = ""
        guard let endpoint = URL(string: path, relativeTo: base)?.absoluteURL else { return }
        var request = URLRequest(url: endpoint)
        if save {
            request.url = endpoint.appending(queryItems: [URLQueryItem(name: key, value: destination)])
            request.httpMethod = "POST"
            request.setValue("dashboard", forHTTPHeaderField: "X-Spliff-Action")
            request.setValue("http://127.0.0.1:\(base.port ?? 8765)", forHTTPHeaderField: "Origin")
        }
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
            guard let value = try JSONDecoder().decode([String: String].self, from: data)[key] else {
                throw URLError(.cannotParseResponse)
            }
            destination = value
            saved = value
            if save { status = "Saved" }
        } catch {
            status = save ? "Couldn’t save the preference." : "Couldn’t load the preference."
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
    let model: CompanionModel

    func makeCoordinator() -> Coordinator { Coordinator(model: model) }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(context.coordinator, name: "spliffMac")
        let web = WKWebView(frame: .zero, configuration: configuration)
        web.navigationDelegate = context.coordinator
        web.load(URLRequest(url: url))
        return web
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
        weak var model: CompanionModel?

        init(model: CompanionModel) { self.model = model }

        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
            guard let model else { return }
            let url = model.dashboardURL
            guard
                  message.frameInfo.isMainFrame,
                  message.frameInfo.securityOrigin.protocol == url.scheme,
                  message.frameInfo.securityOrigin.host == url.host,
                  message.frameInfo.securityOrigin.port == url.port,
                  let command = message.body as? String else { return }
            if command == "reviewOpen" || command == "reviewClosed" {
                model.reviewOpen = command == "reviewOpen"
                return
            }
            guard command == "chooseMusic",
                  !model.busy, !model.connecting, model.runtimeAvailable else { return }
            model.chooseMusic()
        }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url, let origin = model?.dashboardURL else {
                decisionHandler(.cancel)
                return
            }
            if url.scheme == origin.scheme && url.host == origin.host && url.port == origin.port {
                decisionHandler(.allow)
            } else {
                if ["https", "http"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
                decisionHandler(.cancel)
            }
        }
    }
}
