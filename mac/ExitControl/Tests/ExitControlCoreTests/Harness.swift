import Foundation
@testable import ExitControlCore

// The tests' harness: the contract samples, builders and fakes the test files share.

/// The repository's `contracts/` directory.
let contractsDir = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent().deletingLastPathComponent().appending(path: "contracts")

/// A contract sample (`contracts/<name>.json`), decoded as the app decodes the wire.
func sample<T: Decodable>(_ name: String, as type: T.Type = T.self) throws -> T {
    let data = try Data(contentsOf: contractsDir.appending(path: "\(name).json"))
    return try ControllerClient.decoder.decode(T.self, from: data)
}

/// A regular, enabled server with only what the server list sorts and filters on.
func server(_ name: String, _ country: String, _ city: String, load: Int?, p2p: Bool = false) -> Server {
    Server(name: name, country: country, city: city, hostname: "node.protonvpn.net", ip: name, pubkey: "k",
           secureCore: false, tor: false, free: false, stream: false, portForward: p2p,
           ipv6: nil, load: load, enabled: true, entry: nil)
}

/// An executable shell script in a fresh temporary directory (a fake exitctl or tailscale).
func fakeExecutable(_ script: String, name: String = "exitctl") throws -> URL {
    let dir = FileManager.default.temporaryDirectory.appending(path: "fake-\(name)-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let url = dir.appending(path: name)
    try script.write(to: url, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
    return url
}

/// What `tailscale set --exit-node` would be asked, for ExitNodeGuard; fails while `failWith` is set.
@MainActor final class FakeSelector {
    var selected: [String] = []
    var failWith: String?
    var clock = Date(timeIntervalSince1970: 0)

    lazy var exitGuard = ExitNodeGuard(select: { @MainActor [unowned self] node in
        if let m = failWith { throw ProcessRunner.Failure(message: m) }
        selected.append(node)
    }, now: { [unowned self] in clock })
}

/// exitctl's login answers, in order, for LoginFlow; records each step sent and each URL opened.
@MainActor final class FakeLogin {
    var calls: [LoginStep] = []
    var opened: [URL] = []
    var answers: [Result<Account, Error>] = [
        .failure(ExitctlError.needInput(what: "human_verification", message: "CAPTCHA",
                                        info: ["url": .string("https://verify.example/?token=t"), "token": .string("t")])),
        .failure(ExitctlError.needInput(what: "totp", message: "2FA", info: [:])),
        .success(Account(account: "u", loggedIn: true, needs2fa: nil)),
    ]

    lazy var flow: LoginFlow = {
        let f = LoginFlow(login: { [unowned self] step in calls.append(step); return try answers.removeFirst().get() },
                          openURL: { [unowned self] in opened.append($0) })
        f.username = "u"; f.password = "pw"
        return f
    }()
}

/// An ActionRunner busy with a change named "first" until `release()`.
@MainActor struct BusyRunner {
    let runner = ActionRunner()
    let gate = AsyncStream<Void>.makeStream()

    func start(_ log: @escaping @MainActor (String) -> Void) async -> Task<Bool, Error> {
        let t = Task { [runner, gate] in try await runner.submit("first") { log("first"); for await _ in gate.stream { break } } }
        while runner.busy == nil { await Task.yield() }
        return t
    }

    func release() { gate.continuation.yield(()) }
}

/// A connected ConnectionStore, the status sample, and the notifications it has posted so far.
@MainActor func connectedStore() throws -> (ConnectionStore, Status, () -> [ConnectionStore.Notable]) {
    let c = ConnectionStore()
    var notes: [ConnectionStore.Notable] = []
    c.onNotable = { notes.append($0) }
    let st = try sample("controller/status", as: Status.self)
    c.handle(.connected)
    return (c, st, { notes })
}

/// A provider store with Proton-like levers and slot a as the node reports it (`meta`: what exitctl recorded).
@MainActor func providerStore(meta: String) throws -> ProviderStore {
    let slot = try ControllerClient.decoder.decode(KeySlot.self, from: Data("""
    {"slot":"a","role":"exit","source":"uploaded","public_key":"k","options":{"NetShield":"1","VPN Accelerator":"off"},
     "meta":\(meta),"expires_in_days":null}
    """.utf8))
    let options = try ControllerClient.decoder.decode([ProviderOption].self, from: Data("""
    [{"key":"netshield","label":"NetShield","conf_label":"NetShield","kind":"choice","default":0,"choices":{"0":"off","1":"m","2":"a"},"help":""},
     {"key":"accelerator","label":"VPN Accelerator","conf_label":"VPN Accelerator","kind":"bool","default":true,"choices":{},"help":""},
     {"key":"moderate_nat","label":"Moderate NAT","conf_label":"Moderate NAT","kind":"bool","default":false,"choices":{},"help":""}]
    """.utf8))
    let p = ProviderStore()
    p.setForTests(options: options, keys: [slot])
    return p
}

/// A fake exitctl that answers as the real one does: login needs 2FA, options lists NetShield.
func fakeExitctl() throws -> Exitctl {
    let exe = try fakeExecutable("""
    #!/bin/sh
    case "$2" in
      login) cat > /dev/null; echo '{"error": "a 2FA code is needed", "need": "totp"}'; exit 3 ;;
      options) echo '[{"key":"netshield","label":"NetShield","kind":"choice","default":0,"choices":{"0":"off","1":"malware","2":"ads"},"help":""}]' ;;
      *) echo '{"error": "boom"}'; exit 1 ;;
    esac
    """)
    return Exitctl(executable: exe, controllerURL: "http://exit-proton:8080")
}

/// Counts calls across concurrency domains.
actor Counter {
    private var n = 0
    func next() -> Int { defer { n += 1 }; return n }
}
