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
