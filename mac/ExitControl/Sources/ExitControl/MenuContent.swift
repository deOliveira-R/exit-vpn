import AppKit
import ExitControlCore
import SwiftUI

/// The menu (native NSMenu style, like Tailscale's).
struct MenuContent: View {
    let model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        statusSection
        Divider()
        actionsSection
        Divider()
        regionsSection
        Divider()
        Toggle("Secure Core", isOn: Binding(
            get: { model.status?.prefs.secureCore ?? false },
            set: { model.setSecureCore($0) }))
            .disabled(!ready)
        Toggle("Automatic Failover", isOn: Binding(
            get: { model.status?.prefs.autoFailover ?? true },
            set: { model.setAutoFailover($0) }))
            .disabled(!ready)
        Divider()
        Button("Server Browser…") { open("servers") }.keyboardShortcut("b")
        Button("Find Server for URL…") { open("find") }.keyboardShortcut("f")
        Button("Event Log…") { model.settingsTab = "events"; open("settings") }.keyboardShortcut("l")
        Button("Open Status JSON") { model.openStatusJSON() }.disabled(!model.online)
        Divider()
        Button("Settings…") { open("settings") }.keyboardShortcut(",")
        Button("Quit ExitControl") { NSApp.terminate(nil) }.keyboardShortcut("q")
    }

    var ready: Bool { model.online && model.busy == nil }

    func open(_ id: String) {
        openWindow(id: id)
        bringToFront()
    }

    @ViewBuilder var statusSection: some View {
        if let st = model.status {
            let where_ = [st.city, st.country].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: ", ")
            Text("\(flag(st.exitLoc)) \(st.server ?? "no server")  ·  \(where_)")
            if let entry = st.entry, !entry.isEmpty {
                Text("Secure Core: \(flag(entry)) \(entry) → \(flag(st.exitLoc)) \(st.exitLoc ?? "")")
            }
            Text("Exit IP \(st.exitIp ?? "—")" + (st.load.map { "  ·  load \($0)%" } ?? ""))
            if !model.online {
                Text("Offline: \(model.offlineReason ?? "controller unreachable")")
            } else {
                Text(st.health_.label)
            }
            if let p = st.profile, !p.isEmpty {
                Text("Keeping: \(p.map(featureName).joined(separator: ", "))")
            }
            ForEach(st.warnings ?? [], id: \.self) { Text("⚠️ \($0)") }
        } else {
            Text(model.online ? "Loading…" : "Offline: \(model.offlineReason ?? "connecting to \(model.controllerURL)")")
        }
        exitUseLine
        if let busy = model.busy { Text("⏳ \(busy)…") }
        if let err = model.lastError { Text("❌ \(err)") }
    }

    @ViewBuilder var exitUseLine: some View {
        switch model.exitUse {
        case .usingThis: Text("✓ \(model.exitUse.label)")
        case .unknown: EmptyView()
        default:
            Text("⚠️ \(model.exitUse.label)")
            Button("Choose the Exit Node in Tailscale…") {
                NSWorkspace.shared.open(URL(fileURLWithPath: "/Applications/Tailscale.app"))
            }
        }
    }

    @ViewBuilder var actionsSection: some View {
        Button("New Server, Same City") { model.rotate("city") }.disabled(!ready).keyboardShortcut("1")
        Button("New Server, Same Country") { model.rotate("country") }.disabled(!ready).keyboardShortcut("2")
        Button("New Server, Pinned Regions") { model.rotate("any") }.disabled(!ready).keyboardShortcut("3")
    }

    @ViewBuilder var regionsSection: some View {
        let regions = model.status?.prefs.regions ?? []
        Text("Pinned Regions")
        ForEach(regions) { r in
            Toggle("\(flag(code(r.country))) \(r.label)", isOn: Binding(
                get: { isCurrent(r) }, set: { _ in model.select(r) }))
                .disabled(!ready)
        }
        if !model.favorites.isEmpty {
            Menu("Favourites") {
                ForEach(model.favorites, id: \.self) { name in
                    let s = model.servers.first { $0.name.caseInsensitiveCompare(name) == .orderedSame }
                    Button("\(flag(s.flatMap(countryCode))) \(name)" + (s.map { "  ·  \($0.city)" } ?? "")) {
                        model.selectServer(named: name)
                    }.disabled(!ready)
                }
            }
        }
        Menu("More Countries") {
            if model.countries.isEmpty {
                Button("Load Countries") { Task { await model.loadServers() } }
            }
            let byContinent = Dictionary(grouping: model.countries) { continent(code($0)) }
            ForEach(continents + ["Other"], id: \.self) { name in
                if let cs = byContinent[name] {
                    Menu(name) {
                        ForEach(cs, id: \.self) { c in
                            Button("\(flag(code(c))) \(c)") { model.select(Region(country: c)) }.disabled(!ready)
                        }
                    }
                }
            }
        }
    }

    func isCurrent(_ r: Region) -> Bool {
        guard let st = model.status, st.country?.lowercased() == r.country.lowercased() else { return false }
        return r.city == nil || st.city?.lowercased() == r.city?.lowercased()
    }

    func code(_ country: String) -> String? { model.codes[country] }
}

func featureName(_ f: String) -> String {
    ["port_forward": "P2P", "stream": "Streaming", "ipv6": "IPv6", "secure_core": "Secure Core"][f] ?? f
}
