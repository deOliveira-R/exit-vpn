import Foundation
import Testing
@testable import ExitControlCore

// Component: whether this Mac uses the exit node (Tailscale status), the guard that keeps it
// on, and the monitor that feeds the guard.

private func use(_ json: String) -> ExitNodeUse { Tailscale.parse(Data(json.utf8), node: "exit-proton") }

@Test func theControllersNodeAsExitNodeIsUsingThis() {
    #expect(use(#"{"Peer":{"a":{"HostName":"exit-proton","ExitNode":true},"b":{"HostName":"nas"}}}"#) == .usingThis)
}

@Test func anotherExitNodeIsReportedByName() {
    #expect(use(#"{"Peer":{"a":{"HostName":"exit-proton","ExitNode":false},"b":{"HostName":"nas","ExitNode":true}}}"#)
            == .usingOther("nas"))
}

@Test func noExitNodeIsNone() {
    #expect(use(#"{"Peer":{}}"#) == ExitNodeUse.none)
    #expect(use(#"{"BackendState":"Running","Peer":{}}"#) == ExitNodeUse.none)
}

@Test func aStoppedTailscaleIsOffNotNone() {
    #expect(use(#"{"BackendState":"Stopped"}"#) == .tailscaleOff("Stopped"))
}

@MainActor @Test func theGuardSelectsTheNodeWhenThereIsNone() async {
    let fake = FakeSelector()
    #expect(await fake.exitGuard.check(.none, node: "exit-proton", enabled: true) == .reselected)
    #expect(fake.selected == ["exit-proton"])
}

@MainActor @Test func theGuardLeavesEveryOtherStateAlone() async {
    let fake = FakeSelector()
    for use in [ExitNodeUse.usingThis, .usingOther("home-gw"), .tailscaleOff("Stopped"), .unknown("x")] {
        #expect(await fake.exitGuard.check(use, node: "exit-proton", enabled: true) == .nothing, "\(use)")
    }
    #expect(fake.selected.isEmpty)
}

@MainActor @Test func theGuardDoesNothingWhenTurnedOff() async {
    let fake = FakeSelector()
    #expect(await fake.exitGuard.check(.none, node: "exit-proton", enabled: false) == .nothing)
    #expect(fake.selected.isEmpty)
}

@MainActor @Test func aPauseHoldsTheGuardAndEndsByItself() async {
    let fake = FakeSelector()
    fake.exitGuard.pause(for: 600)
    #expect(await fake.exitGuard.check(.none, node: "exit-proton", enabled: true) == .nothing)
    fake.clock += 601
    #expect(await fake.exitGuard.check(.none, node: "exit-proton", enabled: true) == .reselected)
    #expect(fake.exitGuard.pausedUntil == nil)
}

@MainActor @Test func aRepeatedFailureIsReportedOnceAndClearedBySuccess() async {
    let fake = FakeSelector()
    fake.failWith = "not allowed"
    #expect(await fake.exitGuard.check(.none, node: "exit-proton", enabled: true) == .failed("not allowed"))
    #expect(await fake.exitGuard.check(.none, node: "exit-proton", enabled: true) == .nothing)
    #expect(fake.exitGuard.lastFailure == "not allowed")
    fake.failWith = nil
    #expect(await fake.exitGuard.check(.none, node: "exit-proton", enabled: true) == .reselected)
    #expect(fake.exitGuard.lastFailure == nil)
}

@MainActor @Test func theMonitorRereadsSoonAfterTheGuardActs() async throws {
    let readings = Counter()
    let m = ExitNodeMonitor(interval: .seconds(60), read: { _ in await readings.next() == 0 ? .none : .usingThis })
    var seen: [ExitNodeUse] = []
    m.onUse = { use in seen.append(use); return use == .none }
    m.watch(node: "exit-proton")
    for _ in 0..<40 where seen.count < 2 { try await Task.sleep(for: .milliseconds(100)) }
    #expect(seen == [.none, .usingThis], "a second reading one second after the change, not 60")
}
