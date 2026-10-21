import Foundation
import Testing
@testable import ExitControlCore

// Boundary: running a child process (exitctl, tailscale, launchctl): output, stderr, timeouts.

private let sh = URL(fileURLWithPath: "/bin/sh")

@Test func aLargeStderrDoesNotStallTheProcess() async throws {
    let r = try await ProcessRunner(timeout: 10).run(sh, ["-c", "head -c 300000 /dev/zero | tr '\\\\0' x >&2; echo done"])
    #expect(r.code == 0 && String(decoding: r.stdout, as: UTF8.self) == "done\n" && r.stderr.count == 300_000)
}

@Test func aHungProcessIsEndedAtTheTimeout() async throws {
    let t0 = Date()
    await #expect(throws: ProcessRunner.Failure.self) {
        _ = try await ProcessRunner(timeout: 0.5).run(sh, ["-c", "sleep 20"])
    }
    #expect(Date().timeIntervalSince(t0) < 5)
}
