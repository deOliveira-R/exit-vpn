import Foundation
import Testing
@testable import ExitControlCore

// The app's state objects with fakes: the action runner's two policies, the connection's
// stream handling and notifications, provider lever resolution, the login steps, the server
// query, and the process runner's limits.

private func sample<T: Decodable>(_ name: String, as type: T.Type) throws -> T {
    let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let data = try Data(contentsOf: root.appending(path: "contracts/\(name).json"))
    return try ControllerClient.decoder.decode(T.self, from: data)
}

@MainActor @Test func runnerRejectsMenuClicksWhileBusyAndQueuesURLsInOrder() async throws {
    let runner = ActionRunner()
    var order: [String] = []
    var refreshed = 0
    runner.afterSuccess = { refreshed += 1 }
    let gate = AsyncStream<Void>.makeStream()
    let first = Task { try await runner.submit("first") { order.append("first"); for await _ in gate.stream { break } } }
    while runner.busy == nil { await Task.yield() }
    #expect(try await runner.submit("click") { order.append("click") } == false, "refused while busy")
    let queued = (1...3).map { i in Task { try await runner.submit("url \(i)", policy: .enqueue) { order.append("url \(i)") } } }
    try await Task.sleep(for: .milliseconds(50))
    gate.continuation.yield(())
    _ = try await first.value
    for t in queued { _ = try await t.value }
    #expect(order == ["first", "url 1", "url 2", "url 3"])
    #expect(refreshed == 4 && runner.busy == nil && runner.isIdle)
}

@MainActor @Test func runnerKeepsTheLastErrorAndReportsIt() async throws {
    struct Boom: LocalizedError { var errorDescription: String? { "no candidate worked" } }
    let runner = ActionRunner()
    var failures: [String] = []
    runner.onFailure = { label, msg in failures.append("\(label): \(msg)") }
    await #expect(throws: Boom.self) { try await runner.submit("Rotate") { throw Boom() } }
    #expect(runner.lastError == "Rotate: no candidate worked" && failures == ["Rotate: no candidate worked"])
    try await runner.submit("ok") {}
    #expect(runner.lastError == nil)
}

@MainActor @Test func connectionTracksStreamStateAndNotifiesOnce() throws {
    let c = ConnectionStore()
    var notes: [ConnectionStore.Notable] = []
    c.onNotable = { notes.append($0) }
    var st = try sample("controller/status", as: Status.self)
    st.warnings = ["slot a key certificate expires in 5 days"]
    c.handle(.connected)
    c.handle(.status(st))
    #expect(c.online && c.status?.server == st.server && notes.isEmpty, "warnings present at start: no notification")
    st.warnings = ["slot a key certificate expires in 5 days", "pushed catalog is 9 days old"]
    c.handle(.status(st))
    #expect(notes == [.warning("pushed catalog is 9 days old")])
    let events = try sample("controller/events", as: [String: Event].self)
    c.handle(.event(events["degraded"]!))
    c.handle(.event(events["switched"]!))
    #expect(notes.count == 2, "degraded is notable, switched is not")
    c.handle(.incompatible("unreadable status"))
    #expect(c.incompatible != nil)
    c.handle(.status(st))
    #expect(c.incompatible == nil)
    c.handle(.disconnected("timed out"))
    #expect(!c.online && c.offlineReason == "timed out")
}

@MainActor @Test func connectionLogIsBounded() throws {
    let c = ConnectionStore()
    let ev = try sample("controller/events", as: [String: Event].self)["health"]!
    for _ in 0..<(ConnectionStore.logLimit + 20) { c.append(ev) }
    #expect(c.log.count == ConnectionStore.logLimit)
}

