import Foundation
import Observation

/// Keeps this Mac on the exit node. The exit fails closed only while it is selected; with no
/// exit node, traffic leaves the Mac directly. So when the monitor sees none (cleared by a
/// script, the Tailscale menu, a re-login), the guard selects the controller's node again.
/// Another exit node is a deliberate choice and is left alone, and so is Tailscale being off
/// (VPN On Demand's job). While paused (the menu's "Go Direct", for a captive portal) it waits.
@MainActor @Observable
public final class ExitNodeGuard {
    public enum Outcome: Equatable, Sendable {
        case nothing                // in use, another choice, disabled, or paused
        case reselected             // there was no exit node; it is selected again
        case failed(String)         // selecting it failed (reported once per distinct message)
    }

    public private(set) var pausedUntil: Date?
    public private(set) var lastFailure: String?
    @ObservationIgnored private let select: (String) async throws -> Void
    @ObservationIgnored private let now: () -> Date

    public init(select: @escaping (String) async throws -> Void, now: @escaping () -> Date = Date.init) {
        self.select = select
        self.now = now
    }

    public func pause(for duration: TimeInterval) { pausedUntil = now().addingTimeInterval(duration) }
    public func resume() { pausedUntil = nil }

    /// One observation from the monitor. `enabled`: the "keep this Mac on the exit node" setting.
    public func check(_ use: ExitNodeUse, node: String, enabled: Bool) async -> Outcome {
        if let until = pausedUntil, now() >= until { pausedUntil = nil }
        if use == .usingThis { lastFailure = nil }
        guard enabled, pausedUntil == nil, use == .none else { return .nothing }
        do {
            try await select(node)
            lastFailure = nil
            return .reselected
        } catch {
            let message = error.localizedDescription
            defer { lastFailure = message }
            return message == lastFailure ? .nothing : .failed(message)
        }
    }
}
