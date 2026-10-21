import Foundation

/// The server browser's filters and order (pure). Rows of the current Secure Core mode only
/// (the other mode's servers cannot be picked). Ties go by proximity, then load: country,
/// city, load, name, IP, a total order, so rows never swap places when the table redraws.
public struct ServerQuery: Sendable, Equatable {
    public var search = ""
    public var country = ""           // "" = all
    public var p2p = false
    public var streaming = false
    public var ipv6 = false
    public var hideMaintenance = true
    public var secureCore = false

    public init() {}

    public func matches(_ s: Server) -> Bool {
        let q = search.lowercased()
        return s.secureCore == secureCore && !s.tor
            && (country.isEmpty || s.country == country)
            && (!p2p || s.portForward) && (!streaming || s.stream) && (!ipv6 || s.hasIPv6)
            && (!hideMaintenance || s.isEnabled)
            && (q.isEmpty || s.name.lowercased().contains(q) || s.city.lowercased().contains(q)
                || s.country.lowercased().contains(q) || s.ip.contains(q))
    }

    public static func nearestFirst(_ a: Server, _ b: Server) -> Bool {
        let ka = (a.country, a.city, a.load ?? 50), kb = (b.country, b.city, b.load ?? 50)
        if ka != kb { return ka < kb }
        switch a.name.localizedStandardCompare(b.name) {
        case .orderedAscending: return true
        case .orderedDescending: return false
        case .orderedSame: return a.ip < b.ip
        }
    }
}
