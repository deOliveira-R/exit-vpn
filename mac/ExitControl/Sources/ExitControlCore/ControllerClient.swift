import Foundation

public struct APIError: Error, LocalizedError, Sendable {
    public var status: Int
    public var detail: String
    public var errorDescription: String? { status == 0 ? detail : "\(detail) (HTTP \(status))" }
}

/// The controller's HTTP API (contracts/controller/). Auth is Tailscale identity (whois on
/// the node), so there are no credentials here.
public struct ControllerClient: ControllerAPI {
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
    public func find(url: String, countries: [String], max: Int = 10, mode: FindMode = .auto,
                     js: Bool = false) async throws -> FindJob {
        try await get(FindJob.self, "POST", "v1/find",
                      body: Find(url: url, countries: countries, maxCandidates: max, mode: mode.rawValue, js: js))
    }

    // Changes (a switch waits for the new server to pass health checks: long timeouts)
    struct Scope: Encodable { var scope: String }
    struct Select: Encodable { var server: String?; var ip: String?; var country: String?; var city: String? }
    struct PrefsPatch: Encodable {
        var regions: [Region]?
        var autoFailover: Bool?
        var secureCore: Bool?
        var require: [String]?
        var favorites: [String]?
    }
    struct Find: Encodable { var url: String; var countries: [String]; var maxCandidates: Int; var mode: String; var js: Bool }
    struct Apply: Encodable { var server: String; var ip: String? }

    public func perform(_ command: ControllerCommand) async throws {
        let long: TimeInterval = 180
        switch command {
        case .rotate(let scope):
            _ = try await request("POST", "v1/rotate", body: Scope(scope: scope.rawValue), timeout: long)
        case .connect(let r):
            _ = try await request("POST", "v1/select", body: Select(country: r.country, city: r.city), timeout: long)
        case .connectServer(let name, let ip):
            _ = try await request("POST", "v1/select", body: Select(server: name, ip: ip), timeout: long)
        case .secureCore(let on):
            _ = try await request("PUT", "v1/prefs", body: PrefsPatch(secureCore: on), timeout: long)
        case .autoFailover(let on):
            _ = try await request("PUT", "v1/prefs", body: PrefsPatch(autoFailover: on))
        case .regions(let r):
            _ = try await request("PUT", "v1/prefs", body: PrefsPatch(regions: r))
        case .require(let f):
            _ = try await request("PUT", "v1/prefs", body: PrefsPatch(require: f.map(\.rawValue)))
        case .favorites(let f):
            _ = try await request("PUT", "v1/prefs", body: PrefsPatch(favorites: f))
        case .applyFind(let job, let server, let ip):
            _ = try await request("POST", "v1/find/\(job)/apply", body: Apply(server: server, ip: ip), timeout: long)
        }
    }
}
