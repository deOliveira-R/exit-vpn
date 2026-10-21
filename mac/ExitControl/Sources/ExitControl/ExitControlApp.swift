import AppKit
import ExitControlCore
import SwiftUI

@main
struct ExitControlApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var app = AppContainer.shared

    var body: some Scene {
        MenuBarExtra {
            MenuContent(app: app)
        } label: {
            MenuBarLabel(app: app)
        }
        .menuBarExtraStyle(.menu)

        Window("Servers", id: AppRoute.servers.windowID) { ServerBrowser(app: app) }
            .defaultSize(width: 900, height: 560)
        Window("Find a Server for a URL", id: AppRoute.find.windowID) { FindView(app: app) }
            .defaultSize(width: 640, height: 520)
        Window("ExitControl Settings", id: AppRoute.settings(.general).windowID) { SettingsView(app: app) }
            .windowResizability(.contentSize)
    }
}

/// `exitvpn://` URLs arrive as Apple Events. Handling them here (instead of SwiftUI's
/// per-scene matching) lets action URLs run without opening a window.
@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationWillFinishLaunching(_ notification: Notification) {
        NSAppleEventManager.shared().setEventHandler(
            self, andSelector: #selector(handleURL(_:reply:)),
            forEventClass: AEEventClass(kInternetEventClass), andEventID: AEEventID(kAEGetURL))
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        AppContainer.shared.updater.start()   // Sparkle's scheduled checks
    }

    @objc func handleURL(_ event: NSAppleEventDescriptor, reply: NSAppleEventDescriptor) {
        guard let s = event.paramDescriptor(forKeyword: keyDirectObject)?.stringValue,
              let url = URL(string: s) else { return }
        AppContainer.shared.open(url)
    }
}

/// A menu-bar app has no Dock icon, so its windows must pull the app forward.
@MainActor func bringToFront() { NSApp.activate() }

/// The menu-bar icon (shield by health, optional flag). It is always on screen, so it also
/// opens the windows the router asks for.
struct MenuBarLabel: View {
    let app: AppContainer
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        let st = app.connection.status
        HStack(spacing: 3) {
            Image(systemName: StatusText.symbol(online: app.connection.online, status: st,
                                                busy: app.runner.busy != nil))
            if app.settings.showFlag, app.connection.online, let st {
                Text(flag(st.exitLoc ?? st.country.flatMap { app.catalog.codes[$0] }))
            }
        }
        .onChange(of: app.router.request) { _, r in
            if let r { openWindow(id: r.route.windowID); bringToFront() }
        }
    }
}
