import ExitControlCore
import SwiftUI

struct SettingsView: View {
    let app: AppContainer

    var body: some View {
        @Bindable var router = app.router
        TabView(selection: $router.settingsTab) {
            GeneralTab(app: app).tabItem { Label("General", systemImage: "gearshape") }.tag(SettingsTab.general)
            RegionsTab(app: app).tabItem { Label("Regions", systemImage: "pin") }.tag(SettingsTab.regions)
            ConnectionTab(app: app).tabItem { Label("Connection", systemImage: "slider.horizontal.3") }.tag(SettingsTab.connection)
            AccountTab(app: app).tabItem { Label("Account", systemImage: "person.badge.key") }.tag(SettingsTab.account)
            LogTab(app: app).tabItem { Label("Events", systemImage: "list.bullet.rectangle") }.tag(SettingsTab.events)
        }
        .frame(width: 620, height: 460)
        .task { await app.provider.load() }
    }
}

struct GeneralTab: View {
    let app: AppContainer
    @State private var url = ""

    var body: some View {
        @Bindable var settings = app.settings
        Form {
            Section("Exit node") {
                HStack {
                    TextField("Controller URL", text: $url)
                    Button("Apply") { settings.controllerURL = url }.disabled(url == settings.controllerURL)
                }
                LabeledContent("Connection", value: app.connection.online ? "Online" : "Offline: \(app.connection.offlineReason ?? "")")
                LabeledContent("This Mac", value: app.exitNode.use.label)
                Text("Whether this Mac routes through the exit node is chosen in the Tailscale app; ExitControl chooses which VPN server the exit node uses, for every device routed through it.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("exitctl (provider actions)") {
                TextField("Path", text: $settings.exitctlPath, prompt: Text("~/.local/bin, Homebrew or /usr/local/bin"))
                LabeledContent("Found", value: settings.exitctl?.executable.path ?? "not found: uv tool install ./helper")
            }
            Section("App") {
                Toggle("Show the exit country's flag in the menu bar", isOn: $settings.showFlag)
                Toggle("Notifications (failover, degraded, key expiry)", isOn: $settings.notify)
                Toggle("Launch at login", isOn: Binding(get: { app.launchAtLogin }, set: { app.setLaunchAtLogin($0) }))
                Toggle("Check for updates automatically", isOn: Binding(
                    get: { app.updater.automaticChecks }, set: { app.updater.setAutomaticChecks($0) }))
                LabeledContent("Version", value: app.updater.version)
                if let e = app.agentError { Text(e).foregroundStyle(.red).font(.caption) }
            }
        }
        .formStyle(.grouped)
        .onAppear { url = settings.controllerURL }
    }
}

/// Pinned regions in priority order: failover and "New Server, Pinned Regions" walk them.
struct RegionsTab: View {
    let app: AppContainer
    @State private var regions: [Region] = []
    @State private var country = ""
    @State private var city = ""
    @State private var selection: Region.ID?

    var saved: [Region] { app.connection.status?.prefs.regions ?? [] }
    var changed: Bool { regions != saved }

    var body: some View {
        VStack(alignment: .leading) {
            Text("Failover tries the same city, then the same country, then these regions in order. Drag to reorder.")
                .font(.caption).foregroundStyle(.secondary)
            List(selection: $selection) {
                ForEach(regions) { r in
                    Text("\(flag(app.catalog.codes[r.country])) \(r.label)").tag(r.id)
                }
                .onMove { regions.move(fromOffsets: $0, toOffset: $1) }
                .onDelete { regions.remove(atOffsets: $0) }
            }
            .frame(minHeight: 200)
            HStack {
                Picker("Country", selection: $country) {
                    Text("Choose…").tag("")
                    ForEach(app.catalog.countries, id: \.self) { Text("\(flag(app.catalog.codes[$0])) \($0)").tag($0) }
                }
                Picker("City", selection: $city) {
                    Text("Any city").tag("")
                    ForEach(app.catalog.cities(in: country), id: \.self) { Text($0).tag($0) }
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
                Button("Revert") { regions = saved }.disabled(!changed)
                Button("Save") { app.run(.regions(regions)) }.keyboardShortcut(.defaultAction)
                    .disabled(!changed || regions.isEmpty || !app.ready)
            }
            if let e = app.runner.lastError { Text(e).foregroundStyle(.red).font(.caption) }
        }
        .padding()
        .onAppear { regions = saved }
        .onChange(of: saved) { _, new in if !changed { regions = new } }
        .onChange(of: country) { city = "" }
    }
}

/// What every server must have, and the provider's levers (Proton: NetShield, VPN
/// Accelerator, Moderate NAT, NAT-PMP). Levers are bound to the key (D17), so applying them
/// makes a new key for the exit slot.
struct ConnectionTab: View {
    let app: AppContainer
    @State private var values: [String: String] = [:]

    var current: [String: String] { app.provider.levers(slot: "a") }