@MainActor @Test func providerLeversFollowExitctl() throws {
    let p = ProviderStore()
    let options = try sample("exitctl/options", as: [ProviderOption].self)
    #expect(!options.isEmpty)
    // the store's own resolution: defaults, then the .conf comments, then what exitctl recorded
    let json = """
    {"slot":"a","role":"exit","source":"uploaded","public_key":"k","options":{"NetShield":"1","VPN Accelerator":"off"},
     "meta":{"provider":"proton","options":{"netshield":2}},"expires_in_days":null}
    """
    let slot = try ControllerClient.decoder.decode(KeySlot.self, from: Data(json.utf8))
    let opts = """
    [{"key":"netshield","label":"NetShield","conf_label":"NetShield","kind":"choice","default":0,"choices":{"0":"off","1":"m","2":"a"},"help":""},
     {"key":"accelerator","label":"VPN Accelerator","conf_label":"VPN Accelerator","kind":"bool","default":true,"choices":{},"help":""},
     {"key":"moderate_nat","label":"Moderate NAT","conf_label":"Moderate NAT","kind":"bool","default":false,"choices":{},"help":""}]
    """
    p.setForTests(options: try ControllerClient.decoder.decode([ProviderOption].self, from: Data(opts.utf8)), keys: [slot])
    #expect(p.levers(slot: "a") == ["netshield": "2", "accelerator": "off", "moderate_nat": "off"])
    #expect(p.levers(slot: "b") == [:])
}

@MainActor @Test func loginFlowStepsThroughTotpAndCaptcha() async throws {
    var calls: [LoginStep] = []
    var answers: [Result<Account, Error>] = [
        .failure(ExitctlError.needInput(what: "human_verification", message: "CAPTCHA",
                                        info: ["url": .string("https://verify.example/?token=t"), "token": .string("t")])),
        .failure(ExitctlError.needInput(what: "totp", message: "2FA", info: [:])),
        .success(Account(account: "u", loggedIn: true, needs2fa: nil)),
    ]
    var opened: [URL] = []
    let flow = LoginFlow(login: { step in calls.append(step); return try answers.removeFirst().get() },
                         openURL: { opened.append($0) })
    flow.username = "u"; flow.password = "pw"
    await flow.next()
    guard case .captcha(_, "t") = flow.step else { Issue.record("expected the CAPTCHA step"); return }
    #expect(opened.count == 1)
    await flow.next()
    #expect(flow.step == .totp && flow.password.isEmpty, "the password is forgotten at the 2FA step")
    flow.totp = "123456"
    await flow.next()
    #expect(flow.step == .done)
    #expect(calls == [LoginStep(username: "u", password: "pw"),
                      LoginStep(username: "u", password: "pw", hvToken: "t", hvType: "captcha"),
                      LoginStep(totp: "123456")])
}

@Test func serverQueryFiltersAndOrdersNearestFirst() throws {
    let all = try sample("controller/servers", as: ServersResponse.self).servers
    let s = all[0]
    func server(_ name: String, _ country: String, _ city: String, load: Int?, p2p: Bool = false) -> Server {
        var x = s; x.name = name; x.country = country; x.city = city; x.load = load; x.portForward = p2p
        x.secureCore = false; x.tor = false; x.enabled = true; x.ip = name
        return x
    }
    let rows = [server("CH#2", "Switzerland", "Zurich", load: 10), server("US#3", "United States", "Miami", load: 5),
                server("CH#1", "Switzerland", "Geneva", load: 90, p2p: true), server("CH#3", "Switzerland", "Zurich", load: 10)]
    #expect(rows.sorted(by: ServerQuery.nearestFirst).map(\.name) == ["CH#1", "CH#2", "CH#3", "US#3"])
    var q = ServerQuery()
    q.p2p = true
    #expect(rows.filter(q.matches).map(\.name) == ["CH#1"])
    q = ServerQuery(); q.search = "zur"
    #expect(rows.filter(q.matches).count == 2)
}

@Test func processRunnerReadsLargeStderrAndTimesOut() async throws {
    let sh = URL(fileURLWithPath: "/bin/sh")
    let r = try await ProcessRunner(timeout: 10).run(sh, ["-c", "head -c 300000 /dev/zero | tr '\\\\0' x >&2; echo done"])
    #expect(r.code == 0 && String(decoding: r.stdout, as: UTF8.self) == "done\n" && r.stderr.count == 300_000)
    let t0 = Date()
    await #expect(throws: ProcessRunner.Failure.self) {
        _ = try await ProcessRunner(timeout: 0.5).run(sh, ["-c", "sleep 20"])
    }
    #expect(Date().timeIntervalSince(t0) < 5)
}
