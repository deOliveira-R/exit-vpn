import AppKit
import ExitControlCore
import Foundation
import os
import ServiceManagement

/// The app's composition root: it builds the state objects, connects them, and is
/// the one place views, `exitvpn://` URLs and Shortcuts go through.
///
///     AppSettings ──► ControllerClient ──► ConnectionStore (status, events) ──► notifications
///                                      ├─► CatalogStore (servers; reloads on catalog events)
///                 └─► Exitctl ─────────┴─► ProviderStore (account, levers, keys, certificates)
///     ActionRunner: every change, one at a time (menu: refuse while busy; URLs/Shortcuts: queue)
///     Router: which window to open (the menu-bar label opens it)
///     ExitNodeMonitor: whether this Mac uses the exit node (tailscale) ──► ExitNodeGuard
///                      (re-selects it when this Mac has none, unless paused or turned off)
@MainActor @Observable
final class AppContainer {
    static let shared = AppContainer()   // AppDelegate (URLs) and Shortcuts reach the app here

    let settings = AppSettings()
    let connection = ConnectionStore()
    let catalog = CatalogStore()
    let provider = ProviderStore()
    let exitNode = ExitNodeMonitor()
    let exitGuard = ExitNodeGuard(select: { try await Tailscale.selectExitNode($0) })
    let runner = ActionRunner()
    let router = Router()
    let updater = Updater()
    @ObservationIgnored let notifier = Notifier()

    private(set) var agentInstalled = LaunchAgent().installed
    private(set) var agentError: String?
    private(set) var launchAtLogin = SMAppService.mainApp.status == .enabled

    private init() {
        connection.onConnected = { [weak self] in
            Log.info("connected")
            Task { await self?.catalog.loadIfEmpty() }
        }
        connection.onEvent = { [weak self] ev in
            Log.info("event \(ev.summary)")
            if ev.kind == "catalog" || ev.kind == "catalog_reverted" { Task { await self?.catalog.load() } }
        }
        connection.onNotable = { [weak self] note in self?.notify(note) }
        runner.afterSuccess = { [weak self] in await self?.connection.refresh() }
        runner.onFailure = { [weak self] label, message in
            if self?.settings.notify == true { self?.notifier.post(title: "Exit node: \(label) failed", body: message) }
        }
        settings.onControllerURLChange = { [weak self] _ in self?.connect(reinstallAgent: true) }
        exitNode.onUse = { [weak self] use in await self?.guardExitNode(use) ?? false }
        notifier.requestAuthorization()
        connect(reinstallAgent: false)
    }

    var api: ControllerClient? { settings.controller }

    /// (Re)connect everything to the controller in the settings.
    func connect(reinstallAgent: Bool) {
        let api = self.api
        Log.info("connecting to \(settings.controllerURL)")
        connection.connect(api)
        catalog.connect(api)
        provider.connect(cli: settings.exitctl, api: api)
        exitNode.watch(node: settings.nodeName)
        if reinstallAgent && agentInstalled { setAgent(true) }   // the agent pushes to the URL in its plist
    }

    /// Whether the menu's actions can run now.
    var ready: Bool { connection.online && runner.isIdle }

    // Running changes ----------------------------------------------------------------------
    /// From the menu and windows: refused while something else runs.
    func run(_ command: ControllerCommand) {
        Task { _ = try? await runController(command, policy: .rejectIfBusy) }
    }

    /// From URLs and Shortcuts: waits its turn; throws what went wrong.
    func runQueued(_ command: ControllerCommand) async throws {
        try await runController(command, policy: .enqueue)
    }

    private func runController(_ command: ControllerCommand, policy: ActionRunner.Policy) async throws {
        guard let api else { throw AppError("no valid controller URL (Settings → General)") }
        Log.info("action \(command.label)")
        try await runner.submit(command.label, policy: policy) { try await api.perform(command) }
    }

    func run(_ command: ProviderCommand) {
        Task {
            Log.info("provider \(command.label)")
            _ = try? await runner.submit(command.label) { try await self.provider.perform(command) }
        }
    }

    func toggleFavorite(_ name: String) {
        guard let current = connection.status?.prefs.favorites else { return }
        let isFavorite = current.contains { $0.caseInsensitiveCompare(name) == .orderedSame }
        run(.favorites(isFavorite ? current.filter { $0.caseInsensitiveCompare(name) != .orderedSame }
                                  : current + [name]))
    }

    func isFavorite(_ name: String) -> Bool {
        connection.status?.prefs.favorites?.contains { $0.caseInsensitiveCompare(name) == .orderedSame } ?? false
    }

