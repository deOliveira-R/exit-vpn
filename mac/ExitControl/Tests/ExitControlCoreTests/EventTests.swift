import Foundation
import Testing
@testable import ExitControlCore

// Logic: the one-line summaries the event log and notifications show.

@Test func aFailoverSummaryNamesBothServers() throws {
    let ev = try #require(try sample("controller/events", as: [String: Event].self)["failover_done"])
    #expect(ev.summary == "Failed over US-IL#510 → US-IL#710")
    #expect(ev.data["seconds"]?.double == 1.0)
}

@Test func aHealthSummaryShowsTheStateAndTheFirstError() throws {
    let ev = try ControllerClient.decoder.decode(Event.self, from: Data("""
        {"ts":1,"kind":"health","state":"failing_over","errors":["dns: TimeoutError","x"]}
        """.utf8))
    #expect(ev.summary == "Health: failing over (dns: TimeoutError)")
}

@Test func anIPv6SummarySaysWhetherIPv6GoesThroughTheExit() throws {
    let open = try #require(try sample("controller/events", as: [String: Event].self)["ipv6"])
    #expect(open.summary == "IPv6 through the exit on US-NY#1")
    let closed = try ControllerClient.decoder.decode(Event.self, from: Data("""
        {"ts":1,"kind":"ipv6","open":false,"server":"US-NJ#41","exit_ip6":null}
        """.utf8))
    #expect(closed.summary == "IPv6 off on US-NJ#41; apps use IPv4")
}

@Test func aHandoverAndTheStandbySayWhatHappened() throws {
    let samples = try sample("controller/events", as: [String: Event].self)
    #expect(try #require(samples["handover"]).summary == "Handed over US-NY#723 → US-NY#5 (standby, instant)")
    #expect(try #require(samples["standby"]).summary == "Standby ready on US-NY#5")
    let lost = try ControllerClient.decoder.decode(Event.self, from: Data("""
        {"ts":1,"kind":"standby","state":"lost","server":"US-NY#5","reason":"x"}
        """.utf8))
    #expect(lost.summary == "Standby US-NY#5 failed; finding another")
    #expect(try #require(samples["uplink_suspect"]).summary.hasPrefix("Both tunnels failing"))
}
