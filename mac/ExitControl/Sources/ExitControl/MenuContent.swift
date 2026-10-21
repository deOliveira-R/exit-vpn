import AppKit
import ExitControlCore
import SwiftUI

/// The menu (native NSMenu style, like Tailscale's): where the exit is, what to change.
struct MenuContent: View {
    let app: AppContainer
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        statusSection
        Divider()
        ForEach(Array(RotationScope.allCases.enumerated()), id: \.element) { i, scope in
            Button("New Server, \(scope.label)") { app.run(.rotate(scope)) }
                .disabled(!app.ready).keyboardShortcut(KeyEquivalent(Character("\(i + 1)")))
        }
        Divider()
        regionsSection
        Divider()
        Toggle("Secure Core", isOn: Binding(
            get: { app.connection.status?.prefs.secureCore ?? false },
            set: { app.run(.secureCore($0)) }))
            .disabled(!app.ready)
        Toggle("Automatic Failover", isOn: Binding(
            get: { app.connection.status?.prefs.autoFailover ?? true },
            set: { app.run(.autoFailover($0)) }))
            .disabled(!app.ready)
        Divider()
        Button("Server Browser…") { open(.servers) }.keyboardShortcut("b")
        Button("Find Server for URL…") { open(.find) }.keyboardShortcut("f")
        Button("Event Log…") { open(.settings(.events)) }.keyboardShortcut("l")
        Button("Open Status JSON") { app.openStatusJSON() }.disabled(!app.connection.online)
        Divider()
        Button("Settings…") { open(.settings(.general)) }.keyboardShortcut(",")
        Button("Check for Updates…") { app.updater.check() }.disabled(!app.updater.canCheck)
        Text("ExitControl \(app.updater.version)")
        Button("Quit ExitControl") { NSApp.terminate(nil) }.keyboardShortcut("q")
    }

    func open(_ route: AppRoute) {
        if case .settings(let tab) = route { app.router.settingsTab = tab }
        openWindow(id: route.windowID)
        bringToFront()
    }

    @ViewBuilder var statusSection: some View {
        let c = app.connection
        if let st = c.status {
            Text("\(flag(st.exitLoc)) \(st.server ?? "no server")  ·  \(st.place)")
            if let entry = st.entry, !entry.isEmpty {
                Text("Secure Core: \(flag(entry)) \(entry) → \(flag(st.exitLoc)) \(st.exitLoc ?? "")")
            }
            Text("Exit IP \(st.exitIp ?? "—")" + (st.load.map { "  ·  load \($0)%" } ?? ""))
            Text(c.online ? st.health.label : "Offline: \(c.offlineReason ?? "controller unreachable")")
            if let p = st.profile, !p.isEmpty {
                Text("Keeping: \(p.map(Feature.name).joined(separator: ", "))")
            }
            ForEach(st.warnings ?? [], id: \.self) { Text("⚠️ \($0)") }
        } else {
            Text(c.online ? "Loading…" : "Offline: \(c.offlineReason ?? "connecting to \(app.settings.controllerURL)")")
        }
        if let why = c.incompatible { Text("⚠️ This controller's status can't be read; update ExitControl or the exit node (\(why.prefix(80)))") }
        exitUseLine
        if let busy = app.runner.busy { Text("⏳ \(busy)…") }
        if let err = app.runner.lastError { Text("❌ \(err)") }
    }

    @ViewBuilder var exitUseLine: some View {
        let use = app.exitNode.use
        switch use {
        case .usingThis: Text("✓ \(use.label)")
        case .unknown: EmptyView()
        default:
            Text("⚠️ \(use.label)")
            if let failure = app.exitGuard.lastFailure { Text("❌ Re-selecting it failed: \(failure)") }
            Button("Open Tailscale…") {
                NSWorkspace.shared.open(URL(fileURLWithPath: "/Applications/Tailscale.app"))
            }
        }
        guardLine
    }

    /// The exit node guard's pause: direct traffic for a captive portal, then the exit node again.
    @ViewBuilder var guardLine: some View {
        if app.settings.keepExitNode {
            if let until = app.exitGuard.pausedUntil {
                Text("⏸ Direct until \(until.formatted(date: .omitted, time: .shortened)), then the exit node again")
                Button("Use the Exit Node Now") { app.exitGuard.resume() }
            } else {
                Button("Go Direct for 10 Minutes (Captive Portal)") { app.goDirect(for: 600) }
            }
        }
    }

    @ViewBuilder var regionsSection: some View {
        let st = app.connection.status
        Text("Pinned Regions")
        ForEach(st?.prefs.regions ?? []) { r in
            Toggle("\(flag(code(r.country))) \(r.label)", isOn: Binding(
                get: { StatusText.isCurrent(r, st) }, set: { _ in app.run(.connect(r)) }))
                .disabled(!app.ready)
        }
        if let favorites = st?.prefs.favorites, !favorites.isEmpty {
            Menu("Favourites") {
                ForEach(favorites, id: \.self) { name in
                    let s = app.catalog.server(named: name)
                    Button("\(flag(s.flatMap(countryCode))) \(name)" + (s.map { "  ·  \($0.city)" } ?? "")) {
                        app.run(.connectServer(name: name, ip: nil))
                    }.disabled(!app.ready)
                }
            }
        }
        Menu("More Countries") {
            if app.catalog.countries.isEmpty {
                Button("Load Countries") { Task { await app.catalog.load() } }
            }
            ForEach(app.catalog.byContinent, id: \.continent) { group in
                Menu(group.continent) {
                    ForEach(group.countries, id: \.self) { c in
                        Button("\(flag(code(c))) \(c)") { app.run(.connect(Region(country: c))) }.disabled(!app.ready)
                    }
                }
            }
        }
    }

    func code(_ country: String) -> String? { app.catalog.codes[country] }
}
