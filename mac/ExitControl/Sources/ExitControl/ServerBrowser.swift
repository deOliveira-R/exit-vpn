import ExitControlCore
import SwiftUI

/// The live catalog as a table: filter, then connect to one exact server (failover then
/// keeps its features: P2P stays P2P). Filters and order are `ServerQuery`.
struct ServerBrowser: View {
    let app: AppContainer
    @State private var query = ServerQuery()
    @State private var selection: Server.ID?
    @State private var sort = [KeyPathComparator(\Row.country)]

    struct Row: Identifiable {
        let s: Server
        var id: String { s.id }
        var loadSort: Int { s.load ?? 50 }
        var name: String { s.name }
        var city: String { s.city }
        var country: String { s.country }
    }

    var rows: [Row] {
        var q = query
        q.secureCore = app.connection.status?.prefs.secureCore ?? false
        return app.catalog.servers.filter(q.matches).map(Row.init).sorted { a, b in
            for c in sort {
                switch c.compare(a, b) {
                case .orderedAscending: return true
                case .orderedDescending: return false
                case .orderedSame: continue
                }
            }
            return ServerQuery.nearestFirst(a.s, b.s)
        }
    }

    var body: some View {
        let secureCore = app.connection.status?.prefs.secureCore ?? false
        VStack(spacing: 0) {
            HStack {
                Picker("Country", selection: $query.country) {
                    Text("All countries").tag("")
                    ForEach(app.catalog.countries, id: \.self) { Text("\(flag(app.catalog.codes[$0])) \($0)").tag($0) }
                }
                .frame(maxWidth: 260)
                Toggle("P2P", isOn: $query.p2p)
                Toggle("Streaming", isOn: $query.streaming)
                Toggle("IPv6", isOn: $query.ipv6)
                Toggle("Hide maintenance", isOn: $query.hideMaintenance)
                Spacer()
                Text(secureCore ? "Secure Core servers (turn Secure Core off for regular ones)" : "Regular servers")
                    .foregroundStyle(.secondary).font(.caption)
            }
            .toggleStyle(.checkbox)
            .padding(10)
            let shown = rows
            Table(shown, selection: $selection, sortOrder: $sort) {
                TableColumn("★") { r in
                    Button { app.toggleFavorite(r.s.name) } label: {
                        Image(systemName: app.isFavorite(r.s.name) ? "star.fill" : "star")
                            .foregroundStyle(app.isFavorite(r.s.name) ? .yellow : .secondary)
                    }
                    .buttonStyle(.plain).help("Favourites are tried first in their city, country or region")
                }.width(18)
                TableColumn("Server", value: \.name) { r in
                    let current = isCurrent(r.s)
                    Text("\(flag(countryCode(r.s))) \(r.s.name)" + (current ? "  ●" : ""))
                        .fontWeight(current ? .semibold : .regular)
                        .help(current ? "The exit's current server" : "")
                }
                TableColumn("Country", value: \.country)
                TableColumn("City", value: \.city)
                TableColumn("Load", value: \.loadSort) { r in
                    if let l = r.s.load { LoadBar(load: l) } else { Text("—") }
                }.width(120)
                TableColumn("Features") { r in Text(r.s.badges.joined(separator: " · ")).foregroundStyle(.secondary) }
                TableColumn("IP") { r in Text(r.s.ip).monospaced().foregroundStyle(r.s.isEnabled ? .primary : .secondary) }
            }
            .contextMenu(forSelectionType: Server.ID.self) { ids in
                if let s = server(ids.first) {
                    Button("Connect to \(s.name)") { connect(s) }
                    Button(app.isFavorite(s.name) ? "Remove from Favourites" : "Add to Favourites") { app.toggleFavorite(s.name) }
                }
            } primaryAction: { ids in
                if let s = server(ids.first) { connect(s) }
            }
            HStack {
                Text("\(shown.count) of \(app.catalog.servers.count) servers").foregroundStyle(.secondary)
                if let busy = app.runner.busy { ProgressView().controlSize(.small); Text(busy) }
                if let e = app.runner.lastError ?? app.catalog.error { Text(e).foregroundStyle(.red).lineLimit(1) }
                Spacer()
                Button("Refresh") { Task { await app.catalog.load() } }
                Button("Connect") { if let s = server(selection) { connect(s) } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(server(selection) == nil || !app.ready || !(server(selection)?.isEnabled ?? false))
            }
            .padding(10)
        }
        .searchable(text: $query.search, placement: .toolbar, prompt: "Name, city, country or IP")
        .task { await app.catalog.loadIfEmpty() }
    }

    func connect(_ s: Server) { app.run(.connectServer(name: s.name, ip: s.ip)) }

    func isCurrent(_ s: Server) -> Bool {
        app.connection.status?.server == s.name && app.connection.status?.endpointIP == s.ip
    }

    func server(_ id: Server.ID?) -> Server? { id.flatMap { id in app.catalog.servers.first { $0.id == id } } }
}

/// A static load bar. Not a ProgressView: the table reuses row views while scrolling, and a
/// ProgressView animates from the previous row's value, so loads seemed to change on screen.
struct LoadBar: View {
    let load: Int

    var body: some View {
        HStack(spacing: 6) {
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                Capsule().fill(load < 60 ? Color.green : load < 85 ? .orange : .red)
                    .frame(width: max(4, 60 * CGFloat(min(load, 100)) / 100))
            }
            .frame(width: 60, height: 6)
            Text("\(load)%").monospacedDigit()
        }
        .transaction { $0.animation = nil }
    }
}
