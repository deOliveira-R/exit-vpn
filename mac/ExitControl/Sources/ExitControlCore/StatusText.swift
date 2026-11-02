import Foundation

/// How the exit's state reads in the menu, the menu-bar icon and Shortcuts (pure).
public enum StatusText {
    /// "US-CA#556 · Los Angeles, United States · exit IP 1.2.3.4 · Healthy"
    public static func line(_ st: Status) -> String {
        var parts = [st.server ?? "no server", st.place, "exit IP \(st.exitIp ?? "unknown")", st.health.label]
        if let e = st.entry, !e.isEmpty { parts.insert("Secure Core via \(e)", at: 1) }
        return parts.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// "IPv6 2a02:6ea0::30" while IPv6 goes through the exit, "IPv6 off on this server"
    /// otherwise (the exit answers IPv6 with an error, so apps use IPv4); nil from nodes
    /// without IPv6.
    public static func ipv6(_ st: Status) -> String? {
        guard let on = st.ipv6 else { return nil }
        return on ? "IPv6 \(st.exitIp6 ?? "—")" : "IPv6 off on this server"
    }

    /// "Quality: healthy · 21 ms · 0% loss · jitter 1 ms", "Quality: degraded (2.8% loss) ·
    /// 21 ms"; nil from nodes that do not measure it.
    public static func quality(_ q: TunnelQuality?) -> String? {
        guard let q else { return nil }
        if q.state == "measuring" { return "Quality: measuring…" }
        var parts = [q.rttMs.map { "\(Int($0.rounded())) ms" }, "\(percent(q.loss)) loss",
                     q.jitterMs.map { "jitter \(Int($0.rounded())) ms" }].compactMap { $0 }
        let head = q.reasons.isEmpty ? "Quality: \(q.state)" : "Quality: \(q.state) (\(q.reasons.joined(separator: ", ")))"
        if !q.reasons.isEmpty { parts = Array(parts.prefix(1)) }
        return ([head] + parts).joined(separator: " · ")
    }

    /// "0%", "2.8%", "35%".
    static func percent(_ fraction: Double) -> String {
        let p = fraction * 100
        if p == 0 { return "0%" }
        return p < 10 ? String(format: "%.1f%%", p) : "\(Int(p.rounded()))%"
    }

    /// "Standby US-NY#5 ready · 21 ms", "Standby US-NY#5 ready · degraded: 2.8% loss",
    /// "Standby: searching for a server", ...; nil without a standby tunnel.
    public static func standby(_ st: Status) -> String? {
        guard let sb = st.standby else { return nil }
        switch sb.state {
        case "ready":
            var line = "Standby \(sb.server ?? "—") ready"
            if let q = sb.quality, q.state != "measuring" {
                line += q.reasons.isEmpty ? (q.rttMs.map { " · \(Int($0.rounded())) ms" } ?? "")
                                          : " · \(q.state): \(q.reasons.joined(separator: ", "))"
            }
            return line
        case "searching": return "Standby: searching for a server"
        case "off": return "Standby off: slot c has no key"
        case "key_suspect": return "Standby stopped: its key carries no traffic"
        default: return "Standby \(sb.server ?? "—"): \(sb.state.replacingOccurrences(of: "_", with: " "))"
        }
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
