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
    @State private var sort = [KeyPathComparator(\Row.loadSort)]

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
        }.map(Row.init).sorted(using: sort)
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
                TableColumn("") { r in Text(model.isCurrent(r.s) ? "●" : "") }.width(14)
                TableColumn("Server", value: \.name) { r in
                    Text("\(flag(countryCode(r.s))) \(r.s.name)")
                }
                TableColumn("City", value: \.city)
                TableColumn("Country", value: \.country)
                TableColumn("Load", value: \.loadSort) { r in
                    if let l = r.s.load {
                        HStack { ProgressView(value: Double(l), total: 100).tint(l < 60 ? .green : l < 85 ? .orange : .red); Text("\(l)%").monospacedDigit() }
                    } else { Text("—") }
                }.width(120)
                TableColumn("Features") { r in Text(r.s.badges.joined(separator: " · ")).foregroundStyle(.secondary) }
                TableColumn("IP") { r in Text(r.s.ip).monospaced().foregroundStyle(r.s.isEnabled ? .primary : .secondary) }
            }
            .contextMenu(forSelectionType: Server.ID.self) { ids in
                if let s = server(ids.first) { Button("Connect to \(s.name)") { model.select(s) } }
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
