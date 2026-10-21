import Foundation

/// The controller API's shapes (contracts/controller/). Decoded with `.convertFromSnakeCase`;
/// fields the app does not need are left out, and fields older controllers lack are optional.

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
    public var favorites: [String]?   // nil from controllers without favourites
}

public struct CatalogInfo: Codable, Hashable, Sendable {
    public var servers: Int
    public var source: String
    public var pushed: Bool
    public var fetchedAt: Double
    public var enabled: Int?
}

public struct Status: Codable, Hashable, Sendable {
    public var version: String?       // the node's release (node-vX.Y.Z); nil from older nodes
    public var api: Int?              // the controller API version
    public var server: String?
    public var country: String?
    public var city: String?
    public var endpoint: String?
    public var since: Double?
    public var profile: [String]?
    public var entry: String?
    public var features: [String]?
    public var load: Int?
    public var health: Health
    public var exitIp: String?
    public var exitLoc: String?
    public var ipv6: Bool?            // forwarded IPv6 goes through the tunnel; nil from older nodes
    public var exitIp6: String?
    public var errors: [String]
    public var warnings: [String]?
    public var prefs: Prefs
    public var provider: String
    public var keyOptions: [String: String]?
    public var catalog: CatalogInfo
    public var blacklisted: Int?
    public var events: [Event]?

    public var endpointIP: String? { endpoint.map { String($0.split(separator: ":").first ?? "") } }
    /// "Los Angeles, United States" (the parts the controller knows).
    public var place: String { [city, country].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: ", ") }
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
    public var verdict: FindOutcome { FindOutcome(wire: outcome) }
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
