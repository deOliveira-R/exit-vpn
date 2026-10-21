import Foundation
import Testing
@testable import ExitControlCore

// Component: one change at a time; menu clicks are refused while busy, URLs and Shortcuts queue.

@MainActor @Test func aMenuClickIsRefusedWhileAChangeRuns() async throws {
    let busy = BusyRunner()
    var order: [String] = []
    let first = await busy.start { order.append($0) }
    #expect(try await busy.runner.submit("click") { order.append("click") } == false)
    busy.release()
    _ = try await first.value
    #expect(order == ["first"])
}

@MainActor @Test func queuedChangesRunInOrderAndEachRefreshes() async throws {
    let busy = BusyRunner()
    var order: [String] = []
    var refreshed = 0
    busy.runner.afterSuccess = { refreshed += 1 }
    let first = await busy.start { order.append($0) }
    let queued = (1...3).map { i in
        Task { try await busy.runner.submit("url \(i)", policy: .enqueue) { order.append("url \(i)") } }
    }
    try await Task.sleep(for: .milliseconds(50))
    busy.release()
    _ = try await first.value
    for t in queued { _ = try await t.value }
    #expect(order == ["first", "url 1", "url 2", "url 3"])
    #expect(refreshed == 4 && busy.runner.busy == nil && busy.runner.isIdle)
}

@MainActor @Test func aFailureIsKeptReportedAndClearedByTheNextSuccess() async throws {
    struct Boom: LocalizedError { var errorDescription: String? { "no candidate worked" } }
    let runner = ActionRunner()
    var failures: [String] = []
    runner.onFailure = { label, msg in failures.append("\(label): \(msg)") }
    await #expect(throws: Boom.self) { try await runner.submit("Rotate") { throw Boom() } }
    #expect(runner.lastError == "Rotate: no candidate worked" && failures == ["Rotate: no candidate worked"])
    try await runner.submit("ok") {}
    #expect(runner.lastError == nil)
}
