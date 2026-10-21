import Foundation
import Observation

/// Whether this Mac routes through the exit node, from `tailscale status` every `interval`.
/// Read-only: the exit node is chosen in the Tailscale app (PLAN Phase 5).
@MainActor @Observable
public final class ExitNodeMonitor {
    public private(set) var use: ExitNodeUse = .unknown("checking")
    private var task: Task<Void, Never>?
    private let interval: Duration

    public init(interval: Duration = .seconds(10)) { self.interval = interval }

    public func watch(node: String) {
        task?.cancel()
        task = Task { [weak self, interval] in
            while !Task.isCancelled {
                let use = await Tailscale.exitNodeUse(node: node)
                self?.use = use
                try? await Task.sleep(for: interval)
            }
        }
    }
}
