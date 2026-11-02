import Foundation

/// Where an app update stands, as the menu shows it (like Tailscale's menu: "Version 1.2
/// available / Click to download", then "Downloading…" with its progress). Sparkle drives
/// it (the app's `Updater`); this holds only what the menu reads.
public enum UpdateProgress: Equatable, Sendable {
    case idle
    case checking
    case available(version: String)
    case downloading(version: String, received: UInt64, expected: UInt64)
    case extracting(version: String, fraction: Double)
    case installing(version: String)
    case failed(message: String)

    /// The menu item's two lines (nil: no item). Clickable only when `isActionable`.
    public var title: String? {
        switch self {
        case .idle: return nil
        case .checking: return "Checking for Updates…"
        case .available(let v): return "Version \(v) available"
        case .downloading: return "Downloading…"
        case .extracting: return "Preparing the update…"
        case .installing: return "Installing and relaunching…"
        case .failed: return "Update failed"
        }
    }

    public var subtitle: String? {
        switch self {
        case .available: return "Click to download and install"
        case .downloading(let v, _, let total):
            guard total > 0 else { return "Version \(v)" }
            return "Version \(v): \(Int((fraction ?? 0) * 100))% of \(Self.megabytes(total))"
        case .extracting(let v, _), .installing(let v): return "Version \(v)"
        case .failed(let m): return m
        default: return nil
        }
    }

    /// 0…1 while downloading or extracting; nil when unknown.
    public var fraction: Double? {
        switch self {
        case .downloading(_, let got, let total):
            return total > 0 ? min(1, Double(got) / Double(total)) : nil
        case .extracting(_, let f): return f
        default: return nil
        }
    }

    /// Whether clicking the item does something (start the download, or dismiss a failure).
    public var isActionable: Bool {
        switch self {
        case .available, .failed: return true
        default: return false
        }
    }

    /// An update is on its way: the menu-bar icon shows it.
    public var isPending: Bool {
        switch self {
        case .idle, .checking, .failed: return false
        default: return true
        }
    }

    public var version: String? {
        switch self {
        case .available(let v), .downloading(let v, _, _), .extracting(let v, _), .installing(let v): return v
        default: return nil
        }
    }

    // Steps (what Sparkle reports) ---------------------------------------------------------
    public func receivedExpectedLength(_ length: UInt64) -> UpdateProgress {
        guard let v = version else { return self }
        return .downloading(version: v, received: 0, expected: length)
    }

    public func received(_ length: UInt64) -> UpdateProgress {
        if case .downloading(let v, let got, let total) = self {
            return .downloading(version: v, received: got + length, expected: total)
        }
        return self
    }

    /// The GitHub release page of `version`, from the feed URL
    /// (`…/releases/latest/download/appcast.xml` → `…/releases/tag/v<version>`).
    public static func releaseNotesURL(feed: URL?, version: String) -> URL? {
        guard let feed, let r = feed.absoluteString.range(of: "/releases/") else { return nil }
        return URL(string: feed.absoluteString[..<r.upperBound] + "tag/v\(version)")
    }

    static func megabytes(_ bytes: UInt64) -> String {
        String(format: "%.1f MB", Double(bytes) / 1_000_000)
    }
}
