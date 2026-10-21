import Foundation

public struct APIError: Error, LocalizedError, Sendable {
    public var status: Int
    public var detail: String
    public var errorDescription: String? { status == 0 ? detail : "\(detail) (HTTP \(status))" }
}

/// The controller's HTTP API. Auth is Tailscale identity (whois on the node), so there
/// are no credentials here.
public struct ControllerClient: Sendable {
    public var baseURL: URL
    let session: URLSession

    public init(baseURL: URL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    public static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        return e
    }()

    func request(_ method: String, _ path: String, query: [URLQueryItem] = [],
                 body: (any Encodable)? = nil, timeout: TimeInterval = 15) async throws -> Data {
        var comps = URLComponents(url: baseURL.appending(path: path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty { comps.queryItems = query }
        var req = URLRequest(url: comps.url!, timeoutInterval: timeout)
        req.httpMethod = method
        if let body {
            req.httpBody = try Self.encoder.encode(body)
            req.setValue("application/json", forHTTPHeaderField: "content-type")
        }
        let data: Data, resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw APIError(status: 0, detail: "controller unreachable: \(error.localizedDescription)")
        }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            let detail = (try? JSONDecoder().decode([String: JSONValue].self, from: data))?["detail"]
            throw APIError(status: code, detail: detail?.description ?? String(decoding: data, as: UTF8.self))
        }
        return data
    }

    func get<T: Decodable>(_ type: T.Type, _ method: String, _ path: String,
                           query: [URLQueryItem] = [], body: (any Encodable)? = nil,
                           timeout: TimeInterval = 15) async throws -> T {
        let data = try await request(method, path, query: query, body: body, timeout: timeout)
        return try Self.decoder.decode(T.self, from: data)
    }

    // Reads
    public func status() async throws -> Status { try await get(Status.self, "GET", "v1/status") }
    public func keys() async throws -> [KeySlot] { try await get(KeysResponse.self, "GET", "v1/keys").slots }
    public func servers(country: String? = nil) async throws -> ServersResponse {
        try await get(ServersResponse.self, "GET", "v1/servers",
                      query: country.map { [URLQueryItem(name: "country", value: $0)] } ?? [], timeout: 30)
    }
    public func findJob(_ id: String) async throws -> FindJob {
        try await get(FindJob.self, "GET", "v1/find/\(id)")
    }

    // Actions (a switch waits for the new server to pass health checks: long timeouts)
    struct Scope: Encodable { var scope: String }
    struct Select: Encodable { var server: String?; var ip: String?; var country: String?; var city: String? }
    public struct PrefsPatch: Encodable, Sendable {
        public var regions: [Region]?
        public var autoFailover: Bool?
        public var secureCore: Bool?
        public var allowFree: Bool?
        public var require: [String]?
        public var favorites: [String]?
        public init(regions: [Region]? = nil, autoFailover: Bool? = nil, secureCore: Bool? = nil,
                    allowFree: Bool? = nil, require: [String]? = nil, favorites: [String]? = nil) {
            self.regions = regions; self.autoFailover = autoFailover; self.secureCore = secureCore
            self.allowFree = allowFree; self.require = require; self.favorites = favorites
        }
    }
    struct Find: Encodable { var url: String; var countries: [String]; var maxCandidates: Int; var mode: String; var js: Bool }
    struct Apply: Encodable { var server: String; var ip: String? }

    @discardableResult
    public func rotate(_ scope: String) async throws -> Status {
        try await get(Status.self, "POST", "v1/rotate", body: Scope(scope: scope), timeout: 180)
    }
    @discardableResult
    public func select(server: String, ip: String?) async throws -> Status {
        try await get(Status.self, "POST", "v1/select", body: Select(server: server, ip: ip), timeout: 180)
    }
    @discardableResult
    public func select(region: Region) async throws -> Status {
        try await get(Status.self, "POST", "v1/select",
                      body: Select(country: region.country, city: region.city), timeout: 180)
    }
    @discardableResult
    public func setPrefs(_ patch: PrefsPatch) async throws -> Prefs {
        try await get(Prefs.self, "PUT", "v1/prefs", body: patch, timeout: 180)
    }
    public func find(url: String, countries: [String], max: Int = 10, mode: String = "auto",
                     js: Bool = false) async throws -> FindJob {
        try await get(FindJob.self, "POST", "v1/find",
                      body: Find(url: url, countries: countries, maxCandidates: max, mode: mode, js: js))
    }
    @discardableResult
    public func applyFind(job: String, server: String, ip: String?) async throws -> Status {
        try await get(Status.self, "POST", "v1/find/\(job)/apply", body: Apply(server: server, ip: ip), timeout: 180)
    }
}