    var body: some View {
        let required = Set(app.connection.status?.prefs.require ?? [])
        Form {
            Section {
                ForEach(Feature.allCases, id: \.self) { f in
                    Toggle(f.name, isOn: Binding(
                        get: { required.contains(f.rawValue) },
                        set: { on in
                            var r = Feature.allCases.filter { required.contains($0.rawValue) && $0 != f }
                            if on { r.append(f) }
                            app.run(.require(r))
                        }))
                    .disabled(!app.ready)
                }
            } header: {
                Text("Every server must have")
            } footer: {
                Text("Failover and New Server only pick servers with all of these; if none is up, the exit holds traffic (degraded) instead of using one without. The current server is kept until the next change.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section {
                ForEach(app.provider.options) { o in
                    if o.isToggle {
                        Toggle(o.label, isOn: Binding(get: { values[o.key] == "on" },
                                                      set: { values[o.key] = $0 ? "on" : "off" }))
                    } else {
                        Picker(o.label, selection: Binding(get: { values[o.key] ?? "" }, set: { values[o.key] = $0 })) {
                            ForEach(o.sortedChoices, id: \.value) { Text("\($0.value): \($0.label)").tag($0.value) }
                        }
                    }
                    if !o.help.isEmpty { Text(o.help).font(.caption).foregroundStyle(.secondary) }
                }
            } header: {
                Text("\(app.provider.provider.capitalized) options for the exit")
            } footer: {
                Text("Applying makes a new key with these options and swaps it in (about 10 s; traffic pauses ~1–2 s). The controller keeps the old key if the new one does not work.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                if let b = app.runner.busy { ProgressView().controlSize(.small); Text(b) }
                if let e = app.runner.lastError ?? app.provider.error { Text(e).foregroundStyle(.red).font(.caption) }
                Spacer()
                Button("Revert") { values = current }.disabled(values == current)
                Button("Apply") { app.run(.rotateKey(slot: "a", options: values)) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(values == current || !app.runner.isIdle || !app.provider.isLoggedIn)
            }
            if !app.provider.isLoggedIn { Text("Log in under Account first.").font(.caption) }
        }
        .formStyle(.grouped)
        .onAppear { values = current }
        .onChange(of: current) { values = current }
    }
}

struct AccountTab: View {
    let app: AppContainer
    @State private var showLogin = false

    var body: some View {
        let p = app.provider
        Form {
            Section("\(p.provider.capitalized) account") {
                if let a = p.account, a.isLoggedIn {
                    LabeledContent("Logged in as", value: a.account ?? "?")
                    Button("Log Out") { app.run(.logout) }
                } else {
                    Text(p.account?.needs2fa == true ? "Login waiting for a 2FA code" : "Not logged in")
                    Button("Log In…") { showLogin = true }
                }
                Text("The session stays in this Mac's Keychain; it never goes to the exit node.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Keys on the exit node") {
                ForEach(p.keys) { k in
                    HStack {
                        VStack(alignment: .leading) {
                            Text("Slot \(k.slot) (\(k.role)): \(k.deviceName ?? k.source)")
                            Text(k.options.map { "\($0.key)=\($0.value)" }.sorted().joined(separator: ", "))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text(k.expiresInDays.map { "expires in \(Int($0)) days" } ?? "expiry unknown")
                            .foregroundStyle((k.expiresInDays ?? 99) < 14 ? .red : .secondary)
                        Button("Renew") { app.run(.renewKey(slot: k.slot)) }
                        Button("New Key") { app.run(.rotateKey(slot: k.slot, options: [:])) }
                    }
                    .disabled(!app.runner.isIdle || !p.isLoggedIn)
                }
            }
            Section("Certificates on the account") {
                ForEach(p.certificates, id: \.stableID) { c in
                    HStack {
                        Text(c.deviceName.isEmpty ? "(unnamed)" : c.deviceName)
                        if let s = c.slot { Text("slot \(s)").foregroundStyle(.green) }
                        if c.orphan == true { Text("unused").foregroundStyle(.orange) }
                        Spacer()
                        if let e = c.expiresAt { Text(Date(timeIntervalSince1970: e), style: .date).foregroundStyle(.secondary) }
                        if c.slot == nil, let id = c.id {
                            Button("Delete") { app.run(.deleteCertificate(id: id)) }.disabled(!app.runner.isIdle)
                        }
                    }
                }
            }
            Section("Server list") {
                if let cat = app.connection.status?.catalog {
                    LabeledContent("Source", value: cat.pushed ? "live from \(p.provider.capitalized)" : "pinned fallback")
                    LabeledContent("Updated", value: Date(timeIntervalSince1970: cat.fetchedAt).formatted(.relative(presentation: .named)))
                    LabeledContent("Servers", value: "\(cat.enabled ?? cat.servers) of \(cat.servers) up")
                }
                HStack {
                    Button("Push Now") { app.run(.pushServers) }
                    Button("Revert to Pinned") { app.run(.revertServers) }
                }
                .disabled(!app.runner.isIdle)
                Toggle("Push hourly in the background (launchd agent)", isOn: Binding(
                    get: { app.agentInstalled }, set: { app.setAgent($0) }))
                if let e = app.agentError { Text(e).foregroundStyle(.red).font(.caption) }
            }
            if let b = app.runner.busy { HStack { ProgressView().controlSize(.small); Text(b) } }
            if let e = app.runner.lastError ?? p.error { Text(e).foregroundStyle(.red) }
        }
        .formStyle(.grouped)
        .sheet(isPresented: $showLogin) { LoginSheet(app: app) }
    }
}

struct LogTab: View {
    let app: AppContainer
    var body: some View {
        List(app.connection.log.reversed(), id: \.self) { e in
            HStack(alignment: .top) {
                Text(Date(timeIntervalSince1970: e.ts), format: .dateTime.month().day().hour().minute().second())
                    .monospacedDigit().foregroundStyle(.secondary)
                Text(e.summary)
            }
        }
    }
}
