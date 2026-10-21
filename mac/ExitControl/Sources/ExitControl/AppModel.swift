import AppKit
import ExitControlCore
import Foundation
import Observation
import ServiceManagement

/// App state. The controller is the source of truth: status arrives over SSE (with a
/// slow poll as a fallback), actions go over HTTP, provider actions through exitctl.
@MainActor @Observable
final class AppModel {
    static let shared = AppModel()

    // Settings (UserDefaults)
    var controllerURL: String { didSet { save("controllerURL", controllerURL); reconnect() } }
    var exitctlPath: String { didSet { save("exitctlPath", exitctlPath) } }
    var showFlag: Bool { didSet { save("showFlag", showFlag) } }
    var notify: Bool { didSet { save("notify", notify) } }

    // Live state
    var status: Status?
    var online = false
    var offlineReason: String?
    var exitUse: ExitNodeUse = .unknown("checking")
    var log: [Event] = []
    var busy: String?
    var lastError: String?
    var servers: [Server] = []
    var countries: [String] = []
    var codes: [String: String] = [:]   // catalog country → ISO code (for flags)

    // Provider (exitctl)
    var account: Account?
    var options: [ProviderOption] = []
    var keys: [KeySlot] = []
    var certificates: [Certificate] = []
    var agentInstalled = LaunchAgent().installed
    var providerError: String?

    // Windows opened from URLs (the menu-bar label opens them: it is always on screen)
    struct WindowRequest: Equatable { let id: String; let nonce = UUID() }
    var windowRequest: WindowRequest?
    var settingsTab = "general"

    let notifier = Notifier()
    private var streamTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var refreshPending = false

    init() {
        let d = UserDefaults.standard
        controllerURL = d.string(forKey: "controllerURL") ?? "http://exit-proton:8080"
        exitctlPath = d.string(forKey: "exitctlPath") ?? ""
        showFlag = d.object(forKey: "showFlag") as? Bool ?? true
        notify = d.object(forKey: "notify") as? Bool ?? true
        notifier.requestAuthorization()
        reconnect()
    }

    static func debug(_ s: @autoclosure () -> String) {
        if ProcessInfo.processInfo.environment["EXITCONTROL_DEBUG"] != nil { FileHandle.standardError.write(Data((s() + "\n").utf8)) }
    }

    private func save(_ key: String, _ value: Any) { UserDefaults.standard.set(value, forKey: key) }

    var client: ControllerClient? { URL(string: controllerURL).map { ControllerClient(baseURL: $0) } }
    /// The exit node's Tailscale name: the controller URL's first host label.
    var nodeName: String { URL(string: controllerURL)?.host()?.split(separator: ".").first.map(String.init) ?? "exit-proton" }
    var provider: String { status?.provider ?? "proton" }
    var exitctl: Exitctl? {
        Exitctl.locate(override: exitctlPath).map { Exitctl(executable: $0, controllerURL: controllerURL) }
    }

