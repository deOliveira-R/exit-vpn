import Foundation

/// Whether this Mac routes through the exit node. Chosen in the Tailscale app; ExitControl only
/// re-selects the controller's node when none is set (ExitNodeGuard).
public enum ExitNodeUse: Equatable, Sendable {
    case usingThis              // this Mac's exit node is the controller's node
    case usingOther(String)     // another exit node
    case none                   // no exit node: traffic leaves this Mac directly
    case tailscaleOff(String)   // Tailscale not running (its backend state): no tailnet at all
    case unknown(String)        // no tailscale CLI, or it failed

    public var label: String {
        switch self {
        case .usingThis: return "This Mac uses the exit node"
        case .usingOther(let n): return "This Mac uses another exit node (\(n))"
        case .none: return "This Mac is not using the exit node"
        case .tailscaleOff(let state): return "Tailscale is not connected (\(state))"
        case .unknown(let why): return "Exit node use unknown (\(why))"
        }
    }
}

public enum Tailscale {
    static let paths = ["/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
                        "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]

    struct StatusJSON: Decodable {
        struct Peer: Decodable {
            var HostName: String
            var ExitNode: Bool?
        }
        var BackendState: String?
        var Peer: [String: Peer]?
    }

    static var executable: URL? {
        paths.first(where: { FileManager.default.isExecutableFile(atPath: $0) }).map { URL(fileURLWithPath: $0) }
    }

    /// `node`: the exit node's Tailscale host name (e.g. "exit-proton").
    public static func exitNodeUse(node: String) async -> ExitNodeUse {
        guard let exe = executable else { return .unknown("tailscale CLI not found") }
        do {
            let r = try await ProcessRunner(timeout: 10).run(exe, ["status", "--json"])
            return parse(r.stdout, node: node)
        } catch {
            return .unknown(error.localizedDescription)
        }
    }

    /// Makes `node` this Mac's exit node, or none for nil (`tailscale set --exit-node`; other
    /// settings, such as LAN access, stay as they are).
    public static func selectExitNode(_ node: String?) async throws {
        guard let exe = executable else { throw ProcessRunner.Failure(message: "tailscale CLI not found") }
        let r = try await ProcessRunner(timeout: 15).run(exe, ["set", "--exit-node=\(node ?? "")"])
        guard r.code == 0 else {
            throw ProcessRunner.Failure(message: r.lastErrorLine ?? "tailscale set exited with \(r.code)")
        }
    }

    public static func parse(_ data: Data, node: String) -> ExitNodeUse {
        guard let st = try? JSONDecoder().decode(StatusJSON.self, from: data) else {
            return .unknown("unreadable tailscale status")
        }
        if let state = st.BackendState, state != "Running" { return .tailscaleOff(state) }
        guard let active = st.Peer?.values.first(where: { $0.ExitNode == true }) else { return .none }
        return active.HostName.lowercased() == node.lowercased() ? .usingThis : .usingOther(active.HostName)
    }
}
