import Foundation

/// Whether this Mac routes through the exit node. Read-only: choosing the exit node stays
/// in the Tailscale app (PLAN Phase 5).
public enum ExitNodeUse: Equatable, Sendable {
    case usingThis              // this Mac's exit node is the controller's node
    case usingOther(String)     // another exit node
    case none                   // no exit node
    case unknown(String)        // no tailscale CLI, or it failed

    public var label: String {
        switch self {
        case .usingThis: return "This Mac uses the exit node"
        case .usingOther(let n): return "This Mac uses another exit node (\(n))"
        case .none: return "This Mac is not using the exit node"
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
        var Peer: [String: Peer]?
    }

    /// `node`: the exit node's Tailscale host name (e.g. "exit-proton").
    public static func exitNodeUse(node: String) async -> ExitNodeUse {
        guard let exe = paths.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            return .unknown("tailscale CLI not found")
        }
        return await withCheckedContinuation { cont in
            DispatchQueue.global().async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: exe)
                p.arguments = ["status", "--json"]
                let out = Pipe()
                p.standardOutput = out; p.standardError = Pipe()
                do { try p.run() } catch { cont.resume(returning: .unknown(error.localizedDescription)); return }
                let data = out.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                cont.resume(returning: parse(data, node: node))
            }
        }
    }

    public static func parse(_ data: Data, node: String) -> ExitNodeUse {
        guard let st = try? JSONDecoder().decode(StatusJSON.self, from: data) else {
            return .unknown("unreadable tailscale status")
        }
        guard let active = st.Peer?.values.first(where: { $0.ExitNode == true }) else { return .none }
        return active.HostName.lowercased() == node.lowercased() ? .usingThis : .usingOther(active.HostName)
    }
}
