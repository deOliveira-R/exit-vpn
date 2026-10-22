import Foundation

/// One controller event (contracts/controller/events.json): `kind` plus kind-specific fields.
public struct Event: Codable, Hashable, Sendable {
    public var ts: Double
    public var kind: String
    public var data: [String: JSONValue]

    struct Key: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Key.self)
        ts = try c.decode(Double.self, forKey: Key(stringValue: "ts"))
        kind = try c.decode(String.self, forKey: Key(stringValue: "kind"))
        var d: [String: JSONValue] = [:]
        for k in c.allKeys where k.stringValue != "ts" && k.stringValue != "kind" {
            d[k.stringValue] = try c.decode(JSONValue.self, forKey: k)
        }
        data = d
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: Key.self)
        try c.encode(ts, forKey: Key(stringValue: "ts"))
        try c.encode(kind, forKey: Key(stringValue: "kind"))
        for (k, v) in data { try c.encode(v, forKey: Key(stringValue: k)) }
    }

    /// One line for menus, logs and notifications.
    public var summary: String {
        func s(_ k: String) -> String { data[k]?.string ?? "?" }
        switch kind {
        case "failover_done": return "Failed over \(s("old")) → \(s("new"))"
        case "failover_start": return "Failing over from \(s("server"))"
        case "switched": return "Switched \(s("old")) → \(s("new"))"
        case "degraded": return "Degraded: no server worked (\(s("tried")) tried); traffic blocked"
        case "recovered": return "Recovered on \(s("server"))"
        case "candidate_failed": return "Candidate \(s("server")) failed"
        case "key_replaced": return "Key replaced (slot \(s("slot")))"
        case "key_rejected": return "Key rejected (slot \(s("slot")))"
        case "catalog": return "Server list updated (\(s("servers")) servers)"
        case "switch_reverted": return "Switch failed; back on \(s("server"))"
        case "health":
            let first = data["errors"].flatMap { if case .array(let a) = $0 { a.first?.string } else { nil } }
            return "Health: \(s("state").replacingOccurrences(of: "_", with: " "))" + (first.map { " (\($0))" } ?? "")
        case "prefs": return "Settings changed by \(s("by"))"
        case "started": return "Controller started on \(s("server"))"
        case "unhealthy_no_failover": return "Unhealthy; automatic failover is off"
        case "swap_error": return "Switch to \(s("server")) failed: \(s("error"))"
        case "catalog_reverted": return "Server list reverted to the pinned list"
        case "find_started": return "Finding a server for \(s("url"))"
        case "key_meta": return "Key details saved (slot \(s("slot")))"
        case "handover": return "Handed over \(s("old")) → \(s("new")) (standby, instant)"
        case "handover_error": return "Handover to \(s("server")) failed: \(s("error"))"
        case "uplink_suspect": return "Both tunnels failing: likely the VPS's own connection; staying on \(s("server"))"
        case "standby":
            switch s("state") {
            case "ready": return "Standby ready on \(s("server"))"
            case "lost": return "Standby \(s("server")) failed; finding another"
            case "moving": return "Standby leaving \(s("server")): outside the exit's region"
            case "none_found": return "No standby server found (\(s("tried")) tried)"
            case "key_suspect": return "Standby stopped: its key carries no traffic"
            default: return "Standby: \(s("state"))"
            }
        case "ipv6":
            return data["open"] == .bool(true) ? "IPv6 through the exit on \(s("server"))"
                                              : "IPv6 off on \(s("server")); apps use IPv4"
        default: return kind.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
}
