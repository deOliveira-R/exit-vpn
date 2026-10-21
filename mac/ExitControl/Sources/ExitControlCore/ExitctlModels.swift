import Foundation

/// exitctl's `--json` shapes (contracts/exitctl/).

public struct ProviderOption: Codable, Hashable, Sendable, Identifiable {
    public var key: String
    public var label: String
    public var confLabel: String?  // how the .conf comments name it ("NetShield")
    public var kind: String        // "bool" | "choice"
    public var isToggle: Bool { kind == "bool" }
    public var `default`: JSONValue
    public var choices: [String: String]
    public var help: String
    public var id: String { key }
    /// Choice values in numeric order.
    public var sortedChoices: [(value: String, label: String)] {
        choices.sorted { (Int($0.key) ?? 0) < (Int($1.key) ?? 0) }.map { ($0.key, $0.value) }
    }
}

public struct Account: Codable, Sendable, Equatable {
    public var account: String?
    public var loggedIn: Bool?
    public var needs2fa: Bool?
    public init(account: String?, loggedIn: Bool?, needs2fa: Bool?) {
        self.account = account; self.loggedIn = loggedIn; self.needs2fa = needs2fa
    }
    public var isLoggedIn: Bool { account != nil && loggedIn != false && needs2fa != true }
}

public struct Certificate: Codable, Hashable, Sendable, Identifiable {
    public var id: String?
    public var deviceName: String
    public var expiresAt: Double?
    public var options: [String: JSONValue]
    public var wgPublicKey: String?
    public var slot: String?
    public var orphan: Bool?
    public var stableID: String { id ?? deviceName }
}

public struct ProviderInfo: Codable, Hashable, Sendable {
    public var name: String
    public var title: String
    public var capabilities: [String]
}

/// One login step: credentials go to exitctl on stdin as JSON, never in argv.
public struct LoginStep: Encodable, Sendable, Equatable {
    public var username: String?
    public var password: String?
    public var totp: String?
    public var hvToken: String?
    public var hvType: String?
    public init(username: String? = nil, password: String? = nil, totp: String? = nil,
                hvToken: String? = nil, hvType: String? = nil) {
        self.username = username; self.password = password; self.totp = totp
        self.hvToken = hvToken; self.hvType = hvType
    }
}
