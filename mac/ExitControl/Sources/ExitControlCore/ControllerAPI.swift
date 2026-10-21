import Foundation

/// Everything the owner can ask the controller to change. Menu items, `exitvpn://` URLs and
/// Shortcuts all become one of these, run by `ActionRunner`.
public enum ControllerCommand: Sendable, Equatable {
    case rotate(RotationScope)
    case connect(Region)
    case connectServer(name: String, ip: String?)   // ip nil: the name's least loaded server
    case secureCore(Bool)
    case autoFailover(Bool)
    case regions([Region])
    case require([Feature])
    case favorites([String])
    case applyFind(job: String, server: String, ip: String?)

    /// What the menu and notifications say while it runs.
    public var label: String {
        switch self {
        case .rotate(let s): return "New server, \(s.label.lowercased())"
        case .connect(let r): return "Connect to \(r.label)"
        case .connectServer(let n, _): return "Connect to \(n)"
        case .secureCore(let on): return "Secure Core \(on ? "on" : "off")"
        case .autoFailover(let on): return "Automatic failover \(on ? "on" : "off")"
        case .regions: return "Pinned regions"
        case .require: return "Requirements"
        case .favorites: return "Favourites"
        case .applyFind(_, let s, _): return "Switch to \(s)"
        }
    }
}

/// The controller as the app sees it (`ControllerClient`; a fake in tests).
public protocol ControllerAPI: Sendable {
    func status() async throws -> Status
    func servers(country: String?) async throws -> ServersResponse
    func keys() async throws -> [KeySlot]
    func find(url: String, countries: [String], max: Int, mode: FindMode, js: Bool) async throws -> FindJob
    func findJob(_ id: String) async throws -> FindJob
    func perform(_ command: ControllerCommand) async throws
    /// Live updates (server-sent events), reconnecting until the consumer stops.
    func updates() -> AsyncStream<StreamUpdate>
}

public enum StreamUpdate: Sendable {
    case connected
    case status(Status)
    case event(Event)
    /// The controller sent a status this app cannot read (a newer or older API).
    case incompatible(String)
    case disconnected(String)
}
