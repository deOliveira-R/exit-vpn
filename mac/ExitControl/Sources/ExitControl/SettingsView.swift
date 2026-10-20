import ExitControlCore
import SwiftUI

struct SettingsView: View {
    let model: AppModel
    @State private var tab = "general"

    var body: some View {
        TabView(selection: $tab) {
            GeneralTab(model: model).tabItem { Label("General", systemImage: "gearshape") }.tag("general")
            RegionsTab(model: model).tabItem { Label("Regions", systemImage: "pin") }.tag("regions")
            ConnectionTab(model: model).tabItem { Label("Connection", systemImage: "slider.horizontal.3") }.tag("connection")
            AccountTab(model: model).tabItem { Label("Account", systemImage: "person.badge.key") }.tag("account")
            LogTab(model: model).tabItem { Label("Events", systemImage: "list.bullet.rectangle") }.tag("events")
        }
        .frame(width: 620, height: 460)
        .task { await model.loadProvider() }
        .onOpenURL { url in  // exitvpn://settings/account
            if let t = url.pathComponents.dropFirst().first { tab = t }
            bringToFront()
        }
    }
}

struct GeneralTab: View {
    @Bindable var model: AppModel
    @State private var url = ""

    var body: some View {
        Form {
            Section("Exit node") {
                HStack {
                    TextField("Controller URL", text: $url)
                    Button("Apply") { model.controllerURL = url }.disabled(url == model.controllerURL)
                }
                LabeledContent("Connection", value: model.online ? "Online" : "Offline: \(model.offlineReason ?? "")")
                LabeledContent("This Mac", value: model.exitUse.label)
                Text("Whether this Mac routes through the exit node is chosen in the Tailscale app; ExitControl chooses which VPN server the exit node uses, for every device routed through it.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("exitctl (provider actions)") {
                TextField("Path", text: $model.exitctlPath, prompt: Text("~/.local/bin, Homebrew or /usr/local/bin"))
                LabeledContent("Found", value: model.exitctl?.executable.path ?? "not found: uv tool install ./helper")
            }
            Section("App") {
                Toggle("Show the exit country's flag in the menu bar", isOn: $model.showFlag)
                Toggle("Notifications (failover, degraded, key expiry)", isOn: $model.notify)
                Toggle("Launch at login", isOn: Binding(get: { model.launchAtLogin }, set: { model.launchAtLogin = $0 }))
            }
        }
        .formStyle(.grouped)
        .onAppear { url = model.controllerURL }
    }
}

/// Pinned regions in priority order: failover and "New Server, Pinned Regions" walk them.
struct RegionsTab: View {
    let model: AppModel
    @State private var regions: [Region] = []
    @State private var country = ""
    @State private var city = ""
    @State private var selection: Region.ID?

    var cities: [String] { Array(Set(model.servers.filter { $0.country == country && !$0.secureCore }.map(\.city))).sorted() }
    var changed: Bool { regions != (model.status?.prefs.regions ?? []) }

    var body: some View {
        VStack(alignment: .leading) {
            Text("Failover tries the same city, then the same country, then these regions in order. Drag to reorder.")
                .font(.caption).foregroundStyle(.secondary)
            List(selection: $selection) {
                ForEach(regions) { r in
                    Text("\(flag(model.codes[r.country])) \(r.label)").tag(r.id)
                }
                .onMove { regions.move(fromOffsets: $0, toOffset: $1) }
                .onDelete { regions.remove(atOffsets: $0) }
            }
            .frame(minHeight: 200)
            HStack {
                Picker("Country", selection: $country) {
                    Text("Choose…").tag("")
                    ForEach(model.countries, id: \.self) { Text("\(flag(model.codes[$0])) \($0)").tag($0) }
                }
                Picker("City", selection: $city) {
                    Text("Any city").tag("")
                    ForEach(cities, id: \.self) { Text($0).tag($0) }
                }
                .disabled(country.isEmpty)
                Button("Add") {
                    let r = Region(country: country, city: city.isEmpty ? nil : city)
                    if !regions.contains(r) { regions.append(r) }
                }
                .disabled(country.isEmpty)
            }
            HStack {
                Button("Remove") { regions.removeAll { $0.id == selection } }.disabled(selection == nil || regions.count < 2)
                Spacer()
                Button("Revert") { regions = model.status?.prefs.regions ?? [] }.disabled(!changed)
                Button("Save") { model.setRegions(regions) }.keyboardShortcut(.defaultAction)
                    .disabled(!changed || regions.isEmpty || model.busy != nil)
            }
            if let e = model.lastError { Text(e).foregroundStyle(.red).font(.caption) }
        }
        .padding()
        .onAppear { regions = model.status?.prefs.regions ?? [] }
        .onChange(of: model.status?.prefs.regions ?? []) { _, new in if !changed { regions = new } }
        .onChange(of: country) { city = "" }
    }
}

/// Provider levers (Proton: NetShield, VPN Accelerator, Moderate NAT, NAT-PMP). They are
/// bound to the key (D17), so applying them makes a new key for the exit slot.
struct ConnectionTab: View {
    let model: AppModel
    @State private var values: [String: String] = [:]

