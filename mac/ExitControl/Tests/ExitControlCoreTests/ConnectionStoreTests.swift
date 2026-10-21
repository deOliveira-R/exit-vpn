import Foundation
import Testing
@testable import ExitControlCore

// Component: the connection's stream state, which updates notify, and the bounded event log.

@MainActor @Test func onlyAWarningThatAppearsLaterIsNotified() throws {
    let (c, base, notes) = try connectedStore()
    var st = base
    st.warnings = ["slot a key certificate expires in 5 days"]
    c.handle(.status(st))
    #expect(c.online && c.status?.server == st.server && notes().isEmpty, "present at start: no notification")
    st.warnings = ["slot a key certificate expires in 5 days", "pushed catalog is 9 days old"]
    c.handle(.status(st))
    c.handle(.status(st))
    #expect(notes() == [.warning("pushed catalog is 9 days old")])
}

@MainActor @Test func degradedIsNotifiedAndARoutineSwitchIsNot() throws {
    let (c, _, notes) = try connectedStore()
    let events = try sample("controller/events", as: [String: Event].self)
    c.handle(.event(events["degraded"]!))
    c.handle(.event(events["switched"]!))
    #expect(notes() == [.event(events["degraded"]!)])
}

@MainActor @Test func anUnreadableStatusIsFlaggedUntilAReadableOneArrives() throws {
    let (c, st, _) = try connectedStore()
    c.handle(.incompatible("unreadable status"))
    #expect(c.incompatible != nil)
    c.handle(.status(st))
    #expect(c.incompatible == nil)
}

@MainActor @Test func aDisconnectGoesOfflineWithItsReason() throws {
    let (c, st, _) = try connectedStore()
    c.handle(.status(st))
    c.handle(.disconnected("timed out"))
    #expect(!c.online && c.offlineReason == "timed out")
}

@MainActor @Test func theEventLogKeepsOnlyTheNewest() throws {
    let c = ConnectionStore()
    let ev = try sample("controller/events", as: [String: Event].self)["health"]!
    for _ in 0..<(ConnectionStore.logLimit + 20) { c.append(ev) }
    #expect(c.log.count == ConnectionStore.logLimit)
}
