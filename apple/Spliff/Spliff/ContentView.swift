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
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button("Settings", systemImage: "gearshape") { model.settingsOpen = true }
                    .help("Settings")
            }
        }
        .sheet(isPresented: $model.settingsOpen) {
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
    @Environment(\.dismiss) private var dismiss
    @StateObject private var preference = ReplacementPreference()

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack {
                Text("Settings").font(.title2.bold())
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            if model.dashboardReady {
                VStack(alignment: .leading, spacing: 10) {
                    Text("After replacing an album").font(.headline)
                    Picker("Original files", selection: $preference.destination) {
                        Text("Move to Mac Trash").tag("trash")
                        Text("Move to hidden recovery folder").tag("hidden")
                    }.disabled(preference.saved == nil || preference.busy)
                    Text("Spliff uses this setting after you choose Replace and confirm where the files will go.")
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
                Divider()
            }
            DisclosureGroup("ChatGPT connection") { ConnectionView().padding(.top, 12) }
        }
        .padding(24)
        .frame(width: 480)
        .task(id: model.dashboardReady) {
            if model.dashboardReady { await preference.load(base: model.dashboardURL) }
        }
    }
}

@MainActor
private final class ReplacementPreference: ObservableObject {
    @Published var destination = "trash"
    @Published var saved: String?
    @Published var status = ""
    @Published var busy = true

    func load(base: URL, save: Bool = false) async {
        busy = true
        defer { busy = false }
        status = ""
        guard let endpoint = URL(string: "/api/replacement-preference", relativeTo: base)?.absoluteURL else { return }
        var request = URLRequest(url: endpoint)
        if save {
            request.url = endpoint.appending(queryItems: [URLQueryItem(name: "destination", value: destination)])
            request.httpMethod = "POST"
            request.setValue("dashboard", forHTTPHeaderField: "X-Spliff-Action")
            request.setValue("http://127.0.0.1:\(base.port ?? 8765)", forHTTPHeaderField: "Origin")
        }
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
            let value = try JSONDecoder().decode(Preference.self, from: data).destination
            destination = value
            saved = value
            if save { status = "Saved" }
        } catch {
            status = save ? "Couldn’t save the preference." : "Couldn’t load the preference."
        }
    }

    private struct Preference: Decodable { let destination: String }
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
                  message.body as? String == "chooseMusic",
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
