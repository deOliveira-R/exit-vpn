import Foundation
import Testing
@testable import ExitControlCore

let statusJSON = """
{"server":"CH#697","country":"Switzerland","city":"Zurich","endpoint":"89.222.96.158:51820",
 "pubkey":"k=","since":1.0,"profile":["port_forward","stream"],"entry":"","features":["port_forward","stream","ipv6"],
 "load":31,"health":"failing_over","health_since":2.0,"exit_ip":"203.0.113.9","exit_loc":"CH",
 "handshake_age":3.1,"last_ok_age":1.0,"errors":[],"warnings":["slot a key certificate expires in 9 days"],
 "prefs":{"regions":[{"country":"Switzerland","city":null},{"country":"United States","city":"New York"}],
          "auto_failover":true,"secure_core":false,"allow_free":false,"require":[],"countries":["Switzerland","United States"]},
 "provider":"proton","key_options":{"NetShield":"0"},
 "catalog":{"servers":1077,"source":"push:proton api by o","pushed":true,"fetched_at":5.0,"enabled":1052},
 "blacklisted":1,"events":[{"ts":9.0,"kind":"failover_done","old":"CH#697","new":"CH#821","exit_ip":"x","seconds":21.5}]}
"""

@Test func decodesStatus() throws {
    let st = try ControllerClient.decoder.decode(Status.self, from: Data(statusJSON.utf8))
    #expect(st.server == "CH#697" && st.health == .failingOver && st.endpointIP == "89.222.96.158")
    #expect(st.prefs.regions[1] == Region(country: "United States", city: "New York"))
    #expect(st.catalog.pushed && st.keyOptions?["NetShield"] == "0")
    let ev = try #require(st.events?.first)
    #expect(ev.summary == "Failed over CH#697 → CH#821")
    #expect(ev.data["seconds"]?.double == 21.5)
    let h = try ControllerClient.decoder.decode(Event.self, from: Data("""
        {"ts":1,"kind":"health","state":"failing_over","errors":["dns: TimeoutError","x"]}
        """.utf8))
    #expect(h.summary == "Health: failing over (dns: TimeoutError)")
}

@Test func regionEncodesNullCity() throws {
    let e = JSONEncoder(); e.outputFormatting = .sortedKeys
    #expect(String(decoding: try e.encode(Region(country: "Germany")), as: UTF8.self) == #"{"city":null,"country":"Germany"}"#)
}

@Test func prefsPatchOmitsUnsetFields() throws {
    let data = try ControllerClient.encoder.encode(ControllerClient.PrefsPatch(secureCore: true))
    #expect(String(decoding: data, as: UTF8.self) == #"{"secure_core":true}"#)
}

@Test func sseParser() {
    var p = SSEParser()
    let lines = ["event: status", "data: {\"a\":1}", "", ": keepalive", "", "data: x", "data: y", ""]
    let msgs = lines.compactMap { p.feed($0) }
    #expect(msgs == [SSEMessage(event: "status", data: "{\"a\":1}"), SSEMessage(event: "message", data: "x\ny")])
}

@Test func serverBadgesAndFlags() throws {
    let json = """
    {"name":"CH-US#3","country":"United States","city":"New York","hostname":"node-us-244.protonvpn.net",
     "ip":"1.2.3.4","pubkey":"k","secure_core":true,"tor":false,"free":false,"stream":true,"port_forward":false,
     "ipv6":true,"load":40,"enabled":true,"entry":"CH"}
    """
    let s = try ControllerClient.decoder.decode(Server.self, from: Data(json.utf8))
    #expect(s.badges == ["SC CH", "Streaming", "IPv6"])
    #expect(countryCode(hostname: s.hostname) == "US" && countryCode(hostname: "ch-03.x") == "CH")
    #expect(countryCode(s) == "US")
    #expect(flag("ch") == "🇨🇭" && flag(nil) == "🌐" && flag("UK") == "🇬🇧")
    #expect(continent("UK") == "Europe" && continent("br") == "South America" && continent("ZZ") == "Other")
}

