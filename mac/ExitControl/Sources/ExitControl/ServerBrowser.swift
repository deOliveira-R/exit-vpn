import ExitControlCore
import SwiftUI

/// The live catalog as a table: filter, then connect to one exact server.
struct ServerBrowser: View {
    let model: AppModel
    @State private var search = ""
    @State private var country = ""          // "" = all
    @State private var p2p = false
    @State private var streaming = false
    @State private var ipv6 = false
    @State private var hideDisabled = true
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

    var secureCore: Bool { model.status?.prefs.secureCore ?? false }

    var rows: [Row] {
        let q = search.lowercased()
        return model.servers.filter { s in
            s.secureCore == secureCore && !s.tor
                && (country.isEmpty || s.country == country)
                && (!p2p || s.portForward) && (!streaming || s.stream) && (!ipv6 || s.hasIPv6)
                && (!hideDisabled || s.isEnabled)
                && (q.isEmpty || s.name.lowercased().contains(q) || s.city.lowercased().contains(q)
                    || s.country.lowercased().contains(q) || s.ip.contains(q))
        }.map(Row.init).sorted { a, b in
            for c in sort {
                switch c.compare(a, b) {
                case .orderedAscending: return true
                case .orderedDescending: return false
                case .orderedSame: continue
                }
            }
            return Self.nearestFirst(a, b)
        }
    }

    /// Ties go by proximity, then load: country, city, load, name, IP. A total order, so
    /// rows never swap places when the table redraws.
    static func nearestFirst(_ a: Row, _ b: Row) -> Bool {
        let ka = (a.country, a.city, a.loadSort), kb = (b.country, b.city, b.loadSort)
        if ka != kb { return ka < kb }
        switch a.name.localizedStandardCompare(b.name) {
        case .orderedAscending: return true
        case .orderedDescending: return false
        case .orderedSame: return a.s.ip < b.s.ip
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Picker("Country", selection: $country) {
                    Text("All countries").tag("")
                    ForEach(model.countries, id: \.self) { Text("\(flag(model.codes[$0])) \($0)").tag($0) }
                }
                .frame(maxWidth: 260)
                Toggle("P2P", isOn: $p2p)
                Toggle("Streaming", isOn: $streaming)
                Toggle("IPv6", isOn: $ipv6)
                Toggle("Hide maintenance", isOn: $hideDisabled)
                Spacer()
                Text(secureCore ? "Secure Core servers (turn Secure Core off for regular ones)" : "Regular servers")
                    .foregroundStyle(.secondary).font(.caption)
            }
            .toggleStyle(.checkbox)
            .padding(10)
            let shown = rows
            Table(shown, selection: $selection, sortOrder: $sort) {
                TableColumn("★") { r in
                    Button { model.toggleFavorite(r.s.name) } label: {
                        Image(systemName: model.isFavorite(r.s.name) ? "star.fill" : "star")
                            .foregroundStyle(model.isFavorite(r.s.name) ? .yellow : .secondary)
                    }
                    .buttonStyle(.plain).help("Favourites are tried first in their city, country or region")
                }.width(18)
                TableColumn("Server", value: \.name) { r in
                    Text("\(flag(countryCode(r.s))) \(r.s.name)" + (model.isCurrent(r.s) ? "  ●" : ""))
                        .fontWeight(model.isCurrent(r.s) ? .semibold : .regular)
                        .help(model.isCurrent(r.s) ? "The exit's current server" : "")
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
                    Button("Connect to \(s.name)") { model.select(s) }
                    Button(model.isFavorite(s.name) ? "Remove from Favourites" : "Add to Favourites") { model.toggleFavorite(s.name) }
                }
            } primaryAction: { ids in
                if let s = server(ids.first) { model.select(s) }
            }
            HStack {
                Text("\(shown.count) of \(model.servers.count) servers").foregroundStyle(.secondary)
                if let busy = model.busy { ProgressView().controlSize(.small); Text(busy) }
                if let e = model.lastError { Text(e).foregroundStyle(.red).lineLimit(1) }
                Spacer()
                Button("Refresh") { Task { await model.loadServers() } }
                Button("Connect") { if let s = server(selection) { model.select(s) } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(server(selection) == nil || model.busy != nil || !(server(selection)?.isEnabled ?? false))
            }
            .padding(10)
        }
        .searchable(text: $search, placement: .toolbar, prompt: "Name, city, country or IP")
        .task { await model.loadServers() }
    }

    func server(_ id: Server.ID?) -> Server? { id.flatMap { id in model.servers.first { $0.id == id } } }
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
