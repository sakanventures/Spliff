import SwiftUI

@main
struct SpliffApp: App {
    @NSApplicationDelegateAdaptor(SingleInstanceDelegate.self) private var delegate
    @StateObject private var companion = CompanionModel()

    var body: some Scene {
        Window("Spliff", id: "main") {
            ContentView().environmentObject(companion)
                .frame(minWidth: 760, minHeight: 540)
                .task { await companion.refresh() }
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1100, height: 760)
        MenuBarExtra("Spliff", systemImage: "waveform") {
            CompanionMenu().environmentObject(companion)
        }
    }
}

struct CompanionMenu: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.openWindow) var openWindow
    var body: some View {
        Text(model.connected ? "Connected" : "Connection needed")
        Button("Open Spliff") {
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        Button("Settings…") {
            model.settingsOpen = true
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        Divider()
        Button("Restart services") { Task { await model.restart() } }.disabled(model.busy || model.connecting)
        Button("Quit Spliff") { NSApp.terminate(nil) }
    }
}

final class SingleInstanceDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        if let identifier = Bundle.main.bundleIdentifier,
           let existing = NSRunningApplication.runningApplications(withBundleIdentifier: identifier)
            .first(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier && !$0.isTerminated }) {
            existing.activate(options: [.activateAllWindows])
            NSApp.terminate(nil)
        }
    }
}
