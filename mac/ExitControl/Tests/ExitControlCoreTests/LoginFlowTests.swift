import Foundation
import Testing
@testable import ExitControlCore

// Component: the login steps (password, CAPTCHA, 2FA) against exitctl's answers.

@MainActor @Test func aCaptchaOpensTheVerificationPage() async {
    let fake = FakeLogin()
    await fake.flow.next()
    guard case .captcha(_, "t") = fake.flow.step else { Issue.record("expected the CAPTCHA step"); return }
    #expect(fake.opened.map(\.absoluteString) == ["https://verify.example/?token=t"])
}

@MainActor @Test func theTotpStepForgetsThePassword() async {
    let fake = FakeLogin()
    await fake.flow.next()
    await fake.flow.next()
    #expect(fake.flow.step == .totp && fake.flow.password.isEmpty)
}

@MainActor @Test func eachStepSendsWhatThePreviousAnswerAskedFor() async {
    let fake = FakeLogin()
    await fake.flow.next()
    await fake.flow.next()
    fake.flow.totp = "123456"
    await fake.flow.next()
    #expect(fake.flow.step == .done)
    #expect(fake.calls == [LoginStep(username: "u", password: "pw"),
                           LoginStep(username: "u", password: "pw", hvToken: "t", hvType: "captcha"),
                           LoginStep(totp: "123456")])
}