    /// `exitvpn://` URLs (see ExitURL).
    func open(_ url: URL) {
        guard let action = ExitURL(url) else { Log.error("unknown URL \(url)"); return }
        Log.info("url \(url)")
        switch action {
        case .open(let route): router.open(route)
        case .run(let command): Task { try? await runQueued(command) }
        case .refresh: Task { await connection.refresh(); await catalog.load() }
        }
    }

    // Keeping this Mac on the exit node --------------------------------------------------
    private func guardExitNode(_ use: ExitNodeUse) async -> Bool {
        let node = settings.nodeName
        switch await exitGuard.check(use, node: node, enabled: settings.keepExitNode) {
        case .nothing:
            return false
        case .reselected:
            Log.info("this Mac had no exit node; selected \(node)")
            if settings.notify {
                notifier.post(title: "Exit node re-selected",
                              body: "This Mac had no exit node, so its traffic was going out directly. It uses \(node) again.",
                              id: "exit-guard")
            }
            return true
        case .failed(let message):
            Log.error("could not select \(node): \(message)")
            if settings.notify {
                notifier.post(title: "This Mac has no exit node",
                              body: "Selecting \(node) failed: \(message). Traffic is going out directly.", id: "exit-guard")
            }
            return false
        }
    }

    /// Clears this Mac's exit node and pauses the guard, which selects it again afterwards.
    func goDirect(for duration: TimeInterval) {
        exitGuard.pause(for: duration)
        Log.info("direct for \(Int(duration)) s")
        Task { try? await Tailscale.selectExitNode(nil) }
    }

    // Notifications ------------------------------------------------------------------------
    private func notify(_ note: ConnectionStore.Notable) {
        guard settings.notify else { return }
        switch note {
        case .event(let ev):
            notifier.post(title: ev.kind == "degraded" ? "Exit node degraded" : "Exit node", body: ev.summary)
        case .warning(let w):
            notifier.post(title: "Exit node", body: w, id: "warn-\(w)")
        }
    }

    // The Mac side: background agent, launch at login --------------------------------------
    func setAgent(_ on: Bool) {
        Task {
            do {
                if on {
                    guard let x = settings.exitctl else { throw ExitctlError.notInstalled }
                    try await LaunchAgent().install(exitctl: x.executable, provider: provider.provider,
                                                    controllerURL: settings.controllerURL)
                } else {
                    try await LaunchAgent().uninstall()
                }
                agentError = nil
            } catch {
                agentError = "Background agent: \(error.localizedDescription)"
            }
            agentInstalled = LaunchAgent().installed
        }
    }

    func setLaunchAtLogin(_ on: Bool) {
        do { on ? try SMAppService.mainApp.register() : try SMAppService.mainApp.unregister() }
        catch { agentError = "Launch at login: \(error.localizedDescription)" }
        launchAtLogin = SMAppService.mainApp.status == .enabled
    }

    func openStatusJSON() {
        if let u = URL(string: settings.controllerURL)?.appending(path: "v1/status") { NSWorkspace.shared.open(u) }
    }
}

struct AppError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// Which window to open. URLs and the menu ask here; the menu-bar label, always on screen,
/// watches `request` and opens the window (a SwiftUI window can only be opened from a view).
@MainActor @Observable
final class Router {
    struct Request: Equatable { let route: AppRoute; let nonce = UUID() }
    private(set) var request: Request?
    var settingsTab: SettingsTab = .general

    func open(_ route: AppRoute) {
        if case .settings(let tab) = route { settingsTab = tab }
        request = Request(route: route)
    }
}

/// Logging (Console.app, subsystem org.exit-vpn.ExitControl). With EXITCONTROL_DEBUG set,
/// also to stderr: `open --env EXITCONTROL_DEBUG=1 --stderr <file> ExitControl.app`. Messages
/// carry actions, URLs and event summaries, never credentials or keys.
enum Log {
    private static let logger = Logger(subsystem: "org.exit-vpn.ExitControl", category: "app")
    private static let toStderr = ProcessInfo.processInfo.environment["EXITCONTROL_DEBUG"] != nil

    static func info(_ message: String) {
        logger.info("\(message, privacy: .public)")
        if toStderr { FileHandle.standardError.write(Data((message + "\n").utf8)) }
    }

    static func error(_ message: String) {
        logger.error("\(message, privacy: .public)")
        if toStderr { FileHandle.standardError.write(Data(("error: " + message + "\n").utf8)) }
    }
}
