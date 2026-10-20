import AppKit
import ExitControlCore
import SwiftUI

/// Provider login through `exitctl <p> login --stdin`: credentials, then a 2FA code
/// and/or a CAPTCHA in the browser when the provider asks. The password lives only in
/// this sheet's state and on exitctl's stdin.
struct LoginSheet: View {
    let model: AppModel
    @Environment(\.dismiss) private var dismiss

    enum Step { case credentials, totp, captcha(url: URL, token: String) }
    @State private var step: Step = .credentials
    @State private var username = ""
    @State private var password = ""
    @State private var totp = ""
    @State private var working = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Log in to \(model.provider.capitalized)").font(.headline)
            switch step {
            case .credentials:
                TextField("Username or email", text: $username).textContentType(.username)
                SecureField("Password", text: $password).textContentType(.password)
            case .totp:
                Text("Enter the code from your authenticator app.")
                TextField("2FA code", text: $totp).textContentType(.oneTimeCode)
            case .captcha(let url, _):
                Text("\(model.provider.capitalized) asks for a CAPTCHA. Solve it in your browser, then continue here.")
                Button("Open CAPTCHA in Browser") { NSWorkspace.shared.open(url) }
            }
            if let error { Text(error).foregroundStyle(.red).font(.caption) }
            HStack {
                if working { ProgressView().controlSize(.small) }
                Spacer()
                Button("Cancel") { finish() }.keyboardShortcut(.cancelAction)
                Button(continueLabel) { Task { await next() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(working || !canContinue)
            }
        }
        .padding(20)
        .frame(width: 380)
    }

    var continueLabel: String { if case .captcha = step { return "I Solved It" } else { return "Continue" } }
    var canContinue: Bool {
        switch step {
        case .credentials: return !username.isEmpty && !password.isEmpty
        case .totp: return totp.count >= 6
        case .captcha: return true
        }
    }

    func next() async {
        guard let x = model.exitctl else { error = ExitctlError.notInstalled.localizedDescription; return }
        working = true; error = nil
        defer { working = false }
        let req: Exitctl.LoginStep
        switch step {
        case .credentials: req = .init(username: username, password: password)
        case .totp: req = .init(totp: totp)
        case .captcha(_, let token): req = .init(username: username, password: password, hvToken: token, hvType: "captcha")
        }
        do {
            _ = try await x.login(model.provider, req)
            finish()
        } catch ExitctlError.needInput(let what, let message, let info) {
            if what == "totp" {
                password = ""     // the half-open session is in the Keychain now
                step = .totp
            } else if what == "human_verification", let u = info["url"]?.string.flatMap(URL.init(string:)),
                      let token = info["token"]?.string {
                step = .captcha(url: u, token: token)
                NSWorkspace.shared.open(u)
            } else {
                error = message
            }
        } catch {
            self.error = error.localizedDescription
            if case .totp = step { totp = "" }
        }
    }

    func finish() {
        password = ""; totp = ""
        Task { await model.loadProvider() }
        dismiss()
    }
}