    var body: some View {
        Form {
            Section {
                ForEach(model.options) { o in
                    if o.kind == "bool" {
                        Toggle(o.label, isOn: Binding(get: { values[o.key] == "on" || values[o.key] == "true" },
                                                      set: { values[o.key] = $0 ? "on" : "off" }))
                    } else {
                        Picker(o.label, selection: Binding(get: { values[o.key] ?? "" }, set: { values[o.key] = $0 })) {
                            ForEach(o.sortedChoices, id: \.value) { Text("\($0.value): \($0.label)").tag($0.value) }
                        }
                    }
                    if !o.help.isEmpty { Text(o.help).font(.caption).foregroundStyle(.secondary) }
                }
            } header: {
                Text("\(model.provider.capitalized) options for the exit")
            } footer: {
                Text("Applying makes a new key with these options and swaps it in (about 10 s; traffic pauses ~1–2 s). The controller keeps the old key if the new one does not work.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                if let b = model.busy { ProgressView().controlSize(.small); Text(b) }
                if let e = model.providerError { Text(e).foregroundStyle(.red).font(.caption) }
                Spacer()
                Button("Revert") { values = model.currentOptions() }.disabled(values == model.currentOptions())
                Button("Apply") {
                    let v = values
                    model.provider("New key with options") { x, p in try await x.rotate(p, slot: "a", options: v) }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(values == model.currentOptions() || model.busy != nil || model.account?.isLoggedIn != true)
            }
            if model.account?.isLoggedIn != true { Text("Log in under Account first.").font(.caption) }
        }
        .formStyle(.grouped)
        .onAppear { values = model.currentOptions() }
        .onChange(of: model.keys) { values = model.currentOptions() }
        .onChange(of: model.options) { values = model.currentOptions() }
    }
}

struct AccountTab: View {
    let model: AppModel
    @State private var showLogin = false

    var body: some View {
        Form {
            Section("\(model.provider.capitalized) account") {
                if let a = model.account, a.isLoggedIn {
                    LabeledContent("Logged in as", value: a.account ?? "?")
                    Button("Log Out") { model.provider("Log out") { x, p in try await x.logout(p) } }
                } else {
                    Text(model.account?.needs2fa == true ? "Login waiting for a 2FA code" : "Not logged in")
                    Button("Log In…") { showLogin = true }
                }
                Text("The session stays in this Mac's Keychain; it never goes to the exit node.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Keys on the exit node") {
                ForEach(model.keys) { k in
                    HStack {
                        VStack(alignment: .leading) {
                            Text("Slot \(k.slot) (\(k.role)): \(k.deviceName ?? k.source)")
                            Text(k.options.map { "\($0.key)=\($0.value)" }.sorted().joined(separator: ", "))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text(k.expiresInDays.map { "expires in \(Int($0)) days" } ?? "expiry unknown")
                            .foregroundStyle((k.expiresInDays ?? 99) < 14 ? .red : .secondary)
                        Button("Renew") { model.provider("Renew slot \(k.slot)") { x, p in try await x.renew(p, slot: k.slot) } }
                        Button("New Key") {
                            model.provider("New key, slot \(k.slot)") { x, p in try await x.rotate(p, slot: k.slot, options: [:]) }
                        }
                    }
                    .disabled(model.busy != nil || model.account?.isLoggedIn != true)
                }
            }
            Section("Certificates on the account") {
                ForEach(model.certificates, id: \.stableID) { c in
                    HStack {
                        Text(c.deviceName.isEmpty ? "(unnamed)" : c.deviceName)
                        if let s = c.slot { Text("slot \(s)").foregroundStyle(.green) }
                        if c.orphan == true { Text("unused").foregroundStyle(.orange) }
                        Spacer()
                        if let e = c.expiresAt { Text(Date(timeIntervalSince1970: e), style: .date).foregroundStyle(.secondary) }
                        if c.slot == nil, let id = c.id {
                            Button("Delete") { model.provider("Delete certificate") { x, p in try await x.delete(p, id: id) } }
                                .disabled(model.busy != nil)
                        }
                    }
                }
            }
            Section("Server list") {
                if let cat = model.status?.catalog {
                    LabeledContent("Source", value: cat.pushed ? "live from \(model.provider.capitalized)" : "pinned fallback")
                    LabeledContent("Updated", value: Date(timeIntervalSince1970: cat.fetchedAt).formatted(.relative(presentation: .named)))
                    LabeledContent("Servers", value: "\(cat.enabled ?? cat.servers) of \(cat.servers) up")
                }
                HStack {
                    Button("Push Now") { model.provider("Push server list") { x, p in try await x.pushServers(p) } }
                    Button("Revert to Pinned") { model.provider("Revert server list") { x, p in try await x.revertServers(p) } }
                }
                .disabled(model.busy != nil)
                Toggle("Push hourly in the background (launchd agent)", isOn: Binding(
                    get: { model.agentInstalled }, set: { model.setAgent($0) }))
            }
            if let b = model.busy { HStack { ProgressView().controlSize(.small); Text(b) } }
            if let e = model.providerError { Text(e).foregroundStyle(.red) }
        }
        .formStyle(.grouped)
        .sheet(isPresented: $showLogin) { LoginSheet(model: model) }
    }
}

struct LogTab: View {
    let model: AppModel
    var body: some View {
        List(model.log.reversed(), id: \.self) { e in
            HStack(alignment: .top) {
                Text(Date(timeIntervalSince1970: e.ts), format: .dateTime.month().day().hour().minute().second())
                    .monospacedDigit().foregroundStyle(.secondary)
                Text(e.summary)
            }
        }
    }
}
