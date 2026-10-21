import AppKit
import ExitControlCore
import SwiftUI

/// Provider login through exitctl, one step at a time (`LoginFlow`): credentials, then a 2FA
/// code and/or a CAPTCHA in the browser when the provider asks. The password lives only in
/// the flow's state and on exitctl's stdin.
struct LoginSheet: View {
    let app: AppContainer
    @Environment(\.dismiss) private var dismiss
    @State private var flow = LoginFlow(login: { step in try await AppContainer.shared.provider.login(step) },
                                        openURL: { NSWorkspace.shared.open($0) })

    var body: some View {
        @Bindable var f = flow
        VStack(alignment: .leading, spacing: 12) {
            Text("Log in to \(app.provider.provider.capitalized)").font(.headline)
            switch flow.step {
            case .credentials, .done:
                TextField("Username or email", text: $f.username).textContentType(.username)
                SecureField("Password", text: $f.password).textContentType(.password)
            case .totp:
                Text("Enter the code from your authenticator app.")
                TextField("2FA code", text: $f.totp).textContentType(.oneTimeCode)
            case .captcha(let url, _):
                Text("\(app.provider.provider.capitalized) asks for a CAPTCHA. Solve it in your browser, then continue here.")
                Button("Open CAPTCHA in Browser") { NSWorkspace.shared.open(url) }
            }
            if let error = flow.error { Text(error).foregroundStyle(.red).font(.caption) }
            HStack {
                if flow.working { ProgressView().controlSize(.small) }
                Spacer()
                Button("Cancel") { close() }.keyboardShortcut(.cancelAction)
                Button(continueLabel) { Task { await flow.next(); if flow.step == .done { close() } } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(flow.working || !flow.canContinue)
            }
        }
        .padding(20)
        .frame(width: 380)
    }

    var continueLabel: String { if case .captcha = flow.step { return "I Solved It" } else { return "Continue" } }

    func close() {
        flow.finish()
        Task { await app.provider.load() }
        dismiss()
    }
}
