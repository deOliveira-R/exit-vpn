import Foundation
import Observation

/// Whether this Mac routes through the exit node, from `tailscale status` every `interval`.
/// `onUse` sees each reading (the ExitNodeGuard); it returns true when it changed something,
/// and the monitor then reads again a second later instead of a full interval.
@MainActor @Observable
public final class ExitNodeMonitor {
    public private(set) var use: ExitNodeUse = .unknown("checking")
    @ObservationIgnored public var onUse: @MainActor (ExitNodeUse) async -> Bool = { _ in false }
    private var task: Task<Void, Never>?
    private let interval: Duration
    private let read: @Sendable (String) async -> ExitNodeUse

    public init(interval: Duration = .seconds(5),
                read: @escaping @Sendable (String) async -> ExitNodeUse = { await Tailscale.exitNodeUse(node: $0) }) {
        self.interval = interval
        self.read = read
    }

    public func watch(node: String) {
        task?.cancel()
        task = Task { [weak self, interval, read] in
            while !Task.isCancelled {
                let use = await read(node)
                guard let self else { return }
                self.use = use
                let changed = await self.onUse(use)
                try? await Task.sleep(for: changed ? .seconds(1) : interval)
            }
        }
    }
}