@Test func tailscaleExitNodeUse() {
    let using = #"{"Peer":{"a":{"HostName":"exit-proton","ExitNode":true},"b":{"HostName":"nas"}}}"#
    let other = #"{"Peer":{"a":{"HostName":"exit-proton","ExitNode":false},"b":{"HostName":"nas","ExitNode":true}}}"#
    #expect(Tailscale.parse(Data(using.utf8), node: "exit-proton") == .usingThis)
    #expect(Tailscale.parse(Data(other.utf8), node: "exit-proton") == .usingOther("nas"))
    #expect(Tailscale.parse(Data(#"{"Peer":{}}"#.utf8), node: "exit-proton") == ExitNodeUse.none)
}

@Test func exitctlNeedInputAndOptions() async throws {
    // a fake exitctl: prints what the real one prints when 2FA is needed
    let dir = FileManager.default.temporaryDirectory.appending(path: "exitctl-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let script = dir.appending(path: "exitctl")
    try """
    #!/bin/sh
    case "$2" in
      login) cat > /dev/null; echo '{"error": "a 2FA code is needed", "need": "totp"}'; exit 3 ;;
      options) echo '[{"key":"netshield","label":"NetShield","kind":"choice","default":0,"choices":{"0":"off","1":"malware","2":"ads"},"help":""}]' ;;
      *) echo '{"error": "boom"}'; exit 1 ;;
    esac
    """.write(to: script, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
    let x = Exitctl(executable: script, controllerURL: "http://exit-proton:8080")
    await #expect {
        _ = try await x.login("proton", .init(username: "u", password: "p"))
    } throws: { e in
        if case ExitctlError.needInput(let what, _, _) = e { return what == "totp" } else { return false }
    }
    let opts = try await x.options("proton")
    #expect(opts.first?.sortedChoices.map(\.value) == ["0", "1", "2"])
    await #expect(throws: ExitctlError.self) { try await x.perform("proton", .renewKey(slot: "a")) }
}

@Test func exitURLs() {
    func p(_ s: String) -> ExitURL? { ExitURL(URL(string: s)!) }
    #expect(p("exitvpn://servers") == .open(.servers))
    #expect(p("exitvpn://settings/account") == .open(.settings(.account)))
    #expect(p("exitvpn://settings/nonsense") == .open(.settings(.general)))
    #expect(p("exitvpn://rotate?scope=city") == .run(.rotate(.city)))
    #expect(p("exitvpn://rotate") == .run(.rotate(.any)))
    #expect(p("exitvpn://rotate?scope=moon") == nil)
    #expect(p("exitvpn://connect?country=United%20States&city=New%20York")
            == .run(.connect(Region(country: "United States", city: "New York"))))
    #expect(p("exitvpn://connect?country=Switzerland") == .run(.connect(Region(country: "Switzerland"))))
    #expect(p("exitvpn://connect?server=CH%23821") == .run(.connectServer(name: "CH#821", ip: nil)))
    #expect(p("exitvpn://connect") == nil)
    #expect(p("exitvpn://securecore?on=1") == .run(.secureCore(true)))
    #expect(p("exitvpn://securecore?on=off") == .run(.secureCore(false)))
    #expect(p("exitvpn://securecore") == nil)
    #expect(p("exitvpn://refresh") == .refresh)
    #expect(p("https://rotate") == nil && p("exitvpn://launch-missiles") == nil)
}

@Test func vocabularyMatchesTheWire() throws {
    #expect(Health(wire: "failing_over") == .failingOver && Health(wire: "new_state") == .unknown("new_state"))
    #expect(Feature.allCases.map(\.rawValue) == ["port_forward", "stream", "ipv6"])
    #expect(RotationScope.allCases.map(\.rawValue) == ["city", "country", "any"])
    #expect(FindOutcome(wire: "challenge") == .challenge && FindOutcome(wire: "x").icon == "❔")
    #expect(ProviderCommand.rotateKey(slot: "a", options: ["netshield": "2", "moderate_nat": "on"]).arguments
            == ["rotate", "--slot", "a", "--moderate-nat", "on", "--netshield", "2"])
}
