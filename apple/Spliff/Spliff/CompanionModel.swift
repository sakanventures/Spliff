import AppKit
import Combine
import Foundation

struct ServiceReport: Decodable {
    struct Ready: Decodable { let dashboard: Bool; let tunnel: Bool }
    let ready: Ready
}

struct CommandResult: Sendable {
    let code: Int32
    let output: String
    let error: String
}

@MainActor
final class CompanionModel: ObservableObject {
    @Published var settingsOpen = false
    @Published var dashboardReady = false
    @Published var connected = false
    @Published var busy = false
    @Published var connecting = false
    @Published var message = ""
    @Published var runtimeKey = ""
    @Published var project: URL
    @Published var revision = UUID()
    private var checking = false
    private var port = 8765

    init() {
        let saved = UserDefaults.standard.string(forKey: "projectRoot")
        let built = Bundle.main.object(forInfoDictionaryKey: "SpliffProjectRoot") as? String
        project = URL(fileURLWithPath: saved ?? built ?? FileManager.default.homeDirectoryForCurrentUser.path)
            .standardizedFileURL
    }

    var dashboardURL: URL { URL(string: "http://127.0.0.1:\(port)/?embedded=1")! }
    var runtimeAvailable: Bool {
        FileManager.default.fileExists(atPath: project.appendingPathComponent(".venv/bin/python").path)
        && FileManager.default.fileExists(atPath: project.appendingPathComponent(".venv/bin/tunnel-client").path)
    }
    private var database: URL {
        let working = project.appendingPathComponent("artist-workflow.sqlite")
        if FileManager.default.fileExists(atPath: working.path) { return working }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Spliff/catalog.sqlite")
    }

    // Drain both pipes concurrently so a long scan cannot block on stderr.
    nonisolated static func run(project: URL, arguments: [String], key: String = "") async -> CommandResult {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = project.appendingPathComponent(".venv/bin/python")
                process.arguments = ["-m", "spliff"] + arguments
                process.currentDirectoryURL = project
                var env = ProcessInfo.processInfo.environment
                env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
                env["PYTHONPATH"] = project.path
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                if !key.isEmpty { env["CONTROL_PLANE_API_KEY"] = key }
                process.environment = env
                let out = Pipe(), err = Pipe()
                process.standardOutput = out
                process.standardError = err
                do {
                    try process.run()
                    let group = DispatchGroup()
                    let errors = DataBox()
                    group.enter()
                    DispatchQueue.global().async {
                        errors.value = err.fileHandleForReading.readDataToEndOfFile()
                        group.leave()
                    }
                    let data = out.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit()
                    group.wait()
                    continuation.resume(returning: CommandResult(code: process.terminationStatus,
                        output: String(decoding: data, as: UTF8.self),
                        error: String(decoding: errors.value, as: UTF8.self)))
                } catch {
                    continuation.resume(returning: CommandResult(code: -1, output: "", error: error.localizedDescription))
                }
            }
        }
    }

    func refresh() async {
        guard runtimeAvailable, !checking, !busy else { return }
        checking = true
        defer { checking = false }
        let settings = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Spliff/Services/settings.json")
        if let data = try? Data(contentsOf: settings),
           let saved = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let configuredPort = saved["port"] as? Int { port = configuredPort }
        let result = await Self.run(project: project, arguments: ["services", "status"])
        guard result.code == 0, let data = result.output.data(using: .utf8),
              let report = try? JSONDecoder().decode(ServiceReport.self, from: data) else {
            dashboardReady = false; connected = false
            message = result.error.isEmpty ? "Unable to check the connection." : result.error
            return
        }
        let wasReady = dashboardReady
        dashboardReady = report.ready.dashboard
        connected = report.ready.tunnel
        if dashboardReady && !wasReady { revision = UUID() }
    }

    func install(key: String) async {
        guard !busy, !connecting else { return }
        connecting = true
        defer { connecting = false }
        busy = true; message = "Connecting…"
        let result = await Self.run(project: project, arguments: ["--db", database.path,
            "services", "install", "--no-input", "--takeover"] + (key.isEmpty ? [] : ["--replace-key"]), key: key)
        busy = false
        if result.code != 0 { message = result.error; return }
        message = "Starting services…"
        // Initial control-plane polling may take a little time.
        for _ in 0..<12 {
            await refresh()
            if dashboardReady && connected { break }
            try? await Task.sleep(for: .seconds(3))
        }
        message = connected ? "Connected. Refresh Spliff’s tools in ChatGPT." : "Services installed. Check the connection again shortly."
    }

    func restart() async {
        guard !busy, !connecting else { return }
        busy = true
        let result = await Self.run(project: project, arguments: ["services", "restart"])
        busy = false
        message = result.code == 0 ? "Restarting…" : result.error
        await refresh()
        if result.code == 0 && connected { message = "" }
        revision = UUID()
    }

    func chooseProject() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.prompt = "Choose Spliff"
        if panel.runModal() == .OK, let url = panel.url {
            project = url
            UserDefaults.standard.set(url.path, forKey: "projectRoot")
            message = ""
            Task { await refresh() }
        }
    }

    func chooseMusic() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.prompt = "Choose music"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task {
            busy = true; message = "Syncing music…"
            // Follow the active service catalog if it differs from this checkout's default.
            var db = database.path
            let settings = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Spliff/Services/settings.json")
            if let data = try? Data(contentsOf: settings), let saved = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let value = saved["db"] as? String { db = value }
            let result = await Self.run(project: project, arguments: ["--db", db, "sync", url.path])
            busy = false
            message = result.code == 0 ? "Folder sync started." : result.error
            if result.code == 0 { revision = UUID() }
        }
    }
}

// Access is synchronized by DispatchGroup completion before the value is read.
private final class DataBox: @unchecked Sendable { var value = Data() }
