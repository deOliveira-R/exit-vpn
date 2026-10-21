import Foundation

/// `exitvpn://` URLs: windows, and actions for scripts, Shortcuts and other
/// apps. macOS asks before a web page opens a custom scheme, so a page cannot act silently.
///
///     exitvpn://servers | find | settings[/general|regions|connection|account|events]
///     exitvpn://rotate?scope=city|country|any        (default any)
///     exitvpn://connect?country=…[&city=…] | connect?server=CH%23821
///     exitvpn://securecore?on=1|0
///     exitvpn://refresh
public enum ExitURL: Equatable, Sendable {
    case open(AppRoute)
    case run(ControllerCommand)
    case refresh

    public static let scheme = "exitvpn"

    public init?(_ url: URL) {
        guard url.scheme?.lowercased() == Self.scheme, let host = url.host()?.lowercased() else { return nil }
        let q = Dictionary(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?
            .compactMap { i in i.value.map { (i.name.lowercased(), $0) } } ?? [], uniquingKeysWith: { a, _ in a })
        switch host {
        case "servers": self = .open(.servers)
        case "find": self = .open(.find)
        case "settings":
            let tab = url.pathComponents.dropFirst().first.flatMap { SettingsTab(rawValue: $0.lowercased()) }
            self = .open(.settings(tab ?? .general))
        case "rotate":
            guard let scope = RotationScope(rawValue: q["scope"]?.lowercased() ?? "any") else { return nil }
            self = .run(.rotate(scope))
        case "connect":
            if let server = q["server"], !server.isEmpty {
                self = .run(.connectServer(name: server, ip: nil))
            } else if let country = q["country"], !country.isEmpty {
                self = .run(.connect(Region(country: country, city: q["city"].flatMap { $0.isEmpty ? nil : $0 })))
            } else { return nil }
        case "securecore":
            guard let on = q["on"]?.lowercased(), ["1", "0", "true", "false", "on", "off"].contains(on) else { return nil }
            self = .run(.secureCore(["1", "true", "on"].contains(on)))
        case "refresh":
            self = .refresh
        default:
            return nil
        }
    }
}
