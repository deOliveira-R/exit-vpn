import AppKit
import ExitControlCore
import SwiftUI

@main
struct ExitControlApp: App {
    @State private var model = AppModel()

    var body: some Scene {
        MenuBarExtra {
            MenuContent(model: model)
        } label: {
            MenuBarLabel(model: model)
        }
        .menuBarExtraStyle(.menu)

        Window("Servers", id: "servers") { ServerBrowser(model: model) }
            .defaultSize(width: 900, height: 560)
        Window("Find a Server for a URL", id: "find") { FindView(model: model) }
            .defaultSize(width: 640, height: 520)
        Window("ExitControl Settings", id: "settings") { SettingsView(model: model) }
            .windowResizability(.contentSize)
    }
}

/// A menu-bar app has no Dock icon, so its windows must pull the app forward.
@MainActor func bringToFront() { NSApp.activate() }

struct MenuBarLabel: View {
    let model: AppModel

    var body: some View {
        let st = model.status
        HStack(spacing: 3) {
            Image(systemName: symbol)
            if model.showFlag, model.online, let st {
                Text(flag(st.exitLoc ?? st.country.flatMap { model.codes[$0] }))
            }
        }
    }

    var symbol: String {
        guard model.online, let st = model.status else { return "shield.slash" }
        if model.busy != nil { return "arrow.triangle.2.circlepath" }
        switch st.health_ {
        case .healthy: return st.prefs.secureCore ? "lock.shield.fill" : "shield.lefthalf.filled"
        case .starting, .suspect, .failing_over: return "exclamationmark.shield"
        case .degraded: return "xmark.shield.fill"
        case .unknown: return "shield"
        }
    }
}
