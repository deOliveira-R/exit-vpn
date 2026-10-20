import Foundation

/// Controller API shapes (controller/src/exit_vpn/api.py). Decoded with
/// `.convertFromSnakeCase`; fields the app does not need are left out.

public struct Region: Codable, Hashable, Sendable, Identifiable {
    public var country: String
    public var city: String?
    public var id: String { "\(country)/\(city ?? "")" }
    public var label: String { city.map { "\($0), \(country)" } ?? country }
    public init(country: String, city: String? = nil) { self.country = country; self.city = city }

    public func encode(to encoder: Encoder) throws {   // always send "city", null included
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(country, forKey: .country)
        try c.encode(city, forKey: .city)
    }
}

public struct Prefs: Codable, Hashable, Sendable {
    public var regions: [Region]
    public var autoFailover: Bool
    public var secureCore: Bool
    public var allowFree: Bool
    public var require: [String]
}

public struct CatalogInfo: Codable, Hashable, Sendable {
    public var servers: Int
    public var source: String
    public var pushed: Bool
    public var fetchedAt: Double
    public var enabled: Int?
}

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
        default: return kind.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
}

public struct Status: Codable, Hashable, Sendable {
    public var server: String?
    public var country: String?
    public var city: String?
    public var endpoint: String?
    public var since: Double?
    public var profile: [String]?
    public var entry: String?
    public var features: [String]?
    public var load: Int?
    public var health: String
    public var exitIp: String?
    public var exitLoc: String?
    public var errors: [String]
    public var warnings: [String]?
    public var prefs: Prefs
    public var provider: String
    public var keyOptions: [String: String]?
    public var catalog: CatalogInfo
    public var blacklisted: Int?
    public var events: [Event]?

    public var health_: Health { Health(rawValue: health) ?? .unknown }
    public var endpointIP: String? { endpoint.map { String($0.split(separator: ":").first ?? "") } }
}

public enum Health: String, Sendable {
    case starting, healthy, suspect, failing_over, degraded, unknown
    public var label: String {
        switch self {
        case .starting: return "Connecting…"
        case .healthy: return "Healthy"
        case .suspect: return "Checking…"
        case .failing_over: return "Failing over…"
        case .degraded: return "Degraded: kill switch holding"
        case .unknown: return "Unknown"
        }
    }
}

public struct Server: Codable, Hashable, Sendable, Identifiable {
    public var name: String
    public var country: String
    public var city: String
    public var hostname: String
    public var ip: String
    public var pubkey: String
    public var secureCore: Bool
    public var tor: Bool
    public var free: Bool
    public var stream: Bool
    public var portForward: Bool
    public var ipv6: Bool?
    public var load: Int?
    public var enabled: Bool?
    public var entry: String?

    public var id: String { "\(pubkey)@\(ip)" }
    public var isEnabled: Bool { enabled ?? true }
    public var hasIPv6: Bool { ipv6 ?? false }
    /// Short feature badges for lists.
    public var badges: [String] {
        var b: [String] = []
        if secureCore { b.append("SC \(entry ?? "")".trimmingCharacters(in: .whitespaces)) }
        if portForward { b.append("P2P") }
        if stream { b.append("Streaming") }
        if hasIPv6 { b.append("IPv6") }
        if tor { b.append("Tor") }
        if free { b.append("Free") }
        return b
    }
}

public struct ServersResponse: Codable, Sendable {
    public var count: Int
    public var countries: [String]?
    public var servers: [Server]
}

public struct FindResult: Codable, Hashable, Sendable, Identifiable {
    public var server: String
    public var ip: String?
    public var country: String?
    public var city: String?
    public var outcome: String
    public var signals: [String]?
    public var exitIp: String?
    public var elapsedMs: Int?
    public var id: String { "\(server)@\(ip ?? "")" }
}

public struct FindJob: Codable, Sendable {
    public struct Progress: Codable, Sendable { public var done: Int; public var total: Int }
    public var jobId: String
    public var url: String
    public var state: String
    public var error: String?
    public var progress: Progress
    public var results: [FindResult]
    public var running: Bool { state == "running" }
}

public struct KeySlot: Codable, Hashable, Sendable, Identifiable {
    public var slot: String
    public var role: String
    public var source: String
    public var publicKey: String?
    public var options: [String: String]
    public var meta: [String: JSONValue]
    public var expiresInDays: Double?
    public var id: String { slot }
    public var deviceName: String? { meta["device_name"]?.string }
    public var provider: String? { meta["provider"]?.string }
}

public struct KeysResponse: Codable, Sendable { public var slots: [KeySlot] }

/// Country flag from an ISO code ("US" → 🇺🇸).
public func flag(_ code: String?) -> String {
    guard let code, code.count == 2 else { return "🌐" }
    return code.uppercased().unicodeScalars.compactMap {
        UnicodeScalar(127397 + $0.value).map(String.init)
    }.joined()
}

/// ISO code from a Proton-style hostname (us-ca-01…, node-ch-03…), as the controller does.
/// The server's country from its name: "NG#1" and "US-NY#3" give the first code, a Secure Core
/// "CH-US#3" gives its exit (US). Hostnames can name another country (Proton hosts some virtual
/// locations elsewhere: NG#1 is node-es-…), so they are only the fallback.
public func countryCode(_ s: Server) -> String? {
    let parts = s.name.split(separator: "#").first?.split(separator: "-") ?? []
    let code = s.secureCore && parts.count > 1 ? parts[1] : parts.first
    if let c = code, c.count == 2, c.allSatisfy({ $0.isASCII && $0.isUppercase }) { return String(c) }
    return countryCode(hostname: s.hostname)
}

public func countryCode(hostname: String) -> String? {
    let parts = hostname.split(separator: ".").first?.split(separator: "-") ?? []
    let first = parts.first == "node" ? parts.dropFirst().first : parts.first
    guard let f = first, f.count == 2, f.allSatisfy(\.isLetter) else { return nil }
    return f.uppercased()
}
