import Foundation

/// Provider actions run through `exitctl --json` (D20): login, keys, levers, catalog.
/// exitctl is installed separately (`uv tool install ./helper`), so the app never bundles
/// GPLv3 code. Exit codes: 0 ok, 1 error, 2 controller refused (409), 3 needs input.
public enum ExitctlError: Error, LocalizedError, Sendable {
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

public struct ProviderOption: Codable, Hashable, Sendable, Identifiable {
    public var key: String
    public var label: String
    public var confLabel: String?  // how the .conf comments name it ("NetShield")
    public var kind: String        // "bool" | "choice"
    public var `default`: JSONValue
    public var choices: [String: String]
    public var help: String
    public var id: String { key }
    /// Choice values in numeric order.
    public var sortedChoices: [(value: String, label: String)] {
        choices.sorted { (Int($0.key) ?? 0) < (Int($1.key) ?? 0) }.map { ($0.key, $0.value) }
    }
}

public struct Account: Codable, Sendable, Equatable {
    public var account: String?
    public var loggedIn: Bool?
    public var needs2fa: Bool?
    public var isLoggedIn: Bool { account != nil && loggedIn != false && needs2fa != true }
}

public struct Certificate: Codable, Hashable, Sendable, Identifiable {
    public var id: String?
    public var deviceName: String
    public var expiresAt: Double?
    public var options: [String: JSONValue]
    public var wgPublicKey: String?
    public var slot: String?
    public var orphan: Bool?
    public var stableID: String { id ?? deviceName }
}

public struct ProviderInfo: Codable, Hashable, Sendable {
    public var name: String
    public var title: String
    public var capabilities: [String]
}

public struct Exitctl: Sendable {
    public var executable: URL
    public var controllerURL: String

    public init(executable: URL, controllerURL: String) {
        self.executable = executable
        self.controllerURL = controllerURL
    }

    /// Where `uv tool install` and common setups put it; the repo's venv last.
    public static func locate(override: String? = nil) -> URL? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [override, "\(home)/.local/bin/exitctl", "/opt/homebrew/bin/exitctl",
                          "/usr/local/bin/exitctl"].compactMap { $0 }.filter { !$0.isEmpty }
        return candidates.map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath) }
            .first { FileManager.default.isExecutableFile(atPath: $0.path) }
    }

    /// Runs `exitctl <args> --json`; `stdin` is written then closed (used for login
    /// credentials, which never appear in argv).
    public func run(_ args: [String], stdin: Data? = nil) async throws -> Data {
        let exe = executable, url = controllerURL
        return try await withCheckedThrowingContinuation { cont in
            DispatchQueue.global().async {
                let p = Process()
                p.executableURL = exe
                p.arguments = args + ["--json", "--url", url]
                var env = ProcessInfo.processInfo.environment
                // GUI apps get a minimal PATH; proton-core needs gpg (Homebrew)
                env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
                p.environment = env
                let out = Pipe(), err = Pipe(), inp = Pipe()
                p.standardOutput = out; p.standardError = err; p.standardInput = inp
                do { try p.run() } catch {
                    cont.resume(throwing: ExitctlError.failed(code: -1, message: "exitctl: \(error.localizedDescription)"))
                    return
                }
                if let stdin { inp.fileHandleForWriting.write(stdin) }
                try? inp.fileHandleForWriting.close()
                let data = out.fileHandleForReading.readDataToEndOfFile()
                let errText = String(decoding: err.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                p.waitUntilExit()
                let code = p.terminationStatus
                if code == 0 { cont.resume(returning: data); return }
                let obj = (try? JSONDecoder().decode([String: JSONValue].self, from: data)) ?? [:]
                let msg = obj["error"]?.string ?? errText.split(separator: "\n").last.map(String.init) ?? "exitctl failed (\(code))"
                if code == 3, let what = obj["need"]?.string {
                    cont.resume(throwing: ExitctlError.needInput(what: what, message: msg, info: obj))
                } else {
                    cont.resume(throwing: ExitctlError.failed(code: code, message: msg))
                }
            }
        }
    }

    func decode<T: Decodable>(_ t: T.Type, _ args: [String], stdin: Data? = nil) async throws -> T {
        try ControllerClient.decoder.decode(T.self, from: try await run(args, stdin: stdin))
    }

    public func providers() async throws -> [ProviderInfo] { try await decode([ProviderInfo].self, ["providers"]) }
    public func options(_ p: String) async throws -> [ProviderOption] { try await decode([ProviderOption].self, [p, "options"]) }
    public func whoami(_ p: String) async throws -> Account { try await decode(Account.self, [p, "whoami"]) }
    public func certificates(_ p: String) async throws -> [Certificate] { try await decode([Certificate].self, [p, "list"]) }
    public func logout(_ p: String) async throws { _ = try await run([p, "logout"]) }
    public func delete(_ p: String, id: String) async throws { _ = try await run([p, "delete", id]) }
    public func renew(_ p: String, slot: String) async throws { _ = try await run([p, "renew", "--slot", slot]) }
    public func pushServers(_ p: String) async throws { _ = try await run([p, "servers", "--push"]) }
    public func revertServers(_ p: String) async throws { _ = try await run([p, "servers", "--revert"]) }

    /// New key for a slot with the given lever values (flag form: key → "on"/"off"/"2").
    public func rotate(_ p: String, slot: String, options: [String: String]) async throws {
        var args = [p, "rotate", "--slot", slot]
        for (k, v) in options.sorted(by: { $0.key < $1.key }) {
            args += ["--\(k.replacingOccurrences(of: "_", with: "-"))", v]
        }
        _ = try await run(args)
    }

    /// One login step. Credentials go on stdin as JSON; a missing 2FA code or a CAPTCHA
    /// comes back as `.needInput` (what: "totp" / "human_verification" with a "url").
    public struct LoginStep: Encodable, Sendable {
        public var username: String?
        public var password: String?
        public var totp: String?
        public var hvToken: String?
        public var hvType: String?
        public init(username: String? = nil, password: String? = nil, totp: String? = nil,
                    hvToken: String? = nil, hvType: String? = nil) {
            self.username = username; self.password = password; self.totp = totp
            self.hvToken = hvToken; self.hvType = hvType
        }
    }

    public func login(_ p: String, _ step: LoginStep) async throws -> Account {
        let e = JSONEncoder(); e.keyEncodingStrategy = .convertToSnakeCase
        return try await decode(Account.self, [p, "login", "--stdin"], stdin: try e.encode(step))
    }
}
