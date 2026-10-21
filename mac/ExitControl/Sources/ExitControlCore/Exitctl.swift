import Foundation

/// Provider actions run through `exitctl --json` (D20): login, keys, levers, catalog. exitctl
/// is installed separately (`uv tool install ./helper`), so the app never bundles GPLv3 code.
/// Exit codes (contracts/README.md): 0 done, 1 error, 2 the exit node refused, 3 input needed.
public enum ExitctlError: Error, LocalizedError, Sendable, Equatable {
    case notInstalled
    case needInput(what: String, message: String, info: [String: JSONValue])
    case failed(code: Int32, message: String)

    public var errorDescription: String? {
        switch self {
        case .notInstalled: return "exitctl not found (install it: uv tool install ./helper)"
        case .needInput(_, let m, _): return m
        case .failed(_, let m): return m
        }
    }
}

/// What the app asks of the provider (`Exitctl`; a fake in tests).
public protocol ProviderCLI: Sendable {
    func options(_ provider: String) async throws -> [ProviderOption]
    func whoami(_ provider: String) async throws -> Account
    func certificates(_ provider: String) async throws -> [Certificate]
    func login(_ provider: String, _ step: LoginStep) async throws -> Account
    func perform(_ provider: String, _ command: ProviderCommand) async throws
}

/// Everything the owner can ask the provider (through exitctl) to do.
public enum ProviderCommand: Sendable, Equatable {
    case logout
    case rotateKey(slot: String, options: [String: String])   // lever values in flag form
    case renewKey(slot: String)
    case deleteCertificate(id: String)
    case pushServers
    case revertServers

    public var label: String {
        switch self {
        case .logout: return "Log out"
        case .rotateKey(let s, let o): return o.isEmpty ? "New key, slot \(s)" : "New key with options"
        case .renewKey(let s): return "Renew slot \(s)"
        case .deleteCertificate: return "Delete certificate"
        case .pushServers: return "Push server list"
        case .revertServers: return "Revert server list"
        }
    }

    var arguments: [String] {
        switch self {
        case .logout: return ["logout"]
        case .rotateKey(let slot, let options):
            return ["rotate", "--slot", slot] + options.sorted { $0.key < $1.key }.flatMap {
                ["--\($0.key.replacingOccurrences(of: "_", with: "-"))", $0.value]
            }
        case .renewKey(let slot): return ["renew", "--slot", slot]
        case .deleteCertificate(let id): return ["delete", id]
        case .pushServers: return ["servers", "--push"]
        case .revertServers: return ["servers", "--revert"]
        }
    }
}

public struct Exitctl: ProviderCLI {
    public var executable: URL
    public var controllerURL: String
    var runner = ProcessRunner(timeout: 300)   // a key rotation can take ~2 minutes

    public init(executable: URL, controllerURL: String) {
        self.executable = executable
        self.controllerURL = controllerURL
    }

    /// Where `uv tool install` and common setups put it.
    public static func locate(override: String? = nil) -> URL? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [override, "\(home)/.local/bin/exitctl", "/opt/homebrew/bin/exitctl",
                          "/usr/local/bin/exitctl"].compactMap { $0 }.filter { !$0.isEmpty }
        return candidates.map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath) }
            .first { FileManager.default.isExecutableFile(atPath: $0.path) }
    }

    /// Runs `exitctl <args> --json`; `stdin` is written then closed (login credentials).
    public func run(_ args: [String], stdin: Data? = nil) async throws -> Data {
        let r = try await runner.run(executable, args + ["--json", "--url", controllerURL], stdin: stdin)
        if r.code == 0 { return r.stdout }
        let obj = (try? JSONDecoder().decode([String: JSONValue].self, from: r.stdout)) ?? [:]
        let msg = obj["error"]?.string ?? r.lastErrorLine ?? "exitctl failed (\(r.code))"
        if r.code == 3, let what = obj["need"]?.string {
            throw ExitctlError.needInput(what: what, message: msg, info: obj)
        }
        throw ExitctlError.failed(code: r.code, message: msg)
    }

    func decode<T: Decodable>(_ t: T.Type, _ args: [String], stdin: Data? = nil) async throws -> T {
        try ControllerClient.decoder.decode(T.self, from: try await run(args, stdin: stdin))
    }

    public func providers() async throws -> [ProviderInfo] { try await decode([ProviderInfo].self, ["providers"]) }
    public func options(_ p: String) async throws -> [ProviderOption] { try await decode([ProviderOption].self, [p, "options"]) }
    public func whoami(_ p: String) async throws -> Account { try await decode(Account.self, [p, "whoami"]) }
    public func certificates(_ p: String) async throws -> [Certificate] { try await decode([Certificate].self, [p, "list"]) }

    public func login(_ p: String, _ step: LoginStep) async throws -> Account {
        let e = JSONEncoder(); e.keyEncodingStrategy = .convertToSnakeCase
        return try await decode(Account.self, [p, "login", "--stdin"], stdin: try e.encode(step))
    }

    public func perform(_ p: String, _ command: ProviderCommand) async throws {
        _ = try await run([p] + command.arguments)
    }
}
