import Foundation
import Testing
@testable import ExitControlCore

// Boundary: running exitctl: its needs-input answers, its options, its failures, its arguments.

@Test func aLoginNeedingTwoFactorIsANeedInputError() async throws {
    let x = try fakeExitctl()
    await #expect {
        _ = try await x.login("proton", .init(username: "u", password: "p"))
    } throws: { e in
        if case ExitctlError.needInput(let what, _, _) = e { return what == "totp" } else { return false }
    }
}

@Test func optionChoicesComeInTheirNumericOrder() async throws {
    let opts = try await fakeExitctl().options("proton")
    #expect(opts.first?.sortedChoices.map(\.value) == ["0", "1", "2"])
}

@Test func aFailingCommandThrowsAnExitctlError() async throws {
    let x = try fakeExitctl()
    await #expect(throws: ExitctlError.self) { try await x.perform("proton", .renewKey(slot: "a")) }
}

@Test func aKeyRotationPassesItsLeversAsSortedFlags() {
    #expect(ProviderCommand.rotateKey(slot: "a", options: ["netshield": "2", "moderate_nat": "on"]).arguments
            == ["rotate", "--slot", "a", "--moderate-nat", "on", "--netshield", "2"])
}
