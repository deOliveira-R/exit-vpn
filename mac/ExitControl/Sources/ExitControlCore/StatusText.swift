import Foundation

/// How the exit's state reads in the menu, the menu-bar icon and Shortcuts (pure).
public enum StatusText {
    /// "US-CA#556 · Los Angeles, United States · exit IP 1.2.3.4 · Healthy"
    public static func line(_ st: Status) -> String {
        var parts = [st.server ?? "no server", st.place, "exit IP \(st.exitIp ?? "unknown")", st.health.label]
        if let e = st.entry, !e.isEmpty { parts.insert("Secure Core via \(e)", at: 1) }
        return parts.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// The menu-bar symbol (SF Symbols) for the state.
    public static func symbol(online: Bool, status: Status?, busy: Bool) -> String {
        guard online, let st = status else { return "shield.slash" }
        if busy { return "arrow.triangle.2.circlepath" }
        switch st.health {
        case .healthy: return st.prefs.secureCore ? "lock.shield.fill" : "shield.lefthalf.filled"
        case .starting, .suspect, .failingOver: return "exclamationmark.shield"
        case .degraded: return "xmark.shield.fill"
        case .unknown: return "shield"
        }
    }

    /// Whether `region` is where the exit is now (a country region matches any city in it).
    public static func isCurrent(_ region: Region, _ st: Status?) -> Bool {
        guard let st, st.country?.lowercased() == region.country.lowercased() else { return false }
        return region.city == nil || st.city?.lowercased() == region.city?.lowercased()
    }
}
