import Foundation

/// `exitvpn://` URLs (PLAN Phase 5b): windows, and actions for scripts, Shortcuts and
/// other apps. macOS asks before a web page opens a custom scheme, so a page cannot act
/// silently.
public enum ExitURL: Equatable, Sendable {
    case window(String, tab: String?)   // servers, find, settings[/tab]
    case rotate(String)                 // city, country, any
    case connect(Region)
    case connectServer(String)          // a logical name (CH#821): its least loaded server
    case secureCore(Bool)
    case refresh

    public static let scheme = "exitvpn"
    static let windows = ["servers", "find", "settings"]
    static let scopes = ["city", "country", "any"]

    public init?(_ url: URL) {
        guard url.scheme?.lowercased() == Self.scheme, let host = url.host()?.lowercased() else { return nil }
        let q = Dictionary(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?
            .compactMap { i in i.value.map { (i.name.lowercased(), $0) } } ?? [], uniquingKeysWith: { a, _ in a })
        switch host {
        case _ where Self.windows.contains(host):
            self = .window(host, tab: url.pathComponents.dropFirst().first)
        case "rotate":
            let scope = q["scope"]?.lowercased() ?? "any"
            guard Self.scopes.contains(scope) else { return nil }
            self = .rotate(scope)
        case "connect":
            if let server = q["server"], !server.isEmpty {
                self = .connectServer(server)
            } else if let country = q["country"], !country.isEmpty {
                self = .connect(Region(country: country, city: q["city"].flatMap { $0.isEmpty ? nil : $0 }))
            } else { return nil }
        case "securecore":
            guard let on = q["on"]?.lowercased(), ["1", "0", "true", "false", "on", "off"].contains(on) else { return nil }
            self = .secureCore(["1", "true", "on"].contains(on))
        case "refresh":
            self = .refresh
        default:
            return nil
        }
    }
}
