import Foundation
import Observation

/// The provider login, one exitctl step at a time (the sheet shows `step`):
///
///     credentials ──► (2FA on?) totp ──► done
///          └──► (CAPTCHA asked?) captcha(url) ──► credentials again with the token ──► …
///
/// After the password step the half-open session waits in exitctl's Keychain entry, so the
/// password is forgotten here as soon as exitctl asks for the 2FA code.
@MainActor @Observable
public final class LoginFlow {
    public enum Step: Equatable { case credentials, totp, captcha(url: URL, token: String), done }

    public private(set) var step: Step = .credentials
    public private(set) var working = false
    public private(set) var error: String?
    public var username = ""
    public var password = ""
    public var totp = ""

    private let login: @MainActor (LoginStep) async throws -> Account
    /// Opens the CAPTCHA page (the browser); the sheet passes NSWorkspace.
    private let openURL: @MainActor (URL) -> Void

    public init(login: @escaping @MainActor (LoginStep) async throws -> Account,
                openURL: @escaping @MainActor (URL) -> Void = { _ in }) {
        self.login = login
        self.openURL = openURL
    }

    public var canContinue: Bool {
        switch step {
        case .credentials: return !username.isEmpty && !password.isEmpty
        case .totp: return totp.count >= 6
        case .captcha: return true
        case .done: return false
        }
    }

    public func next() async {
        working = true; error = nil
        defer { working = false }
        let request: LoginStep
        switch step {
        case .credentials: request = LoginStep(username: username, password: password)
        case .totp: request = LoginStep(totp: totp)
        case .captcha(_, let token): request = LoginStep(username: username, password: password, hvToken: token, hvType: "captcha")
        case .done: return
        }
        do {
            _ = try await login(request)
            finish()
            step = .done
        } catch ExitctlError.needInput(let what, let message, let info) {
            switch what {
            case "totp":
                password = ""        // the half-open session is in the Keychain now
                step = .totp
            case "human_verification":
                if let url = info["url"]?.string.flatMap(URL.init(string:)), let token = info["token"]?.string {
                    step = .captcha(url: url, token: token)
                    openURL(url)
                } else { error = message }
            case "password":
                step = .credentials
                error = message
            default:
                error = message
            }
        } catch {
            self.error = error.localizedDescription
            if case .totp = step { totp = "" }
        }
    }

    /// Forget what was typed (also on cancel).
    public func finish() { password = ""; totp = "" }
}