    // Connection ---------------------------------------------------------------------
    func reconnect() {
        streamTask?.cancel(); pollTask?.cancel()
        guard let client else { online = false; offlineReason = "invalid controller URL"; return }
        Self.debug("connecting to \(client.baseURL)")
        streamTask = Task { [weak self] in
            for await u in client.updates() {
                guard let self else { return }
                switch u {
                case .status(let st): Self.debug("status \(st.server ?? "-") \(st.health)")
                case .event(let ev): Self.debug("event \(ev.summary)")
                default: Self.debug("update \(u)")
                }
                switch u {
                case .connected:
                    online = true; offlineReason = nil
                    if servers.isEmpty { await loadServers() }
                case .status(let st): apply(st)
                case .event(let ev): handle(ev)
                case .disconnected(let why): online = false; offlineReason = why
                }
            }
        }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshExitUse()
                if let self, !online { await refresh() }   // SSE down: poll so recovery shows fast
                try? await Task.sleep(for: .seconds(10))
            }
        }
    }

    private func apply(_ st: Status) {
        let old = status
        status = st
        if log.isEmpty, let evs = st.events { log = evs }
        for w in st.warnings ?? [] where !(old?.warnings ?? []).contains(w) {
            if notify { notifier.post(title: "Exit node", body: w, id: "warn-\(w)") }
        }
    }

    private func handle(_ ev: Event) {
        log.append(ev)
        if log.count > 200 { log.removeFirst(log.count - 200) }
        if notify, ["failover_done", "degraded", "recovered", "key_rejected"].contains(ev.kind) {
            notifier.post(title: ev.kind == "degraded" ? "Exit node degraded" : "Exit node", body: ev.summary)
        }
        scheduleRefresh()
    }

    /// Events say what happened; the status says where we are now. Coalesce bursts.
    private func scheduleRefresh() {
        guard !refreshPending else { return }
        refreshPending = true
        Task {
            try? await Task.sleep(for: .milliseconds(400))
            refreshPending = false
            await refresh()
        }
    }

    func refresh() async {
        guard let client else { return }
        do {
            apply(try await client.status())
            online = true; offlineReason = nil
        } catch {
            online = false; offlineReason = error.localizedDescription
        }
    }

    func refreshExitUse() async { exitUse = await Tailscale.exitNodeUse(node: nodeName) }

    // Controller actions ---------------------------------------------------------------
    /// Runs one action at a time; errors land in `lastError` (shown in the menu).
    func perform(_ label: String, _ action: @escaping @MainActor (ControllerClient) async throws -> Void) {
        guard let client else { return }
        guard busy == nil else { Self.debug("ignored \(label): busy with \(busy ?? "")"); return }
        busy = label; lastError = nil
        Self.debug("action \(label)")
        Task {
            defer { busy = nil }
            do {
                try await action(client)
                Self.debug("done \(label)")
                await refresh()
            } catch {
                Self.debug("failed \(label): \(error.localizedDescription)")
                lastError = "\(label): \(error.localizedDescription)"
                if notify { notifier.post(title: "Exit node: \(label) failed", body: error.localizedDescription) }
            }
        }
    }

    func rotate(_ scope: String) { perform("Rotate (\(scope))") { try await $0.rotate(scope) } }
    func select(_ s: Server) { perform("Connect to \(s.name)") { try await $0.select(server: s.name, ip: s.ip) } }
    func select(_ r: Region) { perform("Connect to \(r.label)") { try await $0.select(region: r) } }
    func setSecureCore(_ on: Bool) {
        perform("Secure Core \(on ? "on" : "off")") { try await $0.setPrefs(.init(secureCore: on)) }
    }
    func setAutoFailover(_ on: Bool) { perform("Auto failover") { try await $0.setPrefs(.init(autoFailover: on)) } }
    func setRegions(_ r: [Region]) { perform("Pinned regions") { try await $0.setPrefs(.init(regions: r)) } }
    func setRequire(_ r: [String]) { perform("Requirements") { try await $0.setPrefs(.init(require: r)) } }
    func selectServer(named name: String) {
        perform("Connect to \(name)") { try await $0.select(server: name, ip: nil) }
    }

    // Favourites: logical server names, tried first within each failover/rotate tier
    var favorites: [String] { status?.prefs.favorites ?? [] }
    func isFavorite(_ name: String) -> Bool { favorites.contains { $0.caseInsensitiveCompare(name) == .orderedSame } }
    func toggleFavorite(_ name: String) {
        var f = favorites
        if isFavorite(name) { f.removeAll { $0.caseInsensitiveCompare(name) == .orderedSame } } else { f.append(name) }
        perform(isFavorite(name) ? "Remove \(name) from favourites" : "Add \(name) to favourites") {
            try await $0.setPrefs(.init(favorites: f))
        }
    }

    func openWindow(_ id: String, tab: String? = nil) {
        if let tab { settingsTab = tab }
        windowRequest = WindowRequest(id: id)
    }

    /// `exitvpn://` URLs (see ExitURL).
    func open(_ url: URL) {
        guard let action = ExitURL(url) else { Self.debug("unknown URL \(url)"); return }
        Self.debug("url \(url)")
        switch action {
        case .window(let id, let tab): openWindow(id, tab: tab)
        case .rotate(let scope): rotate(scope)
        case .connect(let region): select(region)
        case .connectServer(let name): selectServer(named: name)
        case .secureCore(let on): setSecureCore(on)
        case .refresh: Task { await refresh(); await loadServers() }
        }
    }

    func loadServers() async {
        guard let client else { return }
        do {
            let r = try await client.servers()
            servers = r.servers
            countries = r.countries ?? Array(Set(r.servers.map(\.country))).sorted()
            var c: [String: String] = [:]
            for s in r.servers where c[s.country] == nil { c[s.country] = countryCode(s) }
            codes = c
        } catch { lastError = "Server list: \(error.localizedDescription)" }
    }

    func isCurrent(_ s: Server) -> Bool { status?.server == s.name && status?.endpointIP == s.ip }

    // Provider actions (exitctl) -----------------------------------------------------
    func provider(_ label: String, _ action: @escaping @MainActor (Exitctl, String) async throws -> Void) {
        guard busy == nil else { return }
        guard let x = exitctl else { providerError = ExitctlError.notInstalled.localizedDescription; return }
        busy = label; providerError = nil
        let p = provider
        Self.debug("provider \(label)")
        Task {
            defer { busy = nil }
            do { try await action(x, p); Self.debug("done \(label)") } catch {
                Self.debug("failed \(label): \(error.localizedDescription)")
                providerError = "\(label): \(error.localizedDescription)"
            }
            await loadProvider()
            await refresh()
        }
    }

    func loadProvider() async {
        guard let x = exitctl else { providerError = ExitctlError.notInstalled.localizedDescription; return }
        let p = provider
        account = try? await x.whoami(p)
        if options.isEmpty { options = (try? await x.options(p)) ?? [] }
        if let client { keys = (try? await client.keys()) ?? keys }
        if account?.isLoggedIn == true { certificates = (try? await x.certificates(p)) ?? certificates }
    }

    /// The exit slot's current lever values (flag form), from its recorded options or
    /// the .conf comments the controller reports.
    func currentOptions() -> [String: String] {
        guard let a = keys.first(where: { $0.slot == "a" }) else { return [:] }
        var out: [String: String] = [:]
        if a.provider == provider, case .object(let o)? = a.meta["options"] {
            for (k, v) in o { out[k] = v.string }
        }
        for opt in options where out[opt.key] == nil {
            // a key made elsewhere (e.g. the provider's dashboard): its .conf comments
            if let v = opt.confLabel.flatMap({ a.options[$0] }) {
                out[opt.key] = v
            } else {
                out[opt.key] = opt.default.string
            }
        }
        return out
    }

    func setAgent(_ on: Bool) {
        Self.debug("agent \(on ? "install" : "remove")")
        do {
            if on {
                guard let x = exitctl else { throw ExitctlError.notInstalled }
                try LaunchAgent().install(exitctl: x.executable, provider: provider, controllerURL: controllerURL)
            } else {
                try LaunchAgent().uninstall()
            }
        } catch {
            Self.debug("failed agent: \(error.localizedDescription)")
            providerError = "Background agent: \(error.localizedDescription)"
        }
        agentInstalled = LaunchAgent().installed
    }

    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do { newValue ? try SMAppService.mainApp.register() : try SMAppService.mainApp.unregister() }
            catch { lastError = "Launch at login: \(error.localizedDescription)" }
        }
    }

    func openStatusJSON() {
        if let u = URL(string: controllerURL)?.appending(path: "v1/status") { NSWorkspace.shared.open(u) }
    }
}
