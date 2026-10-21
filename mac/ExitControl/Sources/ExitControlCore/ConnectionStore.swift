import Foundation
import Observation

/// The live connection to the controller: its status, whether it answers, and the recent
/// events. Updates arrive over server-sent events; while the stream is down, the status is
/// polled every `pollInterval` so recovery shows quickly. After each event the status is
/// refreshed (events say what happened, the status says where things are now).
@MainActor @Observable
public final class ConnectionStore {
    /// Something worth a notification.
    public enum Notable: Equatable, Sendable {
        case event(Event)            // failover done, degraded, recovered, key rejected
        case warning(String)         // a status warning that just appeared (key expiry, old catalog)
    }

    public private(set) var status: Status?
    public private(set) var online = false
    public private(set) var offlineReason: String?
    /// The controller answers but its status cannot be read (API mismatch): shown, not hidden.
    public private(set) var incompatible: String?
    public private(set) var log: [Event] = []
    public var onNotable: @MainActor (Notable) -> Void = { _ in }
    /// Called with every event (the catalog store reloads on `catalog`).
    public var onEvent: @MainActor (Event) -> Void = { _ in }
    /// Called when the live stream (re)connects.
    public var onConnected: @MainActor () -> Void = {}

    public static let notableKinds: Set<String> = ["failover_done", "degraded", "recovered", "key_rejected"]
    public static let logLimit = 200

    private var api: (any ControllerAPI)?
    private var streamTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var refreshPending = false
    private let pollInterval: Duration

    public init(pollInterval: Duration = .seconds(10)) {
        self.pollInterval = pollInterval
    }

    public func connect(_ api: (any ControllerAPI)?) {
        stop()
        self.api = api
        status = nil; online = false; incompatible = nil
        guard let api else { offlineReason = "invalid controller URL"; return }
        streamTask = Task { [weak self] in
            for await u in api.updates() {
                guard let self else { return }
                self.handle(u)
            }
        }
        pollTask = Task { [weak self, pollInterval] in
            while !Task.isCancelled {
                try? await Task.sleep(for: pollInterval)
                if let self, !self.online { await self.refresh() }
            }
        }
    }

    public func stop() {
        streamTask?.cancel(); pollTask?.cancel()
        streamTask = nil; pollTask = nil
    }

    func handle(_ u: StreamUpdate) {
        switch u {
        case .connected:
            online = true; offlineReason = nil
            onConnected()
        case .status(let st):
            apply(st)
        case .event(let ev):
            append(ev)
            onEvent(ev)
            if Self.notableKinds.contains(ev.kind) { onNotable(.event(ev)) }
            scheduleRefresh()
        case .incompatible(let why):
            incompatible = why
        case .disconnected(let why):
            online = false; offlineReason = why
        }
    }

    func apply(_ st: Status) {
        let old = status
        status = st
        incompatible = nil
        if log.isEmpty, let evs = st.events { log = evs }
        // Notify warnings that appear, not the ones already there when the app starts
        if let old {
            for w in st.warnings ?? [] where !(old.warnings ?? []).contains(w) { onNotable(.warning(w)) }
        }
    }

    func append(_ ev: Event) {
        log.append(ev)
        if log.count > Self.logLimit { log.removeFirst(log.count - Self.logLimit) }
    }

    private func scheduleRefresh() {
        guard !refreshPending else { return }
        refreshPending = true
        Task {
            try? await Task.sleep(for: .milliseconds(400))   // coalesce bursts of events
            refreshPending = false
            await refresh()
        }
    }

    public func refresh() async {
        guard let api else { return }
        do {
            apply(try await api.status())
            online = true; offlineReason = nil
        } catch let e as DecodingError {
            online = true
            incompatible = "unreadable status from the controller: \(e)"
        } catch {
            online = false; offlineReason = error.localizedDescription
        }
    }
}
