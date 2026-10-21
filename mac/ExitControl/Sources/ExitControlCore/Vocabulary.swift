import Foundation

// The app's vocabulary: the controller's and exitctl's fixed sets of values, as types. Raw
// values are the wire strings (contracts/). Decoding tolerates values a newer controller may
// add (`.unknown` / `.other`), so an update on one side never breaks the other.

/// The exit's health (the controller's health state machine).
public enum Health: Sendable, Hashable, Codable {
    case starting, healthy, suspect, failingOver, degraded, unknown(String)

    public init(wire: String) {
        switch wire {
        case "starting": self = .starting
        case "healthy": self = .healthy
        case "suspect": self = .suspect
        case "failing_over": self = .failingOver
        case "degraded": self = .degraded
        default: self = .unknown(wire)
        }
    }

    public init(from decoder: Decoder) throws {
        self.init(wire: try decoder.singleValueContainer().decode(String.self))
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(wire)
    }

    public var wire: String {
        switch self {
        case .starting: return "starting"
        case .healthy: return "healthy"
        case .suspect: return "suspect"
        case .failingOver: return "failing_over"
        case .degraded: return "degraded"
        case .unknown(let s): return s
        }
    }

    public var isKnown: Bool { if case .unknown = self { return false } else { return true } }

    public var label: String {
        switch self {
        case .starting: return "Connecting…"
        case .healthy: return "Healthy"
        case .suspect: return "Checking…"
        case .failingOver: return "Failing over…"
        case .degraded: return "Degraded: kill switch holding"
        case .unknown: return "Unknown"
        }
    }
}

/// Where "New Server" looks: the current server's city, its country, or the pinned regions.
public enum RotationScope: String, CaseIterable, Sendable, Codable {
    case city, country, any

    public var label: String {
        switch self {
        case .city: return "Same City"
        case .country: return "Same Country"
        case .any: return "Pinned Regions"
        }
    }
}

/// What a server offers that a pick keeps and a requirement asks for.
public enum Feature: String, CaseIterable, Sendable, Codable {
    case p2p = "port_forward", streaming = "stream", ipv6

    public var name: String {
        switch self {
        case .p2p: return "P2P"
        case .streaming: return "Streaming"
        case .ipv6: return "IPv6"
        }
    }

    /// The display name of a feature string, also for values the app does not know.
    public static func name(_ wire: String) -> String {
        Feature(rawValue: wire)?.name ?? (wire == "secure_core" ? "Secure Core" : wire)
    }
}

/// How the prober loads a page when finding a server for a URL.
public enum FindMode: String, CaseIterable, Sendable, Codable {
    case auto, http, browser

    public var label: String {
        switch self {
        case .auto: return "Auto (HTTP, browser when unsure)"
        case .http: return "HTTP only (fast)"
        case .browser: return "Browser"
        }
    }
}

/// The prober's verdict on one server for one URL.
public enum FindOutcome: Sendable, Hashable {
    case ok, blocked, challenge, redirected, suspicious, inconclusive, error, other(String)

    public init(wire: String) {
        switch wire {
        case "ok": self = .ok
        case "blocked": self = .blocked
        case "challenge": self = .challenge
        case "redirected": self = .redirected
        case "suspicious": self = .suspicious
        case "inconclusive": self = .inconclusive
        case "error": self = .error
        default: self = .other(wire)
        }
    }

    public var wire: String {
        switch self {
        case .ok: return "ok"
        case .blocked: return "blocked"
        case .challenge: return "challenge"
        case .redirected: return "redirected"
        case .suspicious: return "suspicious"
        case .inconclusive: return "inconclusive"
        case .error: return "error"
        case .other(let s): return s
        }
    }

    public var icon: String {
        switch self {
        case .ok: return "✅"
        case .blocked: return "⛔️"
        case .challenge: return "🧩"
        case .redirected: return "↪️"
        case .suspicious: return "🤨"
        case .inconclusive, .other: return "❔"
        case .error: return "⚠️"
        }
    }
}

/// The settings window's tabs (also the path of `exitvpn://settings/<tab>`).
public enum SettingsTab: String, CaseIterable, Sendable {
    case general, regions, connection, account, events
}

/// The app's windows.
public enum AppRoute: Sendable, Equatable {
    case servers, find, settings(SettingsTab)

    public var windowID: String {
        switch self {
        case .servers: return "servers"
        case .find: return "find"
        case .settings: return "settings"
        }
    }
}
