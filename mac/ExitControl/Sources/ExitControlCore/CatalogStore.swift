import Foundation
import Observation

/// The controller's server list, for the browser, the menus and flags. Loaded on connect and
/// reloaded when the controller's catalog changes (a push or a revert).
@MainActor @Observable
public final class CatalogStore {
    public private(set) var servers: [Server] = []
    public private(set) var countries: [String] = []
    /// Catalog country → ISO code, for flags.
    public private(set) var codes: [String: String] = [:]
    public private(set) var error: String?
    private var api: (any ControllerAPI)?

    public init() {}

    public func connect(_ api: (any ControllerAPI)?) {
        self.api = api
        servers = []; countries = []; codes = [:]
    }

    public func load() async {
        guard let api else { return }
        do {
            let r = try await api.servers(country: nil)
            servers = r.servers
            countries = r.countries ?? Array(Set(r.servers.map(\.country))).sorted()
            var c: [String: String] = [:]
            for s in r.servers where c[s.country] == nil { c[s.country] = countryCode(s) }
            codes = c
            error = nil
        } catch {
            self.error = "Server list: \(error.localizedDescription)"
        }
    }

    public func loadIfEmpty() async { if servers.isEmpty { await load() } }

    public func server(named name: String) -> Server? {
        servers.first { $0.name.caseInsensitiveCompare(name) == .orderedSame }
    }

    public func cities(in country: String) -> [String] {
        Array(Set(servers.filter { $0.country == country && !$0.secureCore }.map(\.city))).sorted()
    }

    /// Countries grouped by continent, in `continents` order, for the menu.
    public var byContinent: [(continent: String, countries: [String])] {
        let groups = Dictionary(grouping: countries) { continent(codes[$0]) }
        return (continents + ["Other"]).compactMap { c in groups[c].map { (c, $0) } }
    }
}
